# reader-sync verification runner (#85 / S10)

Automated two-client verification for the dual-reader feature (#75). One
declarative scenario suite (`scenarios.json`), two interchangeable
drivers, deterministic assertions, reproducible evidence.

```
scripts/reader-sync/
├── verify.py                — CLI orchestrator (exit 0/1/2/3)
├── verify_core.py           — scenario schema, plan, result JSON, evidence,
│                              exit codes, redaction, marker palette (stdlib only)
├── verify_ops.py            — the op interpreter both drivers share
├── verify_drivers.py        — ChromeDriver (Playwright 2 contexts),
│                              SimulatorDriver (simctl + idb), BystanderPair
├── verify_fixture_server.py — fixture page + long-poll control channel
├── fixture.html             — runner-controlled client driving the REAL
│                              production modules + wire path
├── scenarios.json           — THE declarative suite (single source of truth)
├── ws_probe.py              — connectivity probe
└── README.md
```

## Usage

```bash
# Chrome driver — two BrowserContexts in one Playwright process
# (real two windows with --headed; storage is isolated per context)
uv run --with playwright --with websockets --with pillow \
    python scripts/reader-sync/verify.py \
    --driver chrome --url https://dev-03.<tailnet>.ts.net

# Simulator driver — two explicit-UDID iOS Simulators
uv run --with websockets \
    python scripts/reader-sync/verify.py --driver simulator \
    --leader-udid <UDID-A> --follower-udid <UDID-B> \
    --url http://127.0.0.1:8080

# Useful flags
--dev-env NAME      resolve URL via ./dev.sh url NAME
--scenario NAME     run only the named scenario (repeatable)
--surface ui        real pairing UI instead of the fixture (needs S09)
--headed            chrome: visible windows
--evidence-root DIR --run-id ID   evidence placement control
```

## Exit codes (spec — いずれの失敗も exit 0 にしない)

| code | meaning |
|------|---------|
| 0 | every scenario PASS |
| 1 | an assertion failed — result JSON names the scenario + assertion |
| 2 | environment unavailable (devices/idb/URL unreachable; run is recorded) |
| 3 | an unsupported scenario/op/surface was requested |

## Evidence

`.omo/evidence/reader-sync/<run-id>/`:

- `result.json` — run_id, driver, surface, route, build_sha (target
  `/api/version`), devices (per-surface kind + udid/context + UA + RTC
  support), os, capabilities, plan, per-scenario
  name/transport/status/assertions/rtt_ms, external_state
  before/after, self_check
- `scenarios.json` — the declarations used + the resolved plan
- `<scenario>/<transport>/*-{leader,follower}.png` — both-screen captures
  (the synthetic marker region is pixel-checked, not just screenshotted)
- `<scenario>/<transport>/trace-{leader,follower}.jsonl` — wire trace
  (frame types + timing only; **no codes, tokens, payloads, SDP/ICE**)

Pairing codes exist only in runner memory for the one JOIN — they are
never asserted on, logged, or written. Real folder names are excluded
structurally: only `fx:`-prefixed synthetic identifiers survive redaction.

## Architecture

**Two surfaces, one declaration.** `--surface fixture` (default) serves
`fixture.html` from a runner-side HTTP server; the page loads the REAL
`pairTransport.js`/`pairModel.js` from the target origin and speaks the
real `/ws/reader-sync` protocol — pairing (CREATE/JOIN/APPROVE), SIGNAL
relay, TRANSPORT_STATE, LEAVE — plus a runner command channel for
deterministic step ordering. `--surface ui` targets the production pairing
UI; until the S09 controller wiring (#84) lands it reports `unsupported`
(exit 3) rather than faking success — the scenario grammar does not change
when the switch flips.

**Pre-S05 stand-in (documented).** The server does not yet relay app data
and TURN/GOTO return `unimplemented` (S05). Fixture control frames
(`fxctl`) therefore ride the paired-members-only SIGNAL relay — the same
channel that carries WebRTC signaling — and the fixture leader sequences
turns, matching the role the leader plays on the webrtc-dc plane per
#75 §3.1. When S05 lands, the ops interpreter maps the same declarations
onto real TURN/GOTO frames. Server-side revision-bounded transport
switching *is* exercised for real: `dc_open` does a true RTCPeerConnection
handshake (host candidates) over SIGNAL, reports `TRANSPORT_STATE up`,
and the data-plane flip is asserted from server STATE.

**Markers, not mocks.** Screenshots carry synthetic page markers —
deterministic colors derived from the page name
(`verify_core.marker_rgb`, mirrored in fixture.html) — so "what page is on
which side, including the blank side" is checked from the pixels AND from
the DOM marker AND from the protocol state.

**Faults.** `ws_delay`, `ws_dup`, `ws_drop`, `sig_delay`, `suppress_ice`
(ICE establishment failure), `clear` — injected inside the fixture's own
send path, scoped to its session. `bystander_check` then pairs a second
runner-side session on the same env and asserts it is unaffected (the
spec's isolation requirement). DataChannel disconnect is `dc_close`;
server restart maps to `disconnect`+`resume` (a registry restart severs
links the same way); ICE failure is `suppress_ice`.

**Simulator driver.** Explicit UDIDs only — `simctl boot/openurl/io
screenshot/io recordVideo` and `idb ui tap/text/swipe/pinch/rotate/
button/describe-all`. Devices already booted at entry are used but never
shut down; devices the run boots are shut down on exit (and only those —
`external_state.before/after` records the invariant). `open-simulator.sh`
grew `open|screenshot|shutdown --udid` for the same multi-device rule.

## Scenario grammar (ops)

`pair` (optionally `via:"taps"` — real DOM taps), `open_work` (`work` is a
named `tests/fixtures/reader_sync_tree.py` entry or an inline chapter
list; op-level overrides scenario-level), `turn` (actor, delta ±1),
`goto` (index, clamps), `swap_sides`, `signal` (SIGNAL relay round-trip
both directions — the 双方向 spec), `rtt` (data-plane echo samples →
p50/p95), `dc_open`, `dc_close`, `disconnect`, `resume`, `resume_fail`,
`fault` (type above), `bystander_check`, `device_input`
(tap/text/swipe/pinch/rotate/describe), `tap_element` (AX-label tap),
`background`, `foreground`, `pwa_install`, `wait`, `screenshot`, `leave`.

Scenario-level keys: `name`, `drivers` (default both), `transports`
(default `server-ws`; each entry produces a separate run), `route`
(any/tailnet/lan/loopback), `requires` (capability gate:
webrtc/device_inputs/pwa/ws_bystander/ui), `work`, `ops`.
