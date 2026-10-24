# reader-sync verification runner (#85 S10 → #86 S11)

Automated integrated regression for the dual-reader feature (#75). One
declarative scenario suite (`scenarios.json`), two interchangeable
drivers (Chrome via Playwright, iOS Simulator via simctl+idb),
deterministic assertions, reproducible evidence.

> 「同じページ番号になった」だけでなく、2台が正しい左右画像を表示し
> 復帰できることを自動証明する。

Same page number is not proof: the suite verifies that both surfaces
display the correct left/right **images** (real decoded `<img>` plus a
pixel-checked color marker), stay on the same authoritative revision,
and recover from transport, lifecycle, manifest, image, and server
failures — all without human confirmation until S12.

```
scripts/reader-sync/
├── verify.py                — CLI orchestrator (exit 0/1/2/3)
├── verify_core.py           — scenario schema, plan resolution, result JSON,
│                              evidence layout, redaction, marker palette
├── verify_ops.py            — the op interpreter both drivers share
├── verify_drivers.py        — ChromeDriver (Playwright 2 contexts),
│                              SimulatorDriver (simctl + idb), BystanderPair
├── verify_fixture_server.py — fixture page + long-poll control channel
│                              (+ /manifest.json + /icon.png for PWA)
├── fixture.html             — runner-controlled client driving the REAL
│                              production modules + wire path
├── scenarios.json           — THE declarative suite (single source of truth)
├── seq_probe.py             — protocol/sequence probe
├── ws_probe.py              — connectivity probe
└── README.md
```

## Usage

```bash
# Chrome driver — two BrowserContexts in one Playwright process
uv run --with playwright --with websockets --with pillow \
    python scripts/reader-sync/verify.py \
    --driver chrome --dev-env <name>

# Simulator driver — two explicit-UDID iOS Simulators (already booted)
uv run --with websockets --with pillow \
    python scripts/reader-sync/verify.py --driver simulator \
    --leader-udid <UDID-A> --follower-udid <UDID-B> \
    --dev-env <name>

# Useful flags
--dev-env NAME      resolve URL via ./dev.sh url NAME
--url URL           or point at an explicit target
--scenario NAME     run only the named scenario (repeatable)
--surface ui        real pairing UI instead of the fixture (S09/#84)
--repeat N          override every scenario's repeat count
--headed            chrome: visible windows
--cmd-timeout SEC   per-control-command timeout
--evidence-root DIR --run-id ID   evidence placement control
```

## Exit codes (spec — いずれの失敗も exit 0 にしない)

| code | meaning |
|------|---------|
| 0 | every scenario PASS |
| 1 | an assertion failed — result JSON names the scenario + assertion |
| 2 | environment unavailable (devices/idb/URL/surface setup; run recorded) |
| 3 | an unsupported scenario/op/surface was requested |

Scenario statuses: `pass`, `fail`, `env_unavailable`, `unsupported`.
A scenario that can't run is still written to `result.json` — a silently
dropped scenario is itself a report bug.

## Evidence

`.omo/evidence/reader-sync/<run-id>/`:

- `result.json` — run_id, driver, surface, route, build_sha, devices
  (kind + udid/context + UA + RTC), capabilities, plan, per-scenario
  assertions, rtt_ms/latency_ms, external_state before/after, self_check
- `scenarios.json` — the declarations used + the resolved plan + skips
- `<scenario>/<transport>/*-{leader,follower}.png` — both-screen captures;
  the marker color is verified as a *fraction of the screen* (≥6%),
  so the check is orientation- and chrome-layout-proof
- `<scenario>/<transport>/trace-{leader,follower}.jsonl` — wire trace
  (frame types + timing only; **no codes, tokens, payloads, SDP/ICE**)

Pairing codes exist only in runner memory for the one JOIN — never
asserted on, logged, or written. Real folder names are excluded
structurally: only `fx:`-prefixed synthetic identifiers survive
redaction (`verify_core.redact`).

## Architecture

**Real wire path, not a stand-in.** `fixture.html` loads the production
`pairTransport.js`, `pairModel.js`, and `pairClient.js` straight from the
target origin and runs the real `/ws/reader-sync` protocol: CREATE/JOIN/
APPROVE/READY, OPEN_WORK + chunked MANIFEST (digest + workEpoch +
manifestVersion), TURN/GOTO/SET_OFFSET/SWAP_SIDES sequencing,
APPLIED/STATE convergence, RESUME, and WebRTC data-channel upgrade with
follower→leader→server request relay (`payload.origin = 'follower'`).
The runner only schedules ops and asserts; the page is dumb about
orchestration.

**Two surfaces, one declaration.** `--surface fixture` (default) serves
`fixture.html` via a runner-side control server. `--surface ui` targets
the production pairing UI (`ui_pair_and_inputs`). A scenario declares
`surface: ["ui"]` to opt in; undeclared means fixture-only, so fixture
scenarios are never planned on the real UI and vice versa. Asking for an
incompatible scenario by name records `unsupported` (exit 3).

**Markers + images, not mocks.** Screenshots carry synthetic page
markers — deterministic colors from the page name
(`verify_core.marker_rgb`, mirrored in fixture.html) — so "what page is
on which side, including the blank side" is checked from pixels AND from
the DOM marker AND from the committed protocol state AND from the real
decoded `<img>` (`imgState`: ok/loading/error/stalled).

**Faults.** WebSocket `ws_delay`/`ws_dup`/`ws_drop`, inbound
`in_drop`/`in_corrupt`/`in_inject`, `stale_base` rewrite,
`sig_delay`/`suppress_ice` for WebRTC, `dup_next` request replay, image
`img_fault` modes, manifest corrupt/drop recovery, lifecycle
(`hidden`/`visible`/`pagehide`/`pageshow`/`online`/`offline`), and
`bystander_check` (a second runner-side pair asserted unaffected).
`server_restart_repair` restarts the dev-env container for real.
Manifest corruption asserts the production transient error AND the
fixture's sticky `lastError`, then clears the fault and asserts
retransmission recovery to identical spread/revision/markers/images.

**Wire cleanliness.** Assertions distinguish a legitimately issued local
op, the leader-side WS relay copy of a follower DC request, and
housekeeping APPLIED/READY — none may inflate the sequenced-request
count.

**Control channel.** The page long-polls `/ctrl/next` (server holds ≤25s)
and posts results to `/ctrl/result`. The fetch aborts at 30s so a
stalled long-poll (rotation/backgrounding can freeze in-flight requests)
self-heals instead of killing the channel. Runner-side idempotent reads
(`state`, `code`, `value`, `rect`, `last_tap`, `pull_trace`, …) retry
once on an env timeout.

**Simulator driver.** Explicit UDIDs only — `simctl boot/openurl/io
screenshot` and `idb ui tap/text/swipe/pinch/rotate/button/describe-all/
describe-point`. Devices already booted at entry are used but never shut
down; devices the run boots are shut down on exit (only those —
`external_state.before/after` records the invariant).

## Scenario grammar (ops)

`pair` (optionally `via:"taps"` — real DOM taps through the fixture's own
開始/参加/承認 buttons), `open_work`, `turn`, `goto`, `swap_sides`,
`set_offset`, `burst`, `concurrent`, `signal`, `rtt`, `latency`,
`dc_open`, `dc_close`, `dc_restart`, `disconnect`, `resume`,
`resume_bad`, `reload`, `bfcache`, `lifecycle`, `fault`, `img_fault`,
`bystander_check`, `device_input` (tap/text/swipe/pinch/rotate/describe),
`tap_element`, `ui_input`, `open_url`, `expect`, `background`,
`foreground`, `pwa_install`, `wait`, `screenshot`, `leave`.

Op-level keys: `actor` (leader/follower), `requires` (capability gate),
and **`optional`** — an `EnvError`/`UnsupportedError` on an optional op
is recorded as a visible `optional_<op>` note instead of aborting the
scenario (used where the driver cannot reliably deliver an input kind;
the gap stays readable in result.json).

Scenario-level keys: `name`, `repeat`, `drivers` (default both),
`surface` (default fixture-only), `transports` (default `server-ws`;
each entry produces a separate run; `webrtc-dc` splices `dc_open` after
`pair`), `route` (any/tailnet/lan/loopback), `requires`
(webrtc/device_inputs/pwa/ws_bystander/ui/server_restart), `clients`
(per-role surface kind for simulator: safari/pwa), `work`, `ops`.

## iOS Simulator known limitations (iOS 27)

- **PWA (`pwa_pair`, `pwa_both`):** scripted Add-to-Home-Screen works
  end-to-end (page menu → share → 表示を増やす → ホーム画面に追加 →
  exact-match 追加 confirm; webclip containers are created on disk), but
  the installed webclip never registers on SpringBoard or in the App
  Library search — there is no supported way to launch it standalone.
  The scenarios report `env_unavailable`. Revisit on a different iOS
  version or with a real-device S12 pass.
- **`idb ui pinch`:** produces no web-visible gesture (neither pointer
  events nor legacy gesturestart/gestureend reach the page). The pinch
  op is declared `optional`; taps/swipes still drive real TURNs.
- **Edge-zone taps on wrapped toolbars:** the fixture's gesture layer
  ignores taps whose target is a control (`#fx-bar` wraps on narrow
  screens, so a button can sit inside the edge zone — otherwise one tap
  fires two TURNs).
- **Input font <16px** auto-zooms iOS Safari on focus and swallows the
  next tap's click — the fixture input is 16px and the viewport is
  `user-scalable=no`.
