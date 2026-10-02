import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import pytest
from meshcore import EventType
from meshcore.events import Event, EventDispatcher
from meshcore.packets import BinaryReqType

from meshmon.config import Node
from meshmon.link import LinkDownError, MeshcoreSession, NoReplyError, Reading, Status

REPEATER = Node("Hilltop Repeater", "0123456789abcdef" * 4)
OTHER = Node("Library Room", "fedcba9876543210" * 4, password="guest")

T = TypeVar("T")


class FakeCommands:
    """Records what is sent and answers each command with MSG_SENT (or `result`),
    then dispatches whatever replies the test queued for that command."""

    def __init__(self, mc: "FakeMeshCore") -> None:
        self._mc = mc

    async def send_login(self, contact: dict[str, Any], password: str) -> Event:
        return await self._send("login", contact)

    async def send_binary_req(
        self, contact: dict[str, Any], request_type: BinaryReqType, context: dict[str, Any]
    ) -> Event:
        return await self._send(request_type.name, contact)

    async def add_contact(self, contact: dict[str, Any]) -> Event:
        return await self._send("add_contact", contact)

    async def _send(self, key: str, contact: dict[str, Any]) -> Event:
        mc = self._mc
        mc.sent.append((key, contact["public_key"]))
        result = mc.result or Event(
            EventType.MSG_SENT,
            {"suggested_timeout": mc.suggested_timeout_ms, "expected_ack": b"\x00\x00\x00\x01"},
        )
        for event in mc.replies.get(key, []):
            await mc.dispatcher.dispatch(event)
        return result


class FakeMeshCore:
    def __init__(self) -> None:
        self.dispatcher = EventDispatcher()
        self.contacts: dict[str, dict[str, Any]] = {}
        self.sent: list[tuple[str, str]] = []
        self.replies: dict[str, list[Event]] = {}
        self.result: Event | None = None
        self.suggested_timeout_ms = 50
        self.commands = FakeCommands(self)

    def subscribe(
        self,
        event_type: EventType | None,
        callback: Callable[[Event], Any],
        attribute_filters: dict[str, Any] | None = None,
    ) -> Any:
        return self.dispatcher.subscribe(event_type, callback, attribute_filters)

    def get_contact_by_key_prefix(self, prefix: str) -> dict[str, Any] | None:
        for key, contact in self.contacts.items():
            if key.startswith(prefix):
                return contact
        return None


def run(mc: FakeMeshCore, use: Callable[[MeshcoreSession], Awaitable[T]]) -> T:
    async def go() -> T:
        await mc.dispatcher.start()
        try:
            return await use(MeshcoreSession(mc))
        finally:
            await mc.dispatcher.stop()

    return asyncio.run(go())


def from_node(node: Node) -> dict[str, Any]:
    return {"pubkey_prefix": node.public_key[:12]}


def status_reply(node: Node) -> Event:
    payload = {
        "bat": 4012,
        "tx_queue_len": 0,
        "noise_floor": -112,
        "last_rssi": -87,
        "last_snr": 6.25,
        "nb_recv": 1500,
        "nb_sent": 700,
        "airtime": 120,
        "rx_airtime": 900,
        "uptime": 86_400,
        "recv_errors": 3,
    }
    return Event(EventType.STATUS_RESPONSE, payload, from_node(node))


def telemetry_reply(node: Node, lpp: list[dict[str, Any]]) -> Event:
    return Event(EventType.TELEMETRY_RESPONSE, {"lpp": lpp}, from_node(node))


def test_status_is_read_from_the_nodes_answer() -> None:
    mc = FakeMeshCore()
    mc.replies["STATUS"] = [status_reply(REPEATER)]

    status = run(mc, lambda session: session.status(REPEATER))

    assert status == Status(
        battery_mv=4012,
        tx_queue_length=0,
        noise_floor_dbm=-112,
        last_rssi_dbm=-87,
        last_snr_db=6.25,
        packets_received=1500,
        packets_sent=700,
        tx_airtime_s=120,
        rx_airtime_s=900,
        uptime_s=86_400,
        receive_errors=3,
    )


def test_an_answer_from_another_node_is_ignored() -> None:
    mc = FakeMeshCore()
    mc.replies["STATUS"] = [status_reply(OTHER)]

    with pytest.raises(NoReplyError):
        run(mc, lambda session: session.status(REPEATER))


def test_silence_is_no_reply_and_leaves_no_subscriptions() -> None:
    mc = FakeMeshCore()

    with pytest.raises(NoReplyError):
        run(mc, lambda session: session.status(REPEATER))

    assert mc.dispatcher.subscriptions == []


def test_a_refused_login_is_false() -> None:
    mc = FakeMeshCore()
    mc.replies["login"] = [Event(EventType.LOGIN_FAILED, {}, from_node(REPEATER))]

    assert run(mc, lambda session: session.login(REPEATER)) is False


def test_an_accepted_login_is_true() -> None:
    mc = FakeMeshCore()
    mc.replies["login"] = [Event(EventType.LOGIN_SUCCESS, {}, from_node(REPEATER))]

    assert run(mc, lambda session: session.login(REPEATER)) is True


def test_telemetry_keeps_only_scalar_readings() -> None:
    mc = FakeMeshCore()
    mc.replies["TELEMETRY"] = [
        telemetry_reply(
            REPEATER,
            [
                {"channel": 1, "type": "voltage", "value": 4.01},
                {"channel": 2, "type": "temperature", "value": 21},
                {"channel": 3, "type": "gps", "value": {"latitude": 1.0, "longitude": 2.0}},
            ],
        )
    ]

    readings = run(mc, lambda session: session.telemetry(REPEATER))

    assert readings == (Reading(1, "voltage", 4.01), Reading(2, "temperature", 21.0))


def test_an_unknown_node_is_added_to_the_companion_once() -> None:
    mc = FakeMeshCore()
    mc.replies["STATUS"] = [status_reply(REPEATER)]

    async def twice(session: MeshcoreSession) -> None:
        await session.status(REPEATER)
        await session.status(REPEATER)

    run(mc, twice)

    assert [key for key, _ in mc.sent].count("add_contact") == 1


def test_a_known_contact_is_not_added_again() -> None:
    mc = FakeMeshCore()
    mc.contacts[REPEATER.public_key] = {"public_key": REPEATER.public_key}
    mc.replies["STATUS"] = [status_reply(REPEATER)]

    run(mc, lambda session: session.status(REPEATER))

    assert "add_contact" not in [key for key, _ in mc.sent]


def test_losing_the_companion_while_waiting_is_link_down() -> None:
    mc = FakeMeshCore()
    mc.suggested_timeout_ms = 5000

    async def use(session: MeshcoreSession) -> None:
        asyncio.get_running_loop().call_later(0.05, session.disconnected.set)
        await session.status(REPEATER)

    started = time.monotonic()
    with pytest.raises(LinkDownError):
        run(mc, use)

    assert time.monotonic() - started < 1


def test_nothing_is_sent_once_the_companion_is_lost() -> None:
    mc = FakeMeshCore()

    async def use(session: MeshcoreSession) -> None:
        session.disconnected.set()
        await session.status(REPEATER)

    with pytest.raises(LinkDownError):
        run(mc, use)

    assert mc.sent == []


def test_a_command_the_companion_refuses_is_link_down() -> None:
    mc = FakeMeshCore()
    # Current behaviour; issue #5 makes firmware error codes per-node.
    mc.result = Event(EventType.ERROR, {"error_code": 3})

    with pytest.raises(LinkDownError):
        run(mc, lambda session: session.status(REPEATER))
