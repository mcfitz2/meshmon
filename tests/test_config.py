import json
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
    ],
)
def test_bad_config_is_refused_with_the_reason(tmp_path: Path, data: object, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load(write(tmp_path / "config.json", data))


def test_a_missing_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config.json"):
        load(tmp_path / "config.json")
