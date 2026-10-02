# meshmon

An [openHop](https://github.com/openhop-dev/openhop_repeater) plugin that polls
your MeshCore repeaters and room servers, and serves what they say as Prometheus
metrics: whether each is up, its battery voltage, and the rest of its status and
telemetry.

Every `interval_seconds` (15 minutes by default) meshmon connects to an openHop
companion and asks each node in turn: it logs in, requests the node's status,
then its telemetry. A node that answers is up. One that misses
`down_after_misses` polls in a row (2 by default) is down.

## Metrics

All are labelled `node`, the name you gave it in `config.json`.

| Metric | What |
| --- | --- |
| `meshcore_node_up` | 1 unless the node has missed `down_after_misses` polls in a row |
| `meshcore_node_missed_polls` | Polls missed in a row |
| `meshcore_node_login_ok` | 0 if the node refused the login (wrong password) |
| `meshcore_node_last_seen_timestamp_seconds` | When it last answered with its status |
| `meshcore_node_battery_volts` | Battery voltage |
| `meshcore_node_uptime_seconds` | Time since it started |
| `meshcore_node_noise_floor_dbm`, `_last_rssi_dbm`, `_last_snr_db` | Radio conditions it reports |
| `meshcore_node_tx_queue_length` | Packets waiting to be sent |
| `meshcore_node_packets_received`, `_packets_sent`, `_receive_errors` | Counts since it started |
| `meshcore_node_tx_airtime_seconds`, `_rx_airtime_seconds` | Airtime since it started |
| `meshcore_node_telemetry{channel, type}` | Each Cayenne LPP telemetry reading, such as `type="voltage"` |
| `meshmon_companion_up` | 0 if meshmon couldn't reach the companion, or its last round failed |

A node that stops answering keeps its last status values. Compare
`meshcore_node_last_seen_timestamp_seconds` with `time()` to see how old they
are. If the companion can't be reached, no misses are counted. Alert on
`meshmon_companion_up` for that.

## Setting up

### 1. Give meshmon a companion in openHop

meshmon talks to the mesh through an openHop companion, so it needs no admin
token. A companion takes one TCP client at a time, so give meshmon its own.
Generate an identity key:

```sh
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

Then add a companion under `identities:` in `/etc/openhop_repeater/config.yaml`
and restart openHop (`sudo systemctl restart openhop-repeater`):

```yaml
identities:
  companions:
    - name: "meshmon"
      identity_key: "<the key you generated>"
      settings:
        node_name: "meshmon"
        tcp_port: 5000
        bind_address: "127.0.0.1"
```

`repeater.mode` may be `forward` or `monitor`. In both, companions can
transmit. In `no_tx` they can't, and every node will look down.

### 2. Install the plugin

Download the wheel from the
[latest release](https://github.com/mcfitz2/meshmon/releases/latest) (or build
it with `uv build`), then install it from openHop's Plugins page, or through
its API:

```sh
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -F wheel=@meshmon-0.1.1-py3-none-any.whl \
  http://<pi>:8000/api/plugins/install
```

### 3. List your nodes

openHop seeds `config.json` in the plugin's data directory
(`$OPENHOP_PLUGIN_DATA`). Add your nodes to it. `password` is the node's guest
password, which is enough for status and telemetry. Leave it out where the node
lets a blank password in.

```json
{
  "companion_host": "127.0.0.1",
  "companion_port": 5000,
  "listen_host": "0.0.0.0",
  "listen_port": 9110,
  "interval_seconds": 900,
  "down_after_misses": 2,
  "nodes": [
    {"name": "Hilltop Repeater", "public_key": "<64 hex digits>", "password": "<guest password>"}
  ]
}
```

The file holds passwords, so keep it private: `chmod 600 config.json`. meshmon
warns if it isn't. Restart the plugin after editing it.

To run meshmon by hand, give it the path: `meshmon config.json`.

### 4. Scrape it with Alloy

```alloy
prometheus.scrape "meshmon" {
  targets         = [{"__address__" = "<pi-address>:9110"}]
  scrape_interval = "60s"
  forward_to      = [prometheus.remote_write.mimir.receiver]
}
```

Here `prometheus.remote_write.mimir` is your existing Mimir write. An alert for
a node that's down:

```yaml
- alert: MeshcoreNodeDown
  expr: meshcore_node_up == 0
  for: 5m
```

## Developing

```sh
uv sync
uv run pytest
uv run ruff check src tests && uv run mypy src tests
```

To release, set the same version in `pyproject.toml` and
`plugin/openhop-plugin.json`, run `uv lock`, commit, then tag and push:

```sh
git tag v0.1.2 && git push origin v0.1.2
```

The Release workflow runs CI, checks that the wheel carries the manifest and
that both versions match the tag, then publishes the wheel to a GitHub release.

The poller is tested against a fake session, and `meshmon.link` against a fake
MeshCore. Check changes to `meshmon.link` against a real companion too.
