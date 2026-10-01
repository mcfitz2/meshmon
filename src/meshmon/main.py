"""``meshmon [config.json]``: serve /metrics and poll the nodes every interval.

openHop's plugin manager runs it with ``$OPENHOP_PLUGIN_DATA`` set, and the
config is read from ``config.json`` there unless a path is given.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

from prometheus_client import CollectorRegistry, Gauge, start_http_server

from meshmon import config, link
from meshmon.metrics import Metrics
from meshmon.poller import Poller

log = logging.getLogger("meshmon")


def _config_path(argv: list[str]) -> Path:
    if len(argv) > 1:
        return Path(argv[1])
    data = os.environ.get("OPENHOP_PLUGIN_DATA")
    if data is None:
        raise config.ConfigError("give a config path, or set OPENHOP_PLUGIN_DATA")
    return Path(data) / "config.json"


async def run(settings: config.Config, registry: CollectorRegistry) -> None:
    companion_up = Gauge(
        "meshmon_companion_up",
        "1 if meshmon reached the openHop companion on its last round of polls",
        registry=registry,
    )
    poller = Poller(settings.nodes, settings.down_after_misses, Metrics(registry))
    while True:
        try:
            async with link.connect(settings.companion_host, settings.companion_port) as session:
                companion_up.set(1)
                await poller.poll(session)
        except link.LinkDownError as error:
            log.error("%s", error)
            companion_up.set(0)
        await asyncio.sleep(settings.interval_seconds)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        path = _config_path(sys.argv)
        settings = config.load(path)
    except config.ConfigError as error:
        sys.exit(f"meshmon: {error}")
    if path.stat().st_mode & 0o077:
        log.warning("%s holds node passwords; make it private with chmod 600", path)
    if not settings.nodes:
        log.warning("no nodes in %s; add them to poll", path)
    registry = CollectorRegistry()
    start_http_server(settings.listen_port, settings.listen_host, registry=registry)
    log.info("serving /metrics on %s:%d", settings.listen_host, settings.listen_port)
    asyncio.run(run(settings, registry))
