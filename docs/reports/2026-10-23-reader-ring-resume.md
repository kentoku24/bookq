# Reader ring experiment: resume checkpoint

## Stop instruction

The user requested a resumable checkpoint because only 14% quota remained.
Do not start new optimizations or repeat completed benchmarks automatically.
Finish collecting the already-running final portrait comparison, preserve
artifacts, and stop. This file is the entry point for the next session.

## Repository state

- Worktree: `.worktrees/android-viewer-perf` (relative to the main checkout).
- Branch: `ai/android-viewer-perf`.
- HEAD/base: `362e1ba9a63522d384b20a6f8e8bdc1d5434a295`.
- Changes are intentionally UNCOMMITTED. No push, PR or production deployment.
- Main checkout stays on `main`; do not edit/reset it.
- Preserve all dirty/untracked files: they include the earlier Android
  controls/swipe-settle fixes, their tests/report, and this experiment.
- The existing `.omo/plans/android-viewer-perf.md` remains unchanged.

## Implemented experiment

Exact query flag `?reader=ring` selects the prototype; absent/unknown flags
keep the existing strip reader. The default has NOT been switched.

- `app/static/viewer/ring.js`: three persistent full-viewport slot roots,
  previous/current/next spread; at most six mounted images.
- `app/static/viewer/ringCache.js`: detached image/decode LRU cache,
  12 entries and 64 MiB ESTIMATED decoded-byte budget. Pinned visible/adjacent
  images are protected even if they exceed the byte budget. This is not a
  measured GPU/heap cap.
- Current-page fetch priority promotion, guarded stale completions,
  frozen geometry during motion, rAF-coalesced drag.
- Shared spread model, navigation, gestures, hash/progress, zoom/overview,
  original image URLs and offline resolver remain in use.
- Conditional scripts in `index.html`/`routes.py`; two SW shell assets added.
  Shell contract count updated from 21 to 23.
- Prototype uses viewport-wide stride instead of legacy variable-width
  spacing, and omits strip chapter-divider labels. It is not pixel-identical
  to the old transition geometry.
- One bounded refinement was completed: prepared adjacent taps skip
  synchronous full-window rebinding; redundant bindings/priority writes
  are skipped; far prefetch is refused if its estimated bytes cannot fit.

Do not remove the earlier changes to Android floating-control styling or
the `isTrackSlideEnd` guard in the shared swipe finalizer.

## Verification and unresolved gate

- Initial relevant selection: 72 passed.
- Post-review selection: 35 passed (27 ring cases plus shared tests).
- Latest hot-path selection: 47 passed (28 ring cases plus shared tests).
- The sync-ack regression was then hardened to read its counter in the same
  JS evaluation; that single test passed again.
- Full run: **857 passed, 2 failed, 6 skipped**. This was NOT a clean gate.
- One failure was the intentional shell count change; fixed to exact 23.
- The other was `tests/test_thumbnail_measurement.py::test_measure_thumbnails_real_app`
  returning `broken_images`. Its cause remains UNCONFIRMED; do not label it
  definitely unrelated or fixed just because an isolated rerun passed.
- Focused shell-contract + thumbnail rerun: 12 passed.
- No measurement-probe thresholds or thumbnail implementation were changed.
- Full suite was not rerun after the bounded ring-only refinement.
- Live-data regression wrapper and post-fix real-finger tests were not run.

## Completed final landscape comparison

Refined build, native Android input, desktop DevTools CLOSED, HUD OFF,
same warmed book/range, original image quality. Order: strip/ring/ring/strip;
five samples per operation per block, ten per renderer/operation.
All 80 measured turns reached the expected spread, and both target images
were loaded before every measured turn.

Median Chrome `TOUCH_RELEASED` EventLatency, milliseconds:

| Landscape operation | Strip | Ring |
|---|---:|---:|
| Tap next | 70.5 | 66.1 |
| Tap previous | 69.4 | 68.8 |
| Swipe next | 65.2 | 54.9 |
| Swipe previous | 57.8 | 55.4 |

This measures release to associated presentation, NOT first movement,
animation completion or physical FPS. Small synthetic samples do not prove a
general real-finger speedup. DOM images fell from 18 to 6 in landscape
(three ring spreads). The earlier portrait run showed 17 to 3, but its timing
data predates the last refinement; do not mix those versions.

## Final portrait comparison (completed 2026-10-23 11:41)

Same protocol as landscape: refined build, native input, DevTools CLOSED,
HUD OFF, strip/ring/ring/strip order, five samples per operation per block,
ten per renderer/operation, identical warmed book/range. All 80 measured
turns reached the expected spread and every target image was loaded before
each measured turn. Device stayed portrait throughout.

Median Chrome `TOUCH_RELEASED` EventLatency, milliseconds:

| Portrait operation | Strip | Ring | Delta |
|---|---:|---:|---:|
| Tap next | 87.1 | 71.0 | -16.1 |
| Tap previous | 92.5 | 67.5 | -25.0 |
| Swipe next | 64.8 | 62.5 | -2.3 |
| Swipe previous | 64.5 | 66.6 | +2.1 |

Max latencies: ring 69.2-81.6ms; strip 71.0-97.4ms.
DOM structure during measurement: strip 17 spread nodes / 17 mounted images;
ring 3 slots / up to 6 mounted images (portrait spreads hold one page).
Touch-end handler medians stayed 2-4ms both ways; the win is presentation
side, not input handling.

Portrait taps improved consistently; portrait swipes are a wash. Combined
with landscape (swipe-next -10.3ms, others within noise), the refined ring
is a modest, real but not breakthrough improvement under synthetic input.
Real-finger validation is still required before any default switch.

## Durable artifacts

Gitignored checkpoint directory:

```text
.omo/evidence/reader-ring-20261023/
  refined-landscape/   # copy of ...111704, including sanitized traces/analysis
  refined-portrait/    # copy of ...112807 after completion/analysis
  tools/              # exact copies of the lead-authored /tmp Python helpers
  logs/               # functional/full-gate evidence
  working-tree.patch  # tracked diff only; new source files remain in worktree
  runtime.json        # final runtime snapshot
```

All copies verified complete, including `refined-portrait/analysis.json`
(generated 11:41 after the sidekick's initial pass, then synced in). No
screenshots, videos or real library names belong in the repository, even
under this ignored directory. Local-only qualitative ring video and
outcomes: `/tmp/bookq-ui-check-20261023-112246/` (not timing evidence).

The old exploratory datasets `...105518` and `...110253` predate the last
hot-path refinement. Keep them separate from the final comparison.

## Runtime / restart

- Isolated dev name `android-viewer-perf`, slot 01, working-tree build.
  Resolve URL using `./dev.sh url android-viewer-perf`; no login required.
- Existing device target ID: `1834B2A9BDED6092C31878F6884B2F13`.
- ADB serial: `192.168.1.40:38401`; forward 9222 to
  `localabstract:chrome_devtools_remote`.
- Detached CDP relay last PID 93057: `127.0.0.1:9223` -> 9222, accepts only
  `devtools://devtools` or no Origin. Ordinary website origins get HTTP 403.
- Device restored to portrait, auto-rotation (`free`, accelerometer 1,
  user rotation 0). Original show-touches 1 and pointer-location 0 unchanged.
- Reading index 14, 163 images, default strip, `perf=0`; frontend closed.
- Dev, ADB and relay are deliberately left running for resumption.

Verify process/device/target IDs again after a restart; they may change.
Do not guess wireless pairing codes or print real book hashes/titles.
Tools contain absolute `/tmp` imports and session IDs. Restore missing helper
files from `tools/` if needed, but do not overwrite changed copies blindly.
Inspect/reuse live processes before rebuilding. Do not clear the user's
browser/reading/offline caches to fix stale code.

For source freshness, the benchmark checks the actual executing JS via
`Debugger.getScriptSource` SHA against the worktree, retries normal SWR
reloads, and checks `window.__bookqReader`. A fetched JS response alone is
not proof that the device is running it.

## Real-finger comparison (2026-10-23, portrait, FINAL)

Two blocks per condition at different times, same dev tab renderer (pid
29567 in all traces), same book/range (book moved `rawmanga/` → `manga/`
mid-session — same content, 163 images), DevTools frontend closed, HUD off,
warmed images, 20 gestures per block classified by order + touchmove counts.
Chrome `TOUCH_RELEASED` EventLatency, pooled medians in ms (n=10 each):

| Operation | Strip | Ring | Delta |
|---|---:|---:|---:|
| Tap next | 137.9 | 87.9 | **-50.0 (-36%)** |
| Tap previous | 132.5 | 88.1 | **-44.4 (-34%)** |
| Swipe next | 188.2 | 171.3 | -16.9 (-9%) |
| Swipe previous | 205.0 | 171.6 | -33.4 (-16%) |

Worst-case latencies: strip max 282.4ms; ring max 204.9ms. Ring taps were
also far more consistent (82-94ms range vs strip 104-282ms).

Per-block medians for reference:
- Strip A (12:38): tap 137.8 / 116.0, swipe 171.2 / 205.0
- Strip E (17:13): tap 138.0 / 137.8, swipe 215.8 / 215.7
- Ring D (15:11): tap 82.5 / 93.6, swipe 171.3 / 160.2
- Ring F (17:21): tap 93.7 / 82.4, swipe 171.4 / 193.6

Artifacts: `/tmp/bookq-real-touch-20261023-{123820,145622,171339,172132}/`
(strip A, ring D, strip E, ring F), each with `manual.trace.json`,
`status.json`, `analysis.json`.

Interpretation: real-finger data confirms ring is meaningfully faster —
taps ~35%, swipe-prev ~16%, swipe-next ~9%. Largest remaining cost is still
`SubmitCompositorFrameToPresentation` (~65-150ms medians) — compositor/
display pipeline, not JS or image count. Ring reduces but does not
eliminate that floor.

Capture procedure note: timed-window captures lose blocks whenever gestures
arrive after the window closes (three lost). Window is now 7200s in
`/tmp/bookq-real-touch-capture.py`. Mid-session the book's parent folder
was renamed `rawmanga/`→`manga/` by the user, breaking the stale
`bookq_progress` path — recovered by rewriting to the new path.

## Next-session priorities

1. Read this checkpoint; synthetic (both orientations) and the real-finger
   comparison are final (no rerun needed).
2. Decide the experiment outcome. Evidence now supports ring as a real
   real-finger improvement (taps ~-35%, swipes -9..-16%), consistent with
   synthetic direction. Still recommended: keep opt-in until
   landscape/cold-image real-finger checks and a code review pass.
3. If continuing toward production, isolate the thumbnail gate failure.
   Native Safari was not tested.
