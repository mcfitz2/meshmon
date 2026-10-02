# 0001: Config reload

Status: accepted (pending hardware confirmation, see "Unverified")
Date: 2026-10-01
Issue: https://github.com/mcfitz2/meshmon/issues/11

## Context

meshmon reads `config.json` once at startup (`src/meshmon/main.py:51-53`) and
`run` builds the `Poller` once from `settings.nodes`. The README tells users to
"Restart the plugin after editing it" (`README.md:100-101`). Per-node metric
series are never removed (only telemetry readings are), so a live reload would
also have to clean up series and miss counters for removed nodes.

openHop is the supervisor. All citations below are to
`openhop-dev/openhop_repeater` at `main`, commit
`13eb8b2ea8b1cdb4a07ed6e282dc99e3aa8a5a8b`, read via the GitHub API.

### Q1. What happens when a plugin's config is saved?

The plugin is restarted, not signalled for reload, and the restart is opt-in
per save.

- `PluginManager.set_config(..., restart: bool = False)` writes
  `data/config.json`, then, if `restart` is true and the plugin is enabled and
  has a runtime, calls `self.runtime.restart(plugin_id)`
  (`repeater/plugins/manager.py:397-433`). The response carries
  `"restarted": true|false` (`manager.py:435-436`).
- `restart()` is `stop()` then `start()` (`repeater/plugins/runtime.py:717-719`).
  `stop()` goes through `_terminate_handle`: SIGTERM to the plugin's process
  group, wait up to `STOP_TIMEOUT_SECONDS = 5.0`, then SIGKILL
  (`runtime.py:25`, `697-715`).
- The HTTP endpoint `POST /api/plugins/settings` reads
  `restart = bool(body.get("restart", False))`, so the API default is no
  restart (`repeater/web/plugin_endpoints.py:428-455`). `docs/plugins.md`
  documents the same `"restart": true` field (section "Plugin configuration").
- The Plugins page Settings dialog has a "Restart plugin after save" checkbox
  initialised to `!!e.has_runtime`, so it is checked by default for service
  plugins (`repeater/web/html/assets/Plugins-C1Q8YF-o.js`, minified: the
  `ur=i(!0)` ref and `ur.value=!!e.has_runtime`). The toast says "Saved config
  for <id> and restarted" when `restarted` is true. This is a compiled bundle,
  so it can change between builds.
- No SIGHUP or other reload signal is sent anywhere in the manager. The only
  signals sent by the runtime's stop path are SIGTERM and SIGKILL
  (`runtime.py:698`, `711`, `726`). Nothing watches `config.json` either.

### Q2. Does openHop restart a plugin that exits?

Yes, with no back-off, only a crash-loop limit.

- A supervisor thread ticks every 0.5 s (`runtime.py:770-775`) and runs
  `_check_crashes` (`runtime.py:777-822`). An enabled plugin that exited
  without a stop in progress is restarted immediately via `self.start`
  (`runtime.py:816-822`), with a warning "exited unexpectedly".
- Crash-loop limit: 5 unexpected exits within 60 s marks the plugin `FAILED`
  and it is not restarted again (`runtime.py:30-31`, `800-812`). Documented as
  "restarts with a simple crash-loop limit: 5 unexpected exits in 60s ->
  `FAILED`" (`docs/plugins.md:109-110`).
- Consequence for meshmon (issue #2): exiting non-zero on a fatal condition is
  a valid recovery strategy, but a fast-failing exit (for example a port bind
  failure) is retried every ~0.5 s and ends in `FAILED` after 5 tries.
  `FAILED` needs a manual restart from the UI or API.

## Decision

(a) Not needed. meshmon does not reload its own config.

openHop already restarts the plugin when settings are saved from the Plugins
page, and that restart is the default in the UI. A reload inside meshmon would
duplicate it and would add the removed-node cleanup problem below. The cases it
would help are editing `config.json` by hand on disk, or saving through the API
without `"restart": true`. Both are covered by "restart the plugin".

Follow-up, not part of this spike: reword `README.md:100-101` to say that
saving from the Plugins page restarts the plugin by default (the checkbox can be
unticked, and API callers must pass `"restart": true`), and that hand edits
still need a restart.

## Consequences

- No code change. Restart-on-save means every config change, including
  `listen_port` and `listen_host`, takes effect without special handling.
- A restart drops in-memory state: the miss counters and all series. Prometheus
  sees a counter reset and a short scrape gap (up to one polling round plus the
  5 s stop timeout). This is acceptable for a health exporter.
- If this is revisited, option (b) SIGHUP is not viable (openHop never sends
  it). A build for (c), an mtime check per round, would need to handle:
  - removed nodes: `Gauge.remove` for every per-node gauge in
    `src/meshmon/metrics.py`, plus dropping their `Poller._misses` entries
    (`src/meshmon/poller.py:23-27`);
  - a changed `listen_port` or `listen_host`: requires a restart anyway;
  - invalid edits: keep the old config and log the error;
  - ordering after fixes #2 and #7, which change `main.run` and config
    validation.

## Unverified

- Real-hardware behaviour is not tested: that the Plugins page checkbox really
  restarts meshmon on a device, and how long the restart takes. Deferred.
- The Plugins page default comes from a minified bundle. It was read by
  pattern, not from the frontend source.
- Whether meshmon exits within the 5 s SIGTERM window (otherwise it is
  SIGKILLed). Not tested; `asyncio.run` with the default SIGTERM handling
  should terminate promptly, but this is UNVERIFIED.
- Container plugins use `repeater/plugins/container_supervisor.py`, which was
  not read. This record covers native service plugins only.
