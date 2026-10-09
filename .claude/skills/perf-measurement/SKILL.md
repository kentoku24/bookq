---
name: perf-measurement
description: Use when measuring bookq top-page display-complete performance on a dev environment with real data — boots a --local dev env with the live-data mount, runs scripts/perf/measure.py (ONLY=A) plus a Playwright display-complete pass, and records a docs/reports baseline. Trigger on "パフォーマンス計測して", "表示速度を測って", "measure top page performance", "display-complete を計測", or filling docs/performance.md TODO values.
triggers:
  - パフォーマンス計測して
  - 表示速度を測って
  - measure top page performance
  - display-complete を計測
  - docs/performance.md の実測値を埋める
---

# bookq top-page performance measurement

Measure how long the top page takes to fully display (display-complete: navigation start until every `#tile-grid` image is `complete`), on a dev environment with the real (live) image library mounted read-only and auth skipped. Produces numbers directly comparable to the KPI table in `docs/performance.md`.

**Never substitute this for touching production.** All measurement runs against a `--local` dev environment (`http://127.0.0.1:<port>`). Never point any measurement tool at the production URL, never run `./prd.sh`, never `docker compose up` the production project (a second `bookq` tailnet node fights prod — see `docs/deployment.md`).

## When to use this

- When asked to measure top-page display performance / display-complete (「パフォーマンス計測して」「表示されきるまで測って」)
- When `docs/performance.md` TODO values need filling with measured medians
- Per-PR before/after measurement on real data (same conditions as the KPI table: iPhone profile, 3-run medians)

## The flow

Writes (`docs/`) belong in a task worktree per AGENTS.md §8. Measurement itself is read-only. Run the measurement steps strictly serially — the dev server is single-worker, and parallel runs contaminate each other's timings.

### 1. Boot a dev environment with real data

```bash
./dev.sh up <name> --local --port 8080
```

- `--local` publishes `http://127.0.0.1:<port>` (default 8080; on collision retry once with `--port 8099` and use it everywhere below). No tailnet, no `TS_AUTHKEY` needed.
- Auth is skipped via `BOOKQ_TEST_MODE=true` (dev default) — no login step.
- Do NOT pass `--fixture` (synthetic tree) or `--lan-allow`.

### 2. Verify the real-data mount (STOP if empty)

A green boot does NOT guarantee real data — a fresh worktree falls back to its empty `downloads/`. Check:

```bash
docker inspect <viewer-container> --format '{{json .Mounts}}'   # /app/downloads Source must be the real library, Mode ro
curl -s "http://127.0.0.1:<port>/api/folders?depth=1" | python3 -c "import sys,json;print(len(json.load(sys.stdin)))"
```

- Count must be > 0. If 0 → STOP, do not measure: discover the live-data source read-only via `docker ps --filter name=bookq-prd` + `docker inspect bookq-prd-viewer --format '{{json .Mounts}}'` (ps/inspect only — never exec/restart/stop prod, never read `.env`), verify with an `ls` count, then `./dev.sh down <name>` + `DOWNLOADS_HOST_DIR=<source> ./dev.sh up <name> --local --port <port>`.
- Record env facts: URL/port, container, mount source + top-level count, `/api/version`, folders bytes + count, `/` status (expect 200).

### 3. Canonical baseline (measure.py)

```bash
mkdir -p .omo/evidence/<run-dir>/01-measure
ONLY=A uv run --with playwright --with-requirements requirements-dev.txt \
  python scripts/perf/measure.py http://127.0.0.1:<port> --runs 3 \
  --out .omo/evidence/<run-dir>/01-measure
```

- `ONLY=A` = iPhone profile (390×844 DPR3), the KPI-table condition. Do not modify `measure.py`; do not run `ONLY=B` / `ONLY=A_cpu4x` unless asked.
- Expect `baseline.md` (20-row table, no `-` cells) + `baseline.json` (medians non-null) + `tiles_settled` true on ≥1 run.
- Chromium missing → `uv run --with playwright playwright install chromium` once, then re-run.

### 4. Precision display-complete pass

`measure.py` reports `tiles_settled` (bool) but not display-complete ms. Measure it with a throwaway Playwright script (evidence-only, never under `scripts/`): fresh `browser.new_context` per run (cold: empty cache, no SW) → `add_init_script(PERF_INIT_JS)` → `goto(url, wait_until="commit")` → `wait_for_function(settleAt, 30s)` → evaluate `COLLECT_JS` + SW-registration count → screenshot → `ctx.close()`. iPhone context: viewport 390×844, DPR 3, mobile + touch, iPhone UA below. Desktop: 1280×800 default UA, 1 run, reference-only.

- 3× iPhone + 1× desktop. Expect per-run JSON (`display_complete_ms` non-null, `tile_count > 0`, `sw_registrations == 0`) + png + a summary JSON with the median.
- Cold = fresh browser context. Server-side caches (folders TTL 300 s, thumb disk cache) persist across runs: run 1 is closest to cold-server state, and a TTL miss costs a full rescan (observed ~6.4 s on real data for `?depth=1`).
- Settle definition: every `#tile-grid img` is `img.complete` (covers load AND error), stamped in-page the moment it holds (see caveat in Failure triage).
- A run with no settle in 30 s is recorded `"settle_timeout"` with a state dump — never fabricated; all 3 failed → STOP as env failure.

PERF_INIT_JS (use verbatim):

```js
(() => { try { performance.setResourceTimingBufferSize(2000); } catch (e) {} window.__perf = {navStart: null, firstTile: null, settleAt: null, longtasks: []}; try { new PerformanceObserver((l) => { for (const e of l.getEntries()) window.__perf.longtasks.push({start: e.startTime, dur: e.duration}); }).observe({type: 'longtask', buffered: true}); } catch (e) {} const nav = performance.getEntriesByType('navigation')[0]; window.__perf.navStart = nav ? nav.startTime : 0; const tick = () => { const g = document.getElementById('tile-grid'); if (!g) return false; const imgs = Array.from(g.querySelectorAll('img')); if (imgs.length === 0) return false; if (imgs.every((im) => im.complete) && window.__perf.settleAt === null) { window.__perf.settleAt = performance.now(); return true; } return false; }; const watch = () => { const g = document.getElementById('tile-grid'); if (!g) { document.addEventListener('DOMContentLoaded', watch); return; } if (g.children.length > 0 && window.__perf.firstTile === null) window.__perf.firstTile = performance.now(); new MutationObserver((m, obs) => { if (g.children.length > 0 && window.__perf.firstTile === null) { window.__perf.firstTile = performance.now(); obs.disconnect(); } }).observe(g, {childList: true}); }; watch(); const iv = setInterval(() => { if (tick()) clearInterval(iv); }, 100); setTimeout(() => clearInterval(iv), 60000); })();
```

COLLECT_JS (use verbatim):

```js
() => { const nav = performance.getEntriesByType('navigation')[0] || {}; const res = performance.getEntriesByType('resource').map((e) => ({name: e.name, start: e.startTime, dur: e.duration, transfer: e.transferSize, decoded: e.decodedBodySize})); const bucket = (n) => { const p = decodeURIComponent(new URL(n).pathname); if (p.endsWith('.js')) return 'js'; if (p.endsWith('.css')) return 'css'; if (p.startsWith('/thumb/')) return 'thumb'; if (p.startsWith('/image/')) return 'image'; if (p.startsWith('/api/')) return 'api'; return 'other'; }; const agg = {}; for (const r of res) { const b = bucket(r.name); const a = agg[b] || (agg[b] = {count: 0, transfer: 0, decoded: 0}); a.count += 1; a.transfer += r.transfer || 0; a.decoded += r.decoded || 0; } const folders = res.filter((r) => r.name.includes('/api/folders')); return {url: location.href, display_complete_ms: (window.__perf && window.__perf.settleAt !== null && window.__perf.navStart !== null) ? window.__perf.settleAt - window.__perf.navStart : null, first_tile_ms: (window.__perf && window.__perf.firstTile !== null && window.__perf.navStart !== null) ? window.__perf.firstTile - window.__perf.navStart : null, nav_ttfb_ms: nav.responseStart !== undefined ? nav.responseStart : null, nav_dcl_ms: nav.domContentLoadedEventEnd !== undefined ? nav.domContentLoadedEventEnd : null, nav_load_ms: nav.loadEventEnd !== undefined ? nav.loadEventEnd : null, longtasks: window.__perf ? window.__perf.longtasks : [], resource_agg: agg, folders: folders, tile_count: document.querySelectorAll('#tile-grid .tile, #tile-grid .item, #tile-grid .card').length, tile_img_count: document.querySelectorAll('#tile-grid img').length}; }
```

iPhone UA: `Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1`

### 5. Simulator screenshot (visual proof only)

```bash
./scripts/open-simulator.sh http://127.0.0.1:<port>
xcrun simctl io booted screenshot .omo/evidence/<run-dir>/03-simulator.png
```

Default device iPhone 17. Real Safari has no timing instrumentation — screenshot only. The first shot may be a blank first-paint; reopen once if so. Source of truth for Simulator mechanics: `.claude/skills/ios-simulator/SKILL.md`.

### 6. Tear down

```bash
./dev.sh down <name>
```

Verify with label-filtered `docker ps` (empty) + `./dev.sh list` (row gone). Never `--all` / `--full`, never hand-`docker rm`, never touch other envs.

### 7. Report (delivery)

- `docs/reports/YYYY-MM-DD-<slug>.md`: environment / method / results (per-run values + medians) / KPI comparison / findings (file:line-cited facts only) / evidence-path appendix. Every number cites its evidence file. Redact real-data names (counts/bytes/timings only); reference screenshots as text paths, never embed them.
- `docs/performance.md` TODO block: fill with medians + a method note (Chromium iPhone profile over localhost; tailnet HTTPS and real-device Safari unmeasured — no instrumentation path).

## Failure triage

| Symptom | Cause | Fix |
|---|---|---|
| `/api/folders?depth=1` returns `[]` | `DOWNLOADS_HOST_DIR` unset → empty worktree `downloads/` fallback | Discover prod source read-only (§2), re-up with explicit `DOWNLOADS_HOST_DIR` |
| `dev.sh up` pool exhausted (10/10) | No free slot | Record `./dev.sh list`, STOP — never evict others |
| `tiles_settled` false on all runs | Broken env or page | Cross-check with the precision pass; both failing → STOP as env failure |
| Settle fires with few imgs (e.g. 3 thumbs) | Intermediate render (progressive grid population) | Keep as-recorded; the median absorbs it; document the caveat |
| Port in use | 8080 taken | Retry once with `--port 8099`, use it everywhere |
| Thumb counts differ between runs | Cache warmth + early-settle variance | Expected; report per-run values, headline the median |

## Related

- `docs/performance.md` — KPI table, targets, and the TODO block this fills
- `scripts/perf/measure.py` — canonical baseline tool (read-only, Playwright chromium)
- `tests/test_top_display_perf.py` — settle-definition reference (`img.complete` polling)
