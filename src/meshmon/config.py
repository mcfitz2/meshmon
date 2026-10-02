"""The plugin's config.json, which openHop's plugin manager keeps in
``$OPENHOP_PLUGIN_DATA`` and edits from its Plugins page."""

import dataclasses
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_PUBLIC_KEY = re.compile(r"[0-9a-f]{64}")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Node:
    """A repeater or room server to poll. The password logs in to it; a guest
    password is enough for status and telemetry, and blank works where the node
    allows it."""

    name: str
    public_key: str
    password: str = ""


@dataclass(frozen=True)
class Config:
    companion_host: str = "127.0.0.1"
    companion_port: int = 5000
    """The openHop companion meshmon talks to the mesh through."""
    listen_host: str = "0.0.0.0"
    listen_port: int = 9110
    """Where /metrics is served."""
    interval_seconds: int = 900
    down_after_misses: int = 2
    """Polls in a row a node must miss before it is reported down."""
    nodes: tuple[Node, ...] = ()


_KEYS = {field.name for field in dataclasses.fields(Config)}
_NODE_KEYS = {field.name for field in dataclasses.fields(Node)}


def _node(position: int, raw: Any) -> Node:
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str) or not raw["name"]:
        raise ConfigError(f"node {position} in nodes needs a name")
    public_key = str(raw.get("public_key", "")).lower()
    if not _PUBLIC_KEY.fullmatch(public_key):
        raise ConfigError(f"node {raw['name']}: public_key must be 64 hex digits")
    for key in sorted(set(raw) - _NODE_KEYS):
        log.warning("node %d in nodes: ignoring unknown key %s", position, key)
    return Node(raw["name"], public_key, str(raw.get("password", "")))


def _positive(raw: dict[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{key} must be a whole number of at least 1, not {value!r}")
    return value


def _port(raw: dict[str, Any], key: str, default: int) -> int:
    value = _positive(raw, key, default)
    if value > 65535:
        raise ConfigError(f"{key} must be at most 65535, not {value!r}")
    return value


def _text(raw: dict[str, Any], key: str, default: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key} must be a host name or address, not {value!r}")
    return value


def load(path: Path) -> Config:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(f"can't read {path}: {error}") from error
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must hold a JSON object")
    if not isinstance(raw.get("nodes", []), list):
        raise ConfigError("nodes must be a list")
    for key in sorted(set(raw) - _KEYS):
        log.warning("%s: ignoring unknown key %s", path, key)
    nodes = tuple(
        _node(position, node) for position, node in enumerate(raw.get("nodes", []), start=1)
    )
    for kind in ("name", "public_key"):
        values = [getattr(node, kind) for node in nodes]
        if len(set(values)) != len(values):
            raise ConfigError(f"a node {kind} appears twice")
    defaults = Config()
    return Config(
        companion_host=_text(raw, "companion_host", defaults.companion_host),
        companion_port=_port(raw, "companion_port", defaults.companion_port),
        listen_host=_text(raw, "listen_host", defaults.listen_host),
        listen_port=_port(raw, "listen_port", defaults.listen_port),
        interval_seconds=_positive(raw, "interval_seconds", defaults.interval_seconds),
        down_after_misses=_positive(raw, "down_after_misses", defaults.down_after_misses),
        nodes=nodes,
    )
