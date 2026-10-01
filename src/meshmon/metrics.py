"""The Prometheus metrics meshmon serves, one series per node."""

from prometheus_client import CollectorRegistry, Gauge

from meshmon.config import Node
from meshmon.link import Reading, Status

_NODE = ["node"]


class Metrics:
    def __init__(self, registry: CollectorRegistry) -> None:
        def gauge(name: str, documentation: str, labels: list[str] = _NODE) -> Gauge:
            return Gauge(f"meshcore_node_{name}", documentation, labels, registry=registry)

        self.up = gauge("up", "1 unless the node has missed enough polls in a row")
        self.missed_polls = gauge("missed_polls", "Polls missed in a row")
        self.login_ok = gauge("login_ok", "1 if the node took meshmon's login")
        self.last_seen = gauge(
            "last_seen_timestamp_seconds", "When the node last answered with its status"
        )
        self._status = {
            "battery_volts": gauge("battery_volts", "Battery voltage"),
            "uptime_seconds": gauge("uptime_seconds", "Time since the node started"),
            "noise_floor_dbm": gauge("noise_floor_dbm", "Noise floor the node hears"),
            "last_rssi_dbm": gauge("last_rssi_dbm", "RSSI of the last packet the node heard"),
            "last_snr_db": gauge("last_snr_db", "SNR of the last packet the node heard"),
            "tx_queue_length": gauge("tx_queue_length", "Packets waiting to be sent"),
            # Counts the node keeps; they restart from 0 when it does.
            "packets_received": gauge("packets_received", "Packets received since it started"),
            "packets_sent": gauge("packets_sent", "Packets sent since it started"),
            "tx_airtime_seconds": gauge("tx_airtime_seconds", "Time spent sending"),
            "rx_airtime_seconds": gauge("rx_airtime_seconds", "Time spent receiving"),
            "receive_errors": gauge("receive_errors", "Packets received with errors"),
        }
        self.telemetry = gauge(
            "telemetry", "A Cayenne LPP telemetry reading", ["node", "channel", "type"]
        )
        self._readings: dict[str, set[tuple[str, str]]] = {}

    def status(self, node: Node, status: Status, now: float) -> None:
        values = {
            "battery_volts": status.battery_mv / 1000,
            "uptime_seconds": status.uptime_s,
            "noise_floor_dbm": status.noise_floor_dbm,
            "last_rssi_dbm": status.last_rssi_dbm,
            "last_snr_db": status.last_snr_db,
            "tx_queue_length": status.tx_queue_length,
            "packets_received": status.packets_received,
            "packets_sent": status.packets_sent,
            "tx_airtime_seconds": status.tx_airtime_s,
            "rx_airtime_seconds": status.rx_airtime_s,
            "receive_errors": status.receive_errors,
        }
        for name, value in values.items():
            self._status[name].labels(node.name).set(value)
        self.last_seen.labels(node.name).set(now)

    def readings(self, node: Node, readings: tuple[Reading, ...]) -> None:
        """Set the node's telemetry, dropping readings it no longer reports."""
        current = {(str(reading.channel), reading.type) for reading in readings}
        for channel, kind in self._readings.get(node.name, set()) - current:
            self.telemetry.remove(node.name, channel, kind)
        self._readings[node.name] = current
        for reading in readings:
            self.telemetry.labels(node.name, str(reading.channel), reading.type).set(reading.value)
