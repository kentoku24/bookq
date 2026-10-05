"""TEMPORARY Todo1 harness: /thumb waterfall + cold breakdown (DO NOT permanize).

Plan: `.omo/plans/root-folder-perf-under-5s.md` Todo 1, Step 2.
Extracts `performance.getEntriesByType('resource')` entries whose name
contains `/thumb/`, plus the `/api/folders` split
(TTFB / body bytes+gzip / parse+restore window / first-tile /
lightweight display-complete / multi follow-up), and writes JSON.

Lightweight display-complete (D3): every `#tile-grid .tile[data-path] img`
`img.complete` — the same polling as
`tests/test_top_display_perf.py::_wait_for_all_tile_images`.
`single-cover w=200 first` is NOT implemented yet (Todo3), so on current
code lightweight == final settle; both timestamps are recorded separately
so Todo3 can show the split. The "multi follow-up" pass waits an extra
idle window and recounts imgs/bytes.

Cold definition (plan Goal): viewer restart
(`metadata_index_cache=None` + `folders_body_cache` empty, all workers).
This script CANNOT restart the server itself — start a fresh server first
(run 1 ~= cold, runs 2+ warm via per-process cache), or restart between
runs and merge the JSON files. The `cold` flag in the output records which
case the driver claims.

Usage:
  uv run --with playwright --with-requirements requirements-dev.txt \
    python scripts/perf/collect_thumb_waterfall.py [BASE_URL] [--runs 3] [--out DIR] [--cold]

Writes OUT_DIR/waterfall.json (default OUT_DIR=output/perf-<timestamp>/)
and prints a numeric summary for `output/perf-<timestamp>/baseline.md`.
Read-only: no app code is touched, no asserts are modified.
"""

import argparse
import datetime
import json
import statistics
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser(description="TEMP Todo1 thumb waterfall collector")
parser.add_argument("base_url", nargs="?", default="http://127.0.0.1:8080")
parser.add_argument("--runs", type=int, default=3)
parser.add_argument("--out", default=None,
                    help="output dir (default output/perf-<timestamp>/)")
parser.add_argument("--cold", action="store_true",
                    help="driver asserts BASE was freshly restarted (run 1 ~= cold)")
parser.add_argument("--settle-ms", type=int, default=2500,
                    help="idle window after lightweight settle for multi follow-up")
_args = parser.parse_args()
BASE = _args.base_url.rstrip("/")
OUT_DIR = Path(_args.out or f"output/perf-{datetime.datetime.now():%Y%m%d-%H%M}")
RUNS = _args.runs
COLD = _args.cold
SETTLE_MS = _args.settle_ms

IPHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

# A_iphone profile from scripts/perf/measure.py (390x844 DPR3).
CONTEXT_KWARGS = dict(viewport={"width": 390, "height": 844},
                      device_scale_factor=3, is_mobile=True, has_touch=True,
                      user_agent=IPHONE_UA)

INIT_SCRIPT = r"""
(() => {
  try { performance.setResourceTimingBufferSize(3000); } catch (e) {}
  window.__cc = {firstTile: null};
  const watch = () => {
    const g = document.getElementById('tile-grid');
    if (!g) return false;
    if (g.children.length > 0) { window.__cc.firstTile = performance.now(); return true; }
    new MutationObserver((m, obs) => {
      if (g.children.length > 0 && window.__cc.firstTile === null) {
        window.__cc.firstTile = performance.now(); obs.disconnect();
      }
    }).observe(g, {childList: true});
    return true;
  };
  if (!watch()) document.addEventListener('DOMContentLoaded', watch);
})();
"""

TILE_IMG_SELECTOR = "#tile-grid .tile[data-path] img"
SETTLE_FN = (
    "(sel) => { const imgs=[...document.querySelectorAll(sel)];"
    " return imgs.length > 0 && imgs.every((i) => i.complete); }"
)


def _median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


def run_once(browser):
    ctx = browser.new_context(**CONTEXT_KWARGS)
    page = ctx.new_page()
    page.add_init_script(INIT_SCRIPT)
    page.goto(BASE + "/", wait_until="commit")
    t_nav0 = page.evaluate("() => performance.now()")
    page.wait_for_function(
        "() => document.querySelector('#tile-grid') && "
        "document.querySelector('#tile-grid').children.length > 0",
        timeout=30000)
    page.wait_for_load_state("load")
    # Lightweight display-complete: single-cover w=200 all-tile img.complete
    # (same polling as _wait_for_all_tile_images; multi-grid not yet split).
    try:
        page.wait_for_function(SETTLE_FN, arg=TILE_IMG_SELECTOR, timeout=15000)
        lightweight_ok = True
    except Exception:
        lightweight_ok = False
    t_light = page.evaluate("() => performance.now()")
    n_light = page.evaluate(
        "(sel) => document.querySelectorAll(sel).length", TILE_IMG_SELECTOR)
    # Multi follow-up: idle window, then recount (lazy/imgs added late).
    page.wait_for_timeout(SETTLE_MS)
    t_final = page.evaluate("() => performance.now()")
    n_final = page.evaluate(
        "(sel) => document.querySelectorAll(sel).length", TILE_IMG_SELECTOR)
    tile_total = page.evaluate(
        "() => document.querySelectorAll('#tile-grid .tile').length")

    data = page.evaluate("""() => {
      const nav = performance.getEntriesByType('navigation')[0] || null;
      const res = performance.getEntriesByType('resource').map((e) => ({
        name: e.name, start: e.startTime, dur: e.duration,
        ttfb: e.responseStart - e.requestStart, end: e.responseEnd,
        transfer: e.transferSize, encoded: e.encodedBodySize, decoded: e.decodedBodySize,
      }));
      return {
        nav: nav ? {responseStart: nav.responseStart, responseEnd: nav.responseEnd,
                    dcl: nav.domContentLoadedEventEnd, load: nav.loadEventEnd} : null,
        firstTile: window.__cc.firstTile,
        resources: res,
      };
    }""")

    # Direct /api/folders fetch for body bytes + gzip header (same session).
    api_probe = page.request.get(BASE + "/api/folders")
    try:
        body = api_probe.body()
        api_direct = {"status": api_probe.status,
                      "content_encoding": api_probe.headers.get("content-encoding"),
                      "content_length": api_probe.headers.get("content-length"),
                      "body_bytes": len(body)}
    except Exception as e:  # noqa
        api_direct = {"error": str(e)[:120]}

    thumbs = [r for r in data["resources"] if "/thumb/" in r["name"]]
    thumbs_sorted = sorted(thumbs, key=lambda r: r["start"])
    folders = [r for r in data["resources"] if "/api/folders" in r["name"]]
    f0 = folders[0] if folders else None

    first_tile = data["firstTile"]
    # Breakdown windows (all ms, performance.now() base):
    #  w_backend = nav0 -> folders.responseEnd (server scan+serialize+transfer+parse)
    #  w_js      = folders.responseEnd -> firstTile (res.json+parse+restoreTree+render)
    #  w_thumbs  = firstTile -> lightweight settle (thumb fetch+settle)
    # res.json+parse vs restoreTree cannot be split without app
    # instrumentation (out of scope for Todo1) — recorded combined as w_js.
    w_backend = (f0["end"] - 0) if f0 else None  # startTime-base below
    result = {
        "t_nav0_ms": round(t_nav0, 1),
        "first_tile_ms": round(first_tile, 1) if first_tile is not None else None,
        "lightweight_display_complete_ms": round(t_light, 1),
        "lightweight_ok": lightweight_ok,
        "final_ms": round(t_final, 1),
        "tile_count": tile_total,
        "tile_img_lightweight": n_light,
        "tile_img_final": n_final,
        "nav": data["nav"],
        "api_folders_perf": f0,
        "api_folders_direct": api_direct,
        "thumb_count": len(thumbs),
        "thumb_transfer_bytes": sum((r["transfer"] or 0) for r in thumbs),
        "thumb_decoded_bytes": sum((r["decoded"] or 0) for r in thumbs),
        # waterfall: startTime order (reference for Todo2 depth=1-first assert)
        "thumb_waterfall": [{"name": r["name"], "start": round(r["start"], 1),
                             "dur": round(r["dur"], 1),
                             "ttfb": round(r["ttfb"], 1),
                             "transfer": r["transfer"]} for r in thumbs_sorted],
        "folders_start_ms": round(f0["start"], 1) if f0 else None,
        "first_thumb_start_ms": (round(thumbs_sorted[0]["start"], 1)
                                 if thumbs_sorted else None),
        "_w_backend_ms": round(f0["end"], 1) if f0 else None,
        "_w_js_ms": (round(first_tile - f0["end"], 1)
                     if (f0 and first_tile is not None) else None),
        "_w_thumbs_ms": (round(t_light - first_tile, 1)
                         if first_tile is not None else None),
    }
    ctx.close()
    return result


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        runs = []
        for i in range(RUNS):
            print(f"[waterfall] run {i + 1}/{RUNS}", flush=True)
            try:
                runs.append(run_once(browser))
            except Exception as e:  # noqa
                print(f"  ERROR: {e!r}", flush=True)
                runs.append({"error": repr(e)})
        browser.close()
    good = [r for r in runs if "error" not in r]
    med = {
        "first_tile_ms": _median([r.get("first_tile_ms") for r in good]),
        "lightweight_display_complete_ms":
            _median([r.get("lightweight_display_complete_ms") for r in good]),
        "final_ms": _median([r.get("final_ms") for r in good]),
        "tile_count": _median([r.get("tile_count") for r in good]),
        "tile_img_count": _median([r.get("tile_img_final") for r in good]),
        "api_folders_ttfb_ms":
            _median([(r.get("api_folders_perf") or {}).get("ttfb") for r in good]),
        "api_folders_dur_ms":
            _median([(r.get("api_folders_perf") or {}).get("dur") for r in good]),
        "api_folders_body_bytes":
            _median([(r.get("api_folders_direct") or {}).get("body_bytes") for r in good]),
        "thumb_count": _median([r.get("thumb_count") for r in good]),
        "thumb_transfer_bytes":
            _median([r.get("thumb_transfer_bytes") for r in good]),
        "thumb_decoded_bytes":
            _median([r.get("thumb_decoded_bytes") for r in good]),
        "w_backend_ms": _median([r.get("_w_backend_ms") for r in good]),
        "w_js_ms": _median([r.get("_w_js_ms") for r in good]),
        "w_thumbs_ms": _median([r.get("_w_thumbs_ms") for r in good]),
    }
    # Top-2 contributors to lightweight display-complete (by window ms).
    windows = [("backend(api/scan+transfer)", med["w_backend_ms"]),
               ("js(parse+restore+render)", med["w_js_ms"]),
               ("thumbs(fetch+settle)", med["w_thumbs_ms"])]
    top2 = sorted([w for w in windows if w[1] is not None],
                  key=lambda w: w[1], reverse=True)[:2]
    out = {"base": BASE, "profile": "A_iphone(390x844,DPR3)", "runs": RUNS,
           "cold_claim": COLD,
           "cold_definition": "viewer restart "
           "(metadata_index_cache=None + folders_body_cache empty, all workers); "
           "run 1 ~= cold only if server was freshly restarted before this script ran",
           "label": "lightweight display-complete = single-cover w=200 all-tile "
           "img.complete (D3); multi follow-up recorded separately",
           "median": med, "top2_contributors": top2, "run_details": runs}
    (OUT_DIR / "waterfall.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False))
    print(f"[waterfall] median lightweight_display_complete_ms={med['lightweight_display_complete_ms']}")
    print(f"[waterfall] median first_tile_ms={med['first_tile_ms']}")
    print(f"[waterfall] median api_folders_ttfb_ms={med['api_folders_ttfb_ms']} "
          f"dur_ms={med['api_folders_dur_ms']} body_bytes={med['api_folders_body_bytes']}")
    print(f"[waterfall] median thumb_count={med['thumb_count']} "
          f"transfer_bytes={med['thumb_transfer_bytes']} decoded_bytes={med['thumb_decoded_bytes']}")
    print(f"[waterfall] top2 contributors: {top2}")
    print(f"[waterfall] wrote {OUT_DIR / 'waterfall.json'}")
    if not good:
        sys.exit("no successful runs")


if __name__ == "__main__":
    main()
