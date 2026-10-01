"""Talking to nodes through an openHop companion with ``meshcore_py``.

``MeshcoreSession`` is not unit tested; the poller's tests use a fake Session.
Check it against the real companion.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from meshcore import EventType, MeshCore
from meshcore.packets import BinaryReqType

from meshmon.config import Node


class LinkDownError(Exception):
    """The companion can't be reached, or dropped the connection."""


class NoReplyError(Exception):
    """The node didn't answer in time."""


@dataclass(frozen=True)
class Status:
    battery_mv: int
    tx_queue_length: int
    noise_floor_dbm: int
    last_rssi_dbm: int
    last_snr_db: float
    packets_received: int
    packets_sent: int
    tx_airtime_s: int
    rx_airtime_s: int
    uptime_s: int
    receive_errors: int


@dataclass(frozen=True)
class Reading:
    """One Cayenne LPP telemetry value, such as channel 1's voltage."""

    channel: int
    type: str
    value: float


class Session(Protocol):
    async def login(self, node: Node) -> bool:
        """Log in with the node's password; False if it refuses."""
        ...

    async def status(self, node: Node) -> Status: ...

    async def telemetry(self, node: Node) -> tuple[Reading, ...]: ...


_PREFIX_HEX = 12
"""Replies name their sender by the first 6 bytes of its public key."""
_REPEATER = 2
"""``ADV_TYPE_REPEATER``; a room server logs in and answers the same way."""

Reply = TypeVar("Reply")


def _not_the_login_nag(record: logging.LogRecord) -> bool:
    # meshcore_py warns on every send_login, but send_login_sync waits for
    # LOGIN_SUCCESS from any node and can't tell a refused login from silence,
    # so meshmon keeps send_login and waits for the node's answer itself.
    return "send_login_sync" not in record.getMessage()


logging.getLogger("meshcore").addFilter(_not_the_login_nag)


@asynccontextmanager
async def connect(host: str, port: int) -> AsyncIterator["MeshcoreSession"]:
    """A session on the companion at ``host:port``, closed on leaving. openHop drops
    idle companion clients, so connect for each round of polls."""
    try:
        mc = await MeshCore.create_tcp(host, port)
    except (ConnectionError, OSError) as error:
        raise LinkDownError(f"can't reach the companion at {host}:{port}: {error}") from error
    if mc is None:
        raise LinkDownError(f"the companion at {host}:{port} did not answer")
    try:
        session = MeshcoreSession(mc)
        mc.subscribe(EventType.DISCONNECTED, lambda _event: session.disconnected.set())
        await mc.ensure_contacts()
        await mc.start_auto_message_fetching()
        yield session
    finally:
        await mc.disconnect()


class MeshcoreSession:
    def __init__(self, mc: Any) -> None:
        self._mc = mc
        self._lock = asyncio.Lock()
        self.disconnected = asyncio.Event()

    async def login(self, node: Node) -> bool:
        contact = await self._contact(node)
        return await self._ask(
            node,
            {EventType.LOGIN_SUCCESS: lambda _event: True, EventType.LOGIN_FAILED: lambda _: False},
            lambda mc: mc.commands.send_login(contact, node.password),
        )

    async def status(self, node: Node) -> Status:
        def status(event: Any) -> Status:
            payload = event.payload
            return Status(
                battery_mv=payload["bat"],
                tx_queue_length=payload["tx_queue_len"],
                noise_floor_dbm=payload["noise_floor"],
                last_rssi_dbm=payload["last_rssi"],
                last_snr_db=payload["last_snr"],
                packets_received=payload["nb_recv"],
                packets_sent=payload["nb_sent"],
                tx_airtime_s=payload["airtime"],
                rx_airtime_s=payload["rx_airtime"],
                uptime_s=payload["uptime"],
                receive_errors=payload["recv_errors"],
            )

        return await self._request(node, BinaryReqType.STATUS, EventType.STATUS_RESPONSE, status)

    async def telemetry(self, node: Node) -> tuple[Reading, ...]:
        def readings(event: Any) -> tuple[Reading, ...]:
            return tuple(
                Reading(int(item["channel"]), str(item["type"]), float(item["value"]))
                for item in event.payload["lpp"]
                # Only scalar readings; a GPS fix or an accelerometer is several.
                if isinstance(item["value"], int | float)
            )

        return await self._request(
            node, BinaryReqType.TELEMETRY, EventType.TELEMETRY_RESPONSE, readings
        )

    async def _contact(self, node: Node) -> dict[str, Any]:
        """The companion's contact for ``node``, added with a flood path if it has none."""
        known: dict[str, Any] | None = self._mc.get_contact_by_key_prefix(node.public_key)
        if known is not None:
            return known
        raw = {
            "public_key": node.public_key,
            "type": _REPEATER,
            "flags": 0,
            "out_path_len": -1,
            "out_path": "",
            "out_path_hash_mode": 0,
            "adv_name": node.name,
            "last_advert": 0,
            "adv_lat": 0.0,
            "adv_lon": 0.0,
        }
        await self._command(lambda mc: mc.commands.add_contact(raw))
        self._mc.contacts[node.public_key] = raw
        return raw

    async def _command(self, call: Callable[[Any], Awaitable[Any]]) -> Any:
        """Run ``call(mc)``, one companion command at a time: meshcore_py matches a
        command to its answer by event type alone, so two in flight could swap
        answers."""
        if self.disconnected.is_set():
            raise LinkDownError("lost the companion")
        async with self._lock:
            result = await call(self._mc)
        if result.type == EventType.ERROR:
            raise LinkDownError(f"the companion refused a command: {result.payload}")
        return result

    async def _request(
        self,
        node: Node,
        request_type: BinaryReqType,
        answer: EventType,
        reply: Callable[[Any], Reply],
    ) -> Reply:
        contact = await self._contact(node)
        return await self._ask(
            node,
            {answer: reply},
            lambda mc: mc.commands.send_binary_req(
                contact, request_type, context={"pubkey_prefix_length": _PREFIX_HEX // 2}
            ),
        )

    async def _ask(
        self,
        node: Node,
        replies: dict[EventType, Callable[[Any], Reply]],
        send: Callable[[Any], Awaitable[Any]],
    ) -> Reply:
        """Send with ``send(mc)`` and wait for the first of ``replies`` (event type to
        reply) from ``node``, as long as the companion suggests."""
        reply: asyncio.Future[Reply] = asyncio.get_running_loop().create_future()
        subscriptions = []
        for event_type, answer in replies.items():

            def on_reply(event: Any, answer: Callable[[Any], Reply] = answer) -> None:
                if not reply.done():
                    reply.set_result(answer(event))

            subscriptions.append(
                self._mc.subscribe(
                    event_type, on_reply, {"pubkey_prefix": node.public_key[:_PREFIX_HEX]}
                )
            )
        try:
            sent = await self._command(send)
            timeout = sent.payload["suggested_timeout"] / 1000
            dropped = asyncio.ensure_future(self.disconnected.wait())
            either: set[asyncio.Future[Any]] = {reply, dropped}
            try:
                await asyncio.wait(either, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            finally:
                dropped.cancel()
            if reply.done():
                return reply.result()
            if self.disconnected.is_set():
                raise LinkDownError("lost the companion while waiting for a reply")
            raise NoReplyError(f"{node.name} did not answer within {timeout:.0f} s")
        finally:
            reply.cancel()
            for subscription in subscriptions:
                subscription.unsubscribe()
