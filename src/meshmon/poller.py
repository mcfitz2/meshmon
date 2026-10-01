"""One round of polls: log in to each node, ask for its status and telemetry,
and report it up or down."""

import logging
import time
from collections.abc import Callable, Iterable

from meshmon.config import Node
from meshmon.link import NoReplyError, Session
from meshmon.metrics import Metrics

log = logging.getLogger(__name__)


class Poller:
    def __init__(
        self,
        nodes: Iterable[Node],
        down_after_misses: int,
        metrics: Metrics,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.nodes = tuple(nodes)
        self.down_after_misses = down_after_misses
        self.metrics = metrics
        self.clock = clock
        self._misses = dict.fromkeys((node.name for node in self.nodes), 0)

    async def poll(self, session: Session) -> None:
        """Poll every node once. A LinkDownError from the session ends the round
        without counting a miss: the companion failed, not the nodes."""
        for node in self.nodes:
            try:
                await self._poll(session, node)
            except NoReplyError as error:
                log.info("%s", error)
                self._missed(node)
            else:
                self._misses[node.name] = 0
                self.metrics.missed_polls.labels(node.name).set(0)
                self.metrics.up.labels(node.name).set(1)

    async def _poll(self, session: Session, node: Node) -> None:
        if not await session.login(node):
            log.warning("%s refused the login; check its password in config.json", node.name)
            self.metrics.login_ok.labels(node.name).set(0)
            return
        self.metrics.login_ok.labels(node.name).set(1)
        self.metrics.status(node, await session.status(node), self.clock())
        try:
            readings = await session.telemetry(node)
        except NoReplyError:
            # Status says it's up; telemetry is extra, and not every node sends it.
            log.info("%s sent no telemetry", node.name)
        else:
            self.metrics.readings(node, readings)

    def _missed(self, node: Node) -> None:
        misses = self._misses[node.name] = self._misses[node.name] + 1
        self.metrics.missed_polls.labels(node.name).set(misses)
        self.metrics.up.labels(node.name).set(0 if misses >= self.down_after_misses else 1)
