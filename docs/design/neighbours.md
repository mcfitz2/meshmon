# Design: exporting neighbour links

Status: proposal (issue #9). No code changed.

Firmware citations are to `meshcore-dev/MeshCore` at `main` commit
`a366955cb2f67b8e6842d4f00d2b6a554dddd88a`, read 2026-10-01. Line numbers are for
that commit and will drift. Library citations are to meshcore_py 2.3.14 as
locked in this repo. Anything that only real hardware can settle is marked
"UNVERIFIED — needs real hardware".

## Summary

Build it, opt-in per node, default off. The repeater firmware source shows no
permission check on the neighbours request, so a guest login should get an
answer and the README's "guest password is enough" promise holds. Only
repeaters answer; room servers do not, so the option only makes sense on
repeater nodes. The cost is one extra request per opted-in node per round, plus
extra pages if a node has more than 14 neighbours (at most 4 pages with the
usual table size of 50). Series are bounded by the firmware's neighbour table,
and a 4-byte prefix is a usable identity once mapped back to configured node
names (6 bytes is safer and matches meshmon's existing prefix). Opt-in keeps
the default airtime and series cost unchanged. All firmware behaviour here is
read from source only; none has been tried on a radio.

## Answers

### 1. Permission: guests can ask

`MyMesh::handleRequest` in the repeater (`examples/simple_repeater/MyMesh.cpp:211`)
checks permissions per request type:

- `REQ_TYPE_GET_STATUS` carries the comment "guests can also access this now" (line 216).
- `REQ_TYPE_GET_TELEMETRY_DATA` downgrades guests to base telemetry (lines 240-250).
- `REQ_TYPE_GET_ACCESS_LIST` requires `sender->isAdmin()` (line 262).
- `REQ_TYPE_GET_NEIGHBOURS` (`#define` at line 50, handler at line 276) has **no
  permission check**. Any client in the ACL reaches it.

Login accepts the admin password as `PERM_ACL_ADMIN` and the guest password as
`PERM_ACL_GUEST` (lines 102-106). `handleRequest` is called from the
`PAYLOAD_TYPE_REQ` path for any known client (lines 672-678). So a guest
session gets neighbours, and there is no conflict with the README.

UNVERIFIED — needs real hardware: that a guest login really receives the reply
on deployed firmware. The `request_version` byte (payload[1], must be 0, line
277) suggests the request was added after the others, so older firmware may not
answer (see Risks).

### 2. Room servers: no

The room server's `handleRequest` (`examples/simple_room_server/MyMesh.cpp:151`)
handles only `REQ_TYPE_GET_STATUS` (157), `REQ_TYPE_GET_TELEMETRY_DATA` (181)
and the admin-only `REQ_TYPE_GET_ACCESS_LIST` (202). Its `#define` list
(lines 15-18) has no `REQ_TYPE_GET_NEIGHBOURS`. Anything else reaches
`return 0; // unknown command` (line 216), and the caller sends a reply only
when `reply_len > 0` (line 579). A neighbours request to a room server will
therefore time out as `NoReplyError`.

Neighbours are a repeater feature. `putNeighbour` is called from repeater
advert handling, which keeps only `ADV_TYPE_REPEATER` entries heard with zero
hops (repeater `MyMesh.cpp:654-659`), and from discovery responses (line 841).
A repeater's list therefore holds repeaters it hears directly, not companions
or rooms.

The list is compiled in only when `MAX_NEIGHBOURS` is non-zero
(`#if MAX_NEIGHBOURS`, lines 64 and 301). Variants set it to 50
(e.g. `variants/heltec_v3/platformio.ini:49`). UNVERIFIED — needs real
hardware: the value on the user's actual nodes. If unset, the reply reports
zero neighbours.

### 3. Size and airtime

Request payload (`meshcore/commands/binary.py:136-161`): `0x00` version,
count(1), offset(2), order_by(1), pubkey_prefix_length(1), random tag(4), 11
bytes, sent via `send_binary_req(..., data=req, context={"pubkey_prefix_length": n})`.
The firmware reads these at repeater `MyMesh.cpp:277-291`.

Reply (firmware lines 338-370; parser `meshcore/reader.py:891-919`): 4-byte tag,
2-byte neighbours_count, 2-byte results_count, then
`N * (prefix + 4 secs_ago + 1 snr)` entries. SNR is the raw int8 of `snr*4`
(repeater `MyMesh.h:66-71`; the reader divides by 4, so 0.25 dB steps).

The firmware's results buffer is `uint8_t results_buffer[130]` (line 341) and it
stops adding entries when the next would overflow (lines 344-350). With a
4-byte prefix an entry is 9 bytes, so `floor(130/9) = 14` entries per reply
(126 bytes of entries, 134 with the counters, 138 with the tag). The packet
payload limit is `MAX_PACKET_PAYLOAD = 184` (`src/MeshCore.h:20`), so a reply
is always one frame. With `MAX_NEIGHBOURS=50`, a full table needs 4 pages
(14+14+14+8). Paging uses `offset`; the reply's `neighbours_count` is the
total, so a client stops when `offset + results_count >= neighbours_count`
(`fetch_all_neighbours`, `binary.py:199+`). The firmware sorts per request and
the table can change between pages, so pages are not a consistent snapshot
(an entry may repeat or be skipped). `order_by` is 0 newest first, 1 oldest
first, 2 strongest first, 3 weakest first (line 287).

Airtime: one request and one reply per page, the same shape as the status
request meshmon already sends, with a larger reply. A node with 14 or fewer
neighbours costs one extra request/reply pair per round.

### 4. Identity

A 4-byte prefix is 32 bits. For n repeaters the chance of any collision is
about n^2 / 2^33; for 300 repeaters that is roughly 1 in 95,000. The prefix of a
public key never changes, so it is stable, but uniqueness is not guaranteed.
The firmware allows up to `PUB_KEY_SIZE` (32) bytes (clamp at line 295;
`src/MeshCore.h:8`). meshmon can ask for 6 bytes, the same 12-hex-digit prefix
its `_ask` filter uses (`_PREFIX_HEX = 12`, `link.py:62`). A 6-byte entry is
11 bytes, so 11 entries fit per page (`floor(130/11)`) instead of 14, a small
airtime cost for a negligible collision risk. Recommendation: request 6 bytes
and match configured nodes with `public_key.startswith(prefix)`. If two
configured nodes share a prefix, label by hex rather than guess. Unconfigured
neighbours are labelled with their hex prefix.

So meshmon can name only neighbours it is also configured to monitor; other
repeaters appear as hex. That is acceptable and still informative.

## Proposed metrics

```
meshcore_node_neighbour_snr_db{node, neighbour}
meshcore_node_neighbour_heard_seconds_ago{node, neighbour}
meshcore_node_neighbours{node}      # neighbours_count: total in the node's table
```

`node` is the polled node's configured name. `neighbour` is the configured name
when the prefix matches a configured `public_key`, else the hex prefix. Naming
follows `meshcore_node_<thing>` (`src/meshmon/metrics.py:14`).

Cardinality: at most `MAX_NEIGHBOURS` (50 on the variants checked) times 2
series per opted-in node, typically far fewer. It is bounded by the firmware
table, not by anything meshmon controls. Even 20 nodes at 50 neighbours is
2,000 series.

Stale series: mirror `Metrics.readings` (`metrics.py:59-66`). Keep a set of
neighbour labels per node. After each successful reply, remove series for
labels no longer present, then set the current ones. On a missing reply keep
the last values (as telemetry does), so `heard_seconds_ago` is only as fresh as
the last successful poll; alerts should key off the existing last-seen metric.
The firmware evicts the least recently heard entry when the table is full
(`putNeighbour`, lines 63-86), so a marginal link can appear and disappear;
that churn is expected.

`snr_db` is what the polled node measured on the neighbour's adverts, so the
A-to-B and B-to-A series are different directions. That is useful, and the
README should say so.

## Config

Recommend per-node opt-in: `"neighbours": true` in a node entry, default
`false`. Room servers do not answer, so a global flag would make every such
node time out and burn airtime each round; the user knows which nodes are
repeaters; and airtime on a shared channel is worth protecting. A global
default can be added later if asked for. Parse in `config.py` next to
`password` (`Node` dataclass at `config.py:18-26`, parser at lines 43-48) with a
strict bool check and a `ConfigError` for a non-bool, like the other fields.

## Request flow

In `Poller._poll` (`src/meshmon/poller.py:43`), after telemetry:

```python
if node.neighbours:
    try:
        links = await session.neighbours(node)
    except NoReplyError:
        log.info("%s sent no neighbours", node.name)
    else:
        self.metrics.neighbours(node, links, ...)
```

Failures never fail the node: status already proved it is up, and a missing
reply (old firmware, a room server configured by mistake) must not mark it down
or count as a missed poll, the same as telemetry.

Paging happens inside `MeshcoreSession.neighbours`: loop with
`offset = len(collected)` until `len(collected) >= neighbours_count`, or a page
returns `results_count == 0` (the table shrank), or a page cap (say 4) is hit.
Each page is a separate `_ask` with its own timeout. If a later page times out,
keep what was collected rather than discarding it. Poll time grows with page
count, so count it against the poll interval.

The existing `_ask` filter on `pubkey_prefix` should match the reply, since the
event carries `pubkey_prefix` (`reader.py:915`). UNVERIFIED — needs real
hardware: that the filter matches in practice and that the companion's
`suggested_timeout` is long enough for the larger reply.

## Implementation outline

A follow-up build plan would touch:

- `src/meshmon/config.py`: add `neighbours: bool = False` to `Node`; parse it.
- `src/meshmon/link.py`: `_request` (lines 179-193) passes no `data`. Add an
  optional `data` argument forwarded to `send_binary_req(..., data=...)`, which
  accepts it (`binary.py:136-161`), building the 11-byte payload as
  `req_neighbours_async` does, with a fresh random tag per request. Add
  `neighbours(node) -> tuple[Neighbour, ...]` to the `Session` protocol and to
  `MeshcoreSession`, answering `EventType.NEIGHBOURS_RESPONSE`, with the paging
  loop and a `Neighbour` dataclass (`prefix`, `snr`, `secs_ago`).
- `src/meshmon/metrics.py`: the gauges plus a stale-series set, like
  `readings`. Prefix-to-name resolution is built from the config at startup and
  lives in the poller or metrics, not the session.
- `src/meshmon/poller.py`: the call above.
- `README.md`: the option, the repeater-only caveat, the new series.
- `tests/`, using the fake MeshCore in `tests/test_link.py` (issue #1): a reply with
  three neighbours sets the series; a later reply with one removes the stale
  ones; a two-page reply is stitched; `NoReplyError` leaves the node up and
  `missed_polls` unchanged; a prefix matching a configured node is labelled by
  name and a duplicate prefix by hex; a non-bool `neighbours` is a
  `ConfigError`; nodes without the option never send the request.

## Risks / unknowns

All UNVERIFIED — needs real hardware:

- A guest login receives the neighbours reply (source says yes).
- Firmware age: older repeaters may lack the request and will time out. The
  opt-in flag and an info-level log are the mitigation.
- `MAX_NEIGHBOURS` on the user's builds; 0 gives an empty list.
- Reliability of a reply of up to 138 bytes over a multi-hop path, and whether
  `suggested_timeout` suffices.
- Page consistency (Answer 3), and clock effects: `secs_ago` is computed from the
  repeater's RTC (`MyMesh.cpp:355`), so a wrong clock gives nonsense. The reader
  parses it as signed int32; clamp negatives to 0.
- SNR comes from the last zero-hop advert, not a continuous measurement. Adverts
  are infrequent, so `heard_seconds_ago` can be hours and the SNR is stale to
  match. Dashboards should show both.
