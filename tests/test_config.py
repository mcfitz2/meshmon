import json
import logging
from pathlib import Path

import pytest

from meshmon.config import Config, ConfigError, Node, load

KEY = "0123456789abcdef" * 4


def write(path: Path, data: object) -> Path:
    path.write_text(json.dumps(data))
    return path


def test_defaults_fill_in_everything_but_the_nodes(tmp_path: Path) -> None:
    config = load(write(tmp_path / "config.json", {"nodes": []}))

    assert config == Config(
        companion_host="127.0.0.1",
        companion_port=5000,
        listen_host="0.0.0.0",
        listen_port=9110,
        interval_seconds=900,
        down_after_misses=2,
        nodes=(),
    )


def test_nodes_are_read_with_a_blank_password_by_default(tmp_path: Path) -> None:
    config = load(
        write(
            tmp_path / "config.json",
            {
                "interval_seconds": 600,
                "nodes": [{"name": "Hilltop Repeater", "public_key": KEY.upper()}],
            },
        )
    )

    assert config.interval_seconds == 600
    assert config.nodes == (Node(name="Hilltop Repeater", public_key=KEY, password=""),)


@pytest.mark.parametrize(
    "data, message",
    [
        ({"nodes": [{"name": "x", "public_key": "abc"}]}, "public_key"),
        ({"nodes": [{"public_key": KEY}]}, "name"),
        (
            {"nodes": [{"name": "a", "public_key": KEY}, {"name": "a", "public_key": KEY[::-1]}]},
            "twice",
        ),
        ({"interval_seconds": 0, "nodes": []}, "interval_seconds"),
        ({"down_after_misses": 0, "nodes": []}, "down_after_misses"),
        ({"interval_seconds": True, "nodes": []}, "interval_seconds"),
        ({"listen_port": 70000, "nodes": []}, "listen_port"),
        ({"companion_port": 65536, "nodes": []}, "companion_port"),
        ({"nodes": {"a": 1}}, "nodes must be a list"),
        ({"companion_host": None, "nodes": []}, "companion_host"),
        ({"listen_host": 5, "nodes": []}, "listen_host"),
    ],
)
def test_bad_config_is_refused_with_the_reason(tmp_path: Path, data: object, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load(write(tmp_path / "config.json", data))


def test_a_missing_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config.json"):
        load(tmp_path / "config.json")


def test_a_bad_node_is_named_by_position_without_echoing_it(tmp_path: Path) -> None:
    data = {
        "nodes": [
            {"name": "ok", "public_key": KEY},
            {"public_key": KEY[::-1], "password": "hunter2"},
        ]
    }
    with pytest.raises(ConfigError) as error:
        load(write(tmp_path / "config.json", data))
    assert "node 2" in str(error.value)
    assert "hunter2" not in str(error.value)


def test_a_node_that_is_not_an_object_is_named_by_position_without_echoing_it(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigError) as error:
        load(write(tmp_path / "config.json", {"nodes": ["hunter2"]}))
    assert "node 1" in str(error.value)
    assert "hunter2" not in str(error.value)


def test_unknown_keys_are_warned_about(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    data = {
        "interval_second": 600,
        "nodes": [{"name": "a", "public_key": KEY, "pasword": "x"}],
    }
    with caplog.at_level(logging.WARNING):
        config = load(write(tmp_path / "config.json", data))

    assert config.interval_seconds == 900
    assert "interval_second" in caplog.text
    assert "pasword" in caplog.text


def test_port_65535_is_allowed(tmp_path: Path) -> None:
    config = load(write(tmp_path / "config.json", {"listen_port": 65535, "nodes": []}))

    assert config.listen_port == 65535
