import asyncio
import logging
from dataclasses import replace

import pytest
from prometheus_client import CollectorRegistry

from meshmon.config import Node
from meshmon.link import BadReplyError, LinkDownError, NoReplyError, Reading, Status
from meshmon.metrics import Metrics
from meshmon.poller import Poller

REPEATER = Node("Hilltop Repeater", "0123456789abcdef" * 4)
ROOM = Node("Library Room", "fedcba9876543210" * 4, password="guest")

STATUS = Status(
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


class FakeSession:
    """Answers as each node is set up to: a status, telemetry, or nothing."""

    def __init__(self) -> None:
        self.statuses: dict[str, Status] = {}
        self.readings: dict[str, tuple[Reading, ...]] = {}
        self.refuse_login: set[str] = set()
        self.link_down: set[str] = set()
        self.broken: set[str] = set()
        self.garbled: set[str] = set()
        self.garbled_telemetry: set[str] = set()
        self.logins: list[tuple[str, str]] = []

    async def login(self, node: Node) -> bool:
        if node.public_key in self.link_down:
            raise LinkDownError(node.name)
        if node.public_key not in self.statuses:
            raise NoReplyError(node.name)
        self.logins.append((node.name, node.password))
        return node.public_key not in self.refuse_login

    async def status(self, node: Node) -> Status:
        if node.public_key in self.broken:
            raise RuntimeError("bad payload")
        if node.public_key in self.garbled:
            raise BadReplyError(f"{node.name} sent a reply meshmon can't read")
        if node.public_key not in self.statuses:
            raise NoReplyError(node.name)
        return self.statuses[node.public_key]

    async def telemetry(self, node: Node) -> tuple[Reading, ...]:
        if node.public_key in self.garbled_telemetry:
            raise BadReplyError(f"{node.name} sent telemetry meshmon can't read")
        if node.public_key not in self.readings:
            raise NoReplyError(node.name)
        return self.readings[node.public_key]


class Station:
    def __init__(self, *nodes: Node, down_after_misses: int = 2) -> None:
        self.registry = CollectorRegistry()
        self.now = 1_700_000_000.0
        self.poller = Poller(
            nodes, down_after_misses, Metrics(self.registry), clock=lambda: self.now
        )

    def poll(self, session: FakeSession) -> None:
        asyncio.run(self.poller.poll(session))

    def value(self, name: str, node: Node, **labels: str) -> float | None:
        return self.registry.get_sample_value(name, {"node": node.name, **labels})


def test_a_node_that_answers_is_up_with_its_status() -> None:
    station = Station(REPEATER)
    session = FakeSession()
    session.statuses[REPEATER.public_key] = STATUS

    station.poll(session)

    assert station.value("meshcore_node_up", REPEATER) == 1
    assert station.value("meshcore_node_battery_volts", REPEATER) == 4.012
    assert station.value("meshcore_node_uptime_seconds", REPEATER) == 86_400
    assert station.value("meshcore_node_noise_floor_dbm", REPEATER) == -112
    assert station.value("meshcore_node_last_snr_db", REPEATER) == 6.25
    assert station.value("meshcore_node_packets_received", REPEATER) == 1500
    assert station.value("meshcore_node_last_seen_timestamp_seconds", REPEATER) == 1_700_000_000


def test_it_logs_in_with_the_nodes_password_before_asking() -> None:
    station = Station(ROOM)
    session = FakeSession()
    session.statuses[ROOM.public_key] = STATUS

    station.poll(session)

    assert session.logins == [("Library Room", "guest")]


def test_telemetry_readings_are_labelled_by_channel_and_type() -> None:
    station = Station(REPEATER)
    session = FakeSession()
    session.statuses[REPEATER.public_key] = STATUS
    session.readings[REPEATER.public_key] = (
        Reading(1, "voltage", 4.01),
        Reading(2, "temperature", 21.5),
    )

    station.poll(session)

    assert station.value("meshcore_node_telemetry", REPEATER, channel="1", type="voltage") == 4.01
    assert (
        station.value("meshcore_node_telemetry", REPEATER, channel="2", type="temperature") == 21.5
    )


def test_a_node_without_telemetry_is_still_up() -> None:
    station = Station(REPEATER)
    session = FakeSession()
    session.statuses[REPEATER.public_key] = STATUS

    station.poll(session)

    assert station.value("meshcore_node_up", REPEATER) == 1


def test_a_node_is_down_only_after_missing_enough_polls_in_a_row() -> None:
    station = Station(REPEATER, down_after_misses=2)
    session = FakeSession()
    session.statuses[REPEATER.public_key] = STATUS
    station.poll(session)
    silent = FakeSession()

    station.poll(silent)
    assert station.value("meshcore_node_up", REPEATER) == 1
    assert station.value("meshcore_node_missed_polls", REPEATER) == 1

    station.poll(silent)
    assert station.value("meshcore_node_up", REPEATER) == 0
    # Its last answer is still there, and says when it was.
    assert station.value("meshcore_node_battery_volts", REPEATER) == 4.012
    assert station.value("meshcore_node_last_seen_timestamp_seconds", REPEATER) == 1_700_000_000


def test_answering_again_brings_a_node_back_up() -> None:
    station = Station(REPEATER, down_after_misses=1)
    silent = FakeSession()
    station.poll(silent)
    assert station.value("meshcore_node_up", REPEATER) == 0

    session = FakeSession()
    session.statuses[REPEATER.public_key] = replace(STATUS, battery_mv=3900)
    station.now += 900
    station.poll(session)

    assert station.value("meshcore_node_up", REPEATER) == 1
    assert station.value("meshcore_node_missed_polls", REPEATER) == 0
    assert station.value("meshcore_node_battery_volts", REPEATER) == 3.9
    assert station.value("meshcore_node_last_seen_timestamp_seconds", REPEATER) == 1_700_000_900


def test_a_node_that_refuses_the_login_is_up_but_reports_nothing() -> None:
    station = Station(REPEATER)
    session = FakeSession()
    session.statuses[REPEATER.public_key] = STATUS
    session.refuse_login.add(REPEATER.public_key)

    station.poll(session)

    assert station.value("meshcore_node_up", REPEATER) == 1
    assert station.value("meshcore_node_login_ok", REPEATER) == 0
    assert station.value("meshcore_node_battery_volts", REPEATER) is None


def test_one_silent_node_does_not_stop_the_others_being_polled() -> None:
    station = Station(REPEATER, ROOM, down_after_misses=1)
    session = FakeSession()
    session.statuses[ROOM.public_key] = STATUS

    station.poll(session)

    assert station.value("meshcore_node_up", REPEATER) == 0
    assert station.value("meshcore_node_up", ROOM) == 1


def test_losing_the_companion_ends_the_round_without_counting_misses() -> None:
    station = Station(REPEATER, ROOM, down_after_misses=1)
    session = FakeSession()
    session.link_down.add(REPEATER.public_key)
    session.statuses[ROOM.public_key] = STATUS

    with pytest.raises(LinkDownError):
        station.poll(session)

    for node in (REPEATER, ROOM):
        assert station.value("meshcore_node_up", node) is None
        assert station.value("meshcore_node_missed_polls", node) is None


def test_a_reading_the_node_stops_sending_is_dropped() -> None:
    station = Station(REPEATER)
    session = FakeSession()
    session.statuses[REPEATER.public_key] = STATUS
    session.readings[REPEATER.public_key] = (
        Reading(1, "voltage", 4.01),
        Reading(2, "temperature", 21.5),
    )
    station.poll(session)
    session.readings[REPEATER.public_key] = (Reading(1, "voltage", 4.02),)

    station.poll(session)

    assert station.value("meshcore_node_telemetry", REPEATER, channel="1", type="voltage") == 4.02
    assert (
        station.value("meshcore_node_telemetry", REPEATER, channel="2", type="temperature") is None
    )


def test_an_unexpected_error_counts_as_a_miss_and_the_round_goes_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    station = Station(REPEATER, ROOM, down_after_misses=1)
    session = FakeSession()
    session.statuses[REPEATER.public_key] = STATUS
    session.statuses[ROOM.public_key] = STATUS
    session.broken.add(REPEATER.public_key)

    with caplog.at_level(logging.ERROR):
        station.poll(session)

    assert station.value("meshcore_node_up", REPEATER) == 0
    assert station.value("meshcore_node_missed_polls", REPEATER) == 1
    assert station.value("meshcore_node_up", ROOM) == 1
    assert "polling Hilltop Repeater failed" in caplog.text


def test_a_node_whose_reply_cant_be_read_is_up_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    station = Station(REPEATER)
    session = FakeSession()
    session.statuses[REPEATER.public_key] = STATUS
    session.garbled.add(REPEATER.public_key)

    with caplog.at_level(logging.ERROR):
        station.poll(session)

    assert station.value("meshcore_node_up", REPEATER) == 1
    assert station.value("meshcore_node_missed_polls", REPEATER) == 0
    assert station.value("meshcore_node_battery_volts", REPEATER) is None
    assert [r for r in caplog.records if r.levelno == logging.ERROR]
    assert "Hilltop Repeater sent a reply meshmon can't read" in caplog.text


def test_unreadable_telemetry_does_not_fail_the_node(
    caplog: pytest.LogCaptureFixture,
) -> None:
    station = Station(REPEATER)
    session = FakeSession()
    session.statuses[REPEATER.public_key] = STATUS
    session.garbled_telemetry.add(REPEATER.public_key)

    with caplog.at_level(logging.ERROR):
        station.poll(session)

    assert station.value("meshcore_node_up", REPEATER) == 1
    assert station.value("meshcore_node_missed_polls", REPEATER) == 0
    assert station.value("meshcore_node_battery_volts", REPEATER) == 4.012
    assert "can't read" in caplog.text
