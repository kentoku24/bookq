---
name: page-turn-fps
description: Measure bookq viewer page-turn frame gaps on the real Android tablet — tap/swipe next/prev via `adb shell input`, rAF probe over CDP, strip/ring A/B via --reader, optional --finger real-input mode. Trigger on 「ページ送りのfps測って」「めくりアニメーションのフレーム時間」「viewer turn fps」「reader=ring A/B」.
---

# page-turn-fps — viewer page-turn frame-gap measurement (Android)

One command measures frame gaps during page turns (tap / swipe, both
directions) in the bookq **viewer** on the real Android tablet (CP08-J1,
90Hz). A rAF+touch/idx probe is injected over CDP and `adb shell input`
drives the gestures — **no manual input needed**. This is the viewer-side
counterpart of `tile-flick-fps` (which measures the browse grid).

Trigger on 「ページ送りのfps測って」「めくりアニメーションのフレーム時間」
「viewer turn fps」「reader=ring A/B」— use `android-perf` for HUD sampling,
LoAF/traces and cost attribution, or `tile-flick-fps` for the tile grid.

## Usage

```bash
cd .claude/skills/page-turn-fps/references

uv run --with websockets python3 turn-fps.py --url dev-01.javanese \
    --reader ring          # navigate + verify reader mode, then measure
uv run --with websockets python3 turn-fps.py --url dev-01.javanese \
    --reader strip --taps 5 --swipes 5
uv run --with websockets python3 turn-fps.py --url dev-01.javanese \
    --finger 15            # real-finger pass instead of synthetic input
```

Setup (adb pairing, CDP forward, wakefulness check, tab picking) is
self-recovering exactly like `tile-flick-fps` — the script re-issues the
`adb forward`, reconnects wireless ADB via mDNS, and fails with specific
messages otherwise. The device page must be a bookq **viewer** (a `#/view/`
book open) — the script verifies the hash AND a non-empty image list
*before* sending any input, so it never taps on a browse grid. It also
calibrates the tap Y coordinate against a real `clientY` readback (top
chrome ≠ `screen.height - innerHeight`), holds synthetic taps for 65ms via
a same-point `input swipe`, and aborts if the `?perf=1` HUD is on —
including a persisted `localStorage bookq_perf=1`, which it clears once.

Useful flags: `--taps/--swipes` (per direction, default 5), `--warmup`
(default 4 turns — cold decode skews the first pass), `--settle` (ms of
post-release window counted as turn-active, default 400), `--serial`,
`--reader strip|ring`, `--finger`.

## Reading the output

```
gestures: 20, idx changes: [(14, 15), (15, 16), ...]
tap: 10 gestures, 260 frames
  p50 11.9ms  p90 26.5ms  p99 33.8ms  max 39.0ms
  >16.7ms(>=1 miss): 55 (21.2%)  >27.8ms(>=2): 3 (1.2%)
  ~84 fps at p50
```

- 90Hz panel: 11.1ms = vsync-bound (~90fps). Gap plateaus sit at
  ~11.1/22.2/33.3ms, so miss thresholds are the midpoints: >16.7ms = ≥1
  missed vsync, >27.8ms = ≥2. p50 near 11ms means vsync-bound — **compare
  the tails** (p90/p99/max and miss %), not the median.
- Each gesture is verified to have produced an `idx` change inside its
  window; gestures that did not turn (missed zone, chapter boundary,
  viewer exited) are excluded from the stats, reported as `skipped`, and
  fail the run (exit 1) in synthetic mode — their clean ~11ms windows
  would otherwise flatter the tail.
- Classification: a gesture with zero `touchmove` events is a tap, any
  moves = swipe. Window per gesture = touchstart → touchend + `--settle`.

## Reader A/B protocol

`--reader` navigates the tab (`?reader=ring` vs none) preserving the book
hash, then asserts `window.__bookqReader` — SW stale-while-revalidate can
serve the old app.js, so a mode mismatch fails fast instead of producing
numbers for the wrong build. For comparisons use identical
`--taps/--swipes/--settle`, same tab, same book, warmed each time.

## Caveats (hit for real)

- **rAF gaps ≠ physical display fps.** The probe measures the renderer's
  produced-frame cadence. For true presented-frame rate use
  `android-perf`'s step-5 `screenrecord` + ffmpeg extraction — heavier and
  adds encoder load; rAF is the right A/B proxy.
- **Synthetic `input` is not a real finger** — sparse touchmove density
  under-reports input-path cost. EventLatency/real-finger capture lives in
  the investigation tooling (`bookq-real-touch-capture.py`); this skill's
  `--finger` mode records rAF windows for manual gestures.
- The viewer must already be open on the right book — deep-link restore
  after a folder rename can 404 (check the `/json` tab URL still resolves).
- Re-running against the same tab is safe — the probe un-arms any previous
  install before re-arming, so a second run never re-reports stale frames.

## References

- `references/turn-fps.py` — the whole measurement (probe + input + slice)
- `.claude/skills/android-perf/SKILL.md` — ADB/CDP setup, HUD, LoAF/traces,
  screenrecord ground truth
- `.claude/skills/tile-flick-fps/SKILL.md` — same idea for the tile grid
