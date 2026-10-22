# tile-flick-fps — tile-grid vertical-flick frame-gap measurement (Android)

One command measures FPS during vertical flicks on the bookq tile grid on
the real Android tablet (CP08-J1). Uses a rAF frame-gap probe injected over
CDP + `adb shell input swipe` — reports **scroll-active** frame gaps
(p50/p90/p99/max, >16.7ms, >33ms) instead of the `?perf=1` HUD's coarse
500ms windows.

Trigger on 「タイルで縦フリックのfps測って」「グリッドスクロールのフレーム
時間」「tile flick fps」「scroll-active frame」— use `android-perf` instead
for HUD sampling, LoAF/traces, or cost attribution.

## Prerequisites

Same as `android-perf`: wireless ADB connected (`adb devices` lists CP08_J1),
Chrome CDP forwarded, the bookq tab open on a `#/browse/<folder>` tile grid.
The script re-issues the forward itself; pairing is the manual step (see
`android-perf/SKILL.md` — never guess the pairing code).

**Stale-JS check before trusting numbers**: the SW serves old JS for 2–3
reloads after a deploy. `--reload` hard-reloads the tab; verify the change
marker exists (e.g. `cdp-eval.py 'typeof window.browseWhenSettled'`).

## Usage

```bash
cd .claude/skills/tile-flick-fps/references

# Synthetic flicks (kernel-level input injection — no hands needed):
uv run --with websockets python3 flick-fps.py --url dev-04.javanese

# Real-finger pass (authoritative for input-path costs):
uv run --with websockets python3 flick-fps.py --url dev-04.javanese --finger 12
# → script prints "DRAG NOW"; flick the grid for ~12s, it dumps + reports.
```

Defaults: 3 warm-up swipes + 8 measured upward swipes (`input swipe 700
1100 700 200 700`), scrollTop reset to 0 before arming. Useful flags:
`--swipes/--ms/--gap` (flick count/duration/spacing), `--no-top`,
`--settle MS` (scroll-active window, default 150), `--serial`.

## Reading the output

```
scrolls: 419 events, st 1 -> 3295 (range 3295 px)
scroll-active frames: 484 (of 586 total)
  p50 13.5ms  p90 22.2ms  p99 26.0ms  max 50.6ms
  >16.7ms: 212 (43.8%)   >33ms: 2 (0.4%)
  ~74 fps at p50 (90Hz panel: 11.2ms = vsync-bound)
  (all-frames p50 12.8ms — idle-inclusive, for contrast)
```

- **Only scroll-active frames matter** — a frame ending within `--settle` ms
  of a scroll event. All-frames p50 is printed for contrast; quoting it as
  "fps" overstates results because idle-vsYNC frames dominate a swipe series.
- **st range must be >~1500px** for a real traversal; <400px means the
  gesture bounced/rubber-banded and the numbers are noise.
- 90Hz panel: 11.2ms = one vsync (vsync-bound ≈89fps), 22ms ≈ 45fps,
  33ms ≈ 30fps. The 2026-10 jank baseline was **34–42ms scroll-active p50**;
  post-fix measurements land **13–16ms p50** with <0.5% >33ms.
- Absolute numbers vary with flick speed and device state — compare A/B
  only at identical `--ms/--swipes/--gap`, same tab, same folder.

## Pitfalls (all hit for real)

| Symptom | Cause | Fix |
|---|---|---|
| `probe lost` after run | A **downward** swipe at scrollTop=0 fired pull-to-refresh → page reloaded | Keep swipes upward (`y1>y2`); script defaults already do |
| `NO SCROLL EVENTS` | Swipe hit banner/dialog or wrong tab | `adb shell screencap` first; narrow `--url` |
| `dev-04` matches `dev-04-1` tabs | Ghost-node suffix contains the substring | Pass the FQDN: `--url dev-04.javanese` |
| p50 looks great but grid stutters | You read all-frames p50 (idle-dominated) | Read the scroll-active block |
| First run slower than second | Cold sprite/thumb decode | `--warmup` covers it; don't compare cold vs warm |
| Real finger much worse than `input swipe` | Injected swipes have no touchmove density | Expected — `--finger` is the authoritative pass |
| Numbers differ run to run | Fling speed/thermal/other tabs | Fix params; screencap to confirm no dialogs |

## References

- `references/flick-fps.py` — the whole measurement (probe + swipes + slice)
- `.claude/skills/android-perf/SKILL.md` — ADB/CDP setup, HUD, LoAF
  attribution, screenrecord ground-truth, wireless-ADB pairing
