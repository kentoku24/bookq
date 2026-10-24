# bookq on-device Android performance measurement

Measure real scroll/page-turn FPS on the Android tablet (FPD CP08-J1, Unisoc
T620 / Mali-G57 / 1200×1920 / Android 15) via ADB + Chrome DevTools Protocol +
the in-app `?perf=1` HUD. This is the complement of `perf-measurement` (which
times top-page display-complete on a local Playwright run) — use THIS skill for
"is scrolling/page-turning janky on the actual device".

## When to use

- 「実機で FPS 計測して」「タイル/ページめくりがカクつく」「tablet で遅い」
- A/B-ing a rendering change on the real GPU (synthetic Desktop numbers lie —
  the CP08-J1's Mali-G57 is the bottleneck, not the M4 host)
- Deciding whether a jank is main-thread JS vs compositor/raster cost

## Prerequisites

```bash
adb devices                                        # CP08_J1 must be listed
adb forward tcp:9222 localabstract:chrome_devtools_remote   # Chrome CDP
curl -s http://localhost:9222/json | head          # must list pages
```

- Wireless ADB drops on sleep/network changes: `adb pair <ip:port>` (code from
  the tablet's 開発者向けオプション→ワイヤレスデバッグ→ペア設定) then
  `adb connect <ip:port>`. Ask the user for the pairing code — never guess.
- The dev environment's URL is `https://dev-XX.<tailnet>.ts.net` (XX = slot
  01..10). `./dev.sh url <name>` resolves the env name to its slot FQDN.
  **If only a `dev-XX-1` name serves, that's a ghost-node collision** — an
  offline `dev-XX` node still registered in the tailnet forced the suffix.
  Recover via docs/deployment.md「ノード名の衝突」; until then, measure
  against the `dev-XX-1` URL (dev.sh prints the real FQDN after `up`).
- Measurement page: `https://dev-XX.../?perf=1&r=<cache-buster>#/browse/<folder>`
  — bump `r=` to force a fresh navigation past SW stale-while-revalidate.

## The HUD (`?perf=1`)

`fps <N> worst <N>ms view:<orig|jpeg> st:<scrollTop> g:<0|1|-> v<N>` bottom-left.

- **fps/worst** — 500ms-window rAF rate + worst frame. rAF starves during jank,
  so numbers skew *pessimistic* vs visible smoothness once listeners are passive.
- **st** — `#sidebar-scroll.scrollTop`. If `st` never moves across samples the
  gesture did not scroll — your fps is measuring idle/bounce, not scroll.
- **g** — thumbnail gate (1 on / 0 off / - marker unset). Chrome default `g:0`,
  Firefox default `g:1`.
- **v** — build marker bump per deploy (currently `v10`). **ALWAYS verify the
  marker before interpreting numbers** — the Service Worker serves stale JS;
  it takes 2–3 reloads (or site-data clear) to flush.

## Diagnostic flags (opt-in via URL or localStorage)

| Flag | Effect |
|---|---|
| `?thumbgate=1` / `bookq_thumbgate=1` | scroll-settled img gate: offscreen imgs → `data-src`, rehydrate 250ms after settle, batches of 4/60ms, suppressed while a touch is down |
| `?noblur=1` | `#toolbar` backdrop-filter off (diagnostic; Android now gets this via the `ua-android` class automatically) |
| `?view=jpeg` / `bookq_view=jpeg` | reader serves JPEG-normalised `/view/` URLs instead of AVIF/WebP originals |

`localStorage` overrides are read once per document — a `location.hash` change
does NOT re-read them. Full `location.reload()` is required.

## Measurement flow

Run serially — one measurement at a time, the device is single-window.

### 1. Establish ground truth BEFORE measuring

```bash
adb shell screencap -p /sdcard/s.png && adb pull /sdcard/s.png /tmp/s.png
python3 -c "from PIL import Image; Image.open('/tmp/s.png').resize((720,1152)).save('/tmp/s_small.png')"
```

Look at it. Verify: correct app (not tab switcher/dialog), correct folder,
HUD present with the expected `v` marker. The HUD text sits bottom-left;
crop `y∈[1500,1800]` at full 1200×1920 for a zoomed read.

### 2. Reset scroll position — ALWAYS

```bash
cdp-eval.py "document.getElementById('sidebar-scroll').scrollTop=0; 'top'"
```

Swiping while already at the bottom produces input-cost fps with `st` frozen —
classic false-bad reading.

### 3. Fire the gesture series while sampling the HUD

```bash
cdp-hud-sample.py 12 &                                # ~550ms × 12 samples
sleep 1
for i in 1 2 3 4 5 6 7 8; do
  adb shell input swipe 600 1300 600 400 90; sleep 0.45
done
```

Read the series: samples where `st` is increasing are real mid-scroll fps;
samples at a fixed `st` are settle/bounce. A Chrome fling covers ~2800px in
<1s, so short swipes (`1100→800`, 70ms) give better mid-scroll capture.

### 4. Attribute the cost (only if mid-scroll fps is still bad)

```bash
cdp-eval.py "$(cat references/perf-observers.js)"     # arms __lt + __loaf
# ...run the gesture series again...
cdp-eval.py 'JSON.stringify({lt:window.__lt,loaf:window.__loaf})'
```

- `blocking:0` + `scripts:[]` → **compositor/raster-side** (backdrop-filter
  re-blur, layer cost, GPU back-pressure). Do not chase JS.
- `scripts:[ms@file:line:invoker]` → JS on the frame; fix that handler.
- This is how `backdrop-filter` was caught: 50-95ms frames, zero blocking.

### 5. True display rate (ground truth vs HUD)

HUD fps is an rAF proxy. For actual screen update rate:

```bash
adb shell screenrecord --time-limit 20 /sdcard/r.mp4    # FOREGROUND — a
# backgrounded screenrecord gets killed and writes no moov atom
# (fire swipes during the window, then:)
adb pull /sdcard/r.mp4 /tmp/ && ffmpeg -i /tmp/r.mp4 /tmp/f_%03d.png
ls /tmp/f_*.png | wc -l                                # frames ÷ 20s = real fps
```

Diff frames to confirm the grid actually scrolled (a static page at "90fps"
proves nothing). `vsync` is not a valid ffmpeg option on this build — extract
raw frames instead.

### 6. Real-finger pass (the only authoritative number)

`input swipe` emits coarse, sparse touch events — it does NOT reproduce a real
finger's continuous `touchmove` density. After synthetic numbers look good,
ask the user to flick the tile grid and read the HUD. Synthetic ≈ real for
compositor-bound costs (blur, raster); it under-reports input-path costs
(non-passive listeners, hydration mid-drag).

## Pitfalls (all hit for real)

| Symptom | Cause | Fix |
|---|---|---|
| HUD shows old behaviour after deploy | SW stale-while-revalidate | Check `v` marker; reload 2–3× |
| `st` frozen during swipe series | At scroll bottom (bounce) or swipe started on the 続きから banner | `scrollTop=0` first; swipe from grid area, not banner |
| fps bad with `st` never moving | Swipe consumed by Chrome tab-switcher / app-switch dialog | Screencap first; `curl /json/activate/<id>` to raise the tab |
| Multiple tabs claim `visibility:visible` | Android Chrome reports several WebContents visible | Don't trust visibilityState — screencap, or pick tab by URL substring |
| `Input.dispatchTouchEvent` / `synthesizeScrollGesture` hangs | Unreliable on this Chrome build | Use `input swipe` (coarse but works) or JS `scrollTop` animation |
| JS `scrollTop` animation = 80-90fps but real flick is slow | JS path bypasses input dispatch entirely | It's a render-cost probe, NOT representative of the input path |
| Numbers differ run to run | Cold sprite/thumb generation on first scroll | Scroll the full list once to warm, then measure |
| `g:-` despite `thumbgate` override | `_gateOn` memoised per document | Full reload after changing localStorage/flag |
| screenrecord output won't decode | Killed in background → no moov | Run `--time-limit` in foreground shell |
| `dev-XX` URL hangs / only `dev-XX-1` serves | Offline ghost node still holds the `dev-XX` name in the tailnet | `tailscale status` for the ghost; delete in admin console, re-up (docs/deployment.md「ノード名の衝突」) |

## References

- `references/cdp-eval.py` — eval JS in a device Chrome tab
  (`uv run --with websockets python3 cdp-eval.py '<expr>' [url-substring]`)
- `references/cdp-hud-sample.py` — in-page HUD sampler for gesture series
- `references/perf-observers.js` — LoAF + longtask collectors (step 4)
- `.claude/skills/page-turn-fps/SKILL.md` — page-turn frame-gap measurement
  in the viewer (synthetic tap/swipe + rAF probe, `--reader strip|ring`
  A/B, `--finger` real-finger pass). rAF = produced-frame cadence; physical
  display fps still needs step 5.
- `.claude/skills/video-verification/SKILL.md` — same frame-extraction idea
  for timing-dependent glitches
- `.claude/skills/perf-measurement/SKILL.md` — display-complete timing
  (different question: "how fast does the page appear", not "does it scroll")
