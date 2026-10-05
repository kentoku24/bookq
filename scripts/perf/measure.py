"""bookq performance baseline (read-only, Playwright chromium).

Usage: uv run --with playwright --with-requirements requirements-dev.txt python scripts/perf/measure.py [BASE_URL] [--out DIR] [--runs N]
Env: ONLY=A|B|A_cpu4x restricts to a single profile (full keys A_iphone|B_tablet|A_iphone_cpu4x also accepted).
Writes OUT_DIR/baseline.json and OUT_DIR/baseline.md (default OUT_DIR=output/perf-<timestamp>/).
"""
import argparse
import datetime
import json
import os
import statistics
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser(description="bookq perf measurement (Playwright chromium)")
parser.add_argument("base_url", nargs="?", default="http://example-host.local:8080")
parser.add_argument("--out", default=None, help="output dir (default output/perf-<timestamp>/)")
parser.add_argument("--runs", type=int, default=3)
_args = parser.parse_args()
BASE = _args.base_url.rstrip("/")
OUT_DIR = Path(_args.out or f"output/perf-{datetime.datetime.now():%Y%m%d-%H%M}")
RUNS = _args.runs
TURNS = 10
TURN_SPACING_MS = 300

IPHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
IPAD_UA = ("Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
           "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

PROFILES = {
    "A_iphone": dict(viewport={"width": 390, "height": 844}, device_scale_factor=3,
                     is_mobile=True, has_touch=True, user_agent=IPHONE_UA),
    "B_tablet": dict(viewport={"width": 1920, "height": 1200}, device_scale_factor=2,
                     is_mobile=False, has_touch=True, user_agent=IPAD_UA),
}

INIT_SCRIPT = r"""
(() => {
  try { performance.setResourceTimingBufferSize(2000); } catch (e) {}
  window.__cc = {firstTile: null, longtasks: [], rafGaps: 0, rafRunning: false, rafMax: 0};
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) window.__cc.longtasks.push({start: e.startTime, dur: e.duration});
    }).observe({type: 'longtask', buffered: true});
  } catch (e) {}
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
  window.__cc.rafStart = () => {
    window.__cc.rafGaps = 0; window.__cc.rafMax = 0; window.__cc.rafRunning = true;
    let last = performance.now();
    const tick = (t) => {
      const gap = t - last; last = t;
      if (gap > 50) window.__cc.rafGaps++;
      if (gap > window.__cc.rafMax) window.__cc.rafMax = gap;
      if (window.__cc.rafRunning) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  };
  window.__cc.rafStop = () => { window.__cc.rafRunning = false; };
})();
"""


def median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


def resp_info(resp):
    """Size / timing / headers from a Playwright response."""
    req = resp.request
    t = req.timing or {}
    hdrs = resp.headers
    size = None
    try:
        sz = resp.request.sizes() if hasattr(resp.request, "sizes") else None
        if sz:
            size = sz.get("responseBodySize")
    except Exception:
        pass
    if size is None:
        cl = hdrs.get("content-length")
        if cl and cl.isdigit():
            size = int(cl)
    if size is None:
        try:
            size = len(resp.body())
        except Exception:
            size = None
    start = t.get("requestStart", -1)
    end = t.get("responseEnd", -1)
    ttfb = t.get("responseStart", -1)
    return {
        "url": resp.url,
        "status": resp.status,
        "bytes": size,
        "ttfb_ms": round(ttfb - start, 1) if start >= 0 and ttfb >= 0 else None,
        "total_ms": round(end - start, 1) if start >= 0 and end >= 0 else None,
        "cache_control": hdrs.get("cache-control"),
        "etag": hdrs.get("etag"),
        "content_type": hdrs.get("content-type"),
        "content_encoding": hdrs.get("content-encoding"),
    }


def path_of(url):
    return unquote(urlparse(url).path)


def run_once(browser, profile_name, cpu_throttle=None):
    prof = PROFILES[profile_name]
    ctx = browser.new_context(**prof)
    page = ctx.new_page()
    page.add_init_script(INIT_SCRIPT)
    cdp = ctx.new_cdp_session(page)
    if cpu_throttle:
        cdp.send("Emulation.setCPUThrottlingRate", {"rate": cpu_throttle})

    responses = []

    def on_resp(r):
        try:
            responses.append(resp_info(r))
        except Exception as e:  # noqa
            responses.append({"url": r.url, "error": str(e)})

    page.on("response", on_resp)
    result = {"profile": profile_name, "cpu_throttle": cpu_throttle or 1}

    # ---- Step 1: top page ----
    page.goto(BASE + "/", wait_until="commit")
    page.wait_for_function("() => document.querySelector('#tile-grid') && document.querySelector('#tile-grid').children.length > 0", timeout=30000)
    page.wait_for_load_state("load")
    # settle: all tile imgs complete (like tests/test_top_display_perf.py), bounded
    try:
        page.wait_for_function(
            "() => { const imgs=[...document.querySelectorAll('#tile-grid img')]; return imgs.length===0 || imgs.every(i=>i.complete); }",
            timeout=15000)
        tiles_settled = True
    except Exception:
        tiles_settled = False
    page.wait_for_timeout(500)
    nav = page.evaluate("""() => {
      const n = performance.getEntriesByType('navigation')[0];
      const res = performance.getEntriesByType('resource').map(e => ({name: e.name, type: e.initiatorType,
         start: e.startTime, dur: e.duration, transfer: e.transferSize, encoded: e.encodedBodySize, decoded: e.decodedBodySize,
         ttfb: e.responseStart - e.requestStart}));
      return {
        dcl: n ? n.domContentLoadedEventEnd : null,
        load: n ? n.loadEventEnd : null,
        responseEnd: n ? n.responseEnd : null,
        ttfb: n ? n.responseStart : null,
        firstTile: window.__cc.firstTile,
        tileCount: document.querySelectorAll('#tile-grid .tile, #tile-grid .item, #tile-grid .card').length,
        tileImgCount: document.querySelectorAll('#tile-grid img').length,
        longtasks: window.__cc.longtasks.slice(),
        resources: res,
      };
    }""")

    def bucket(name):
        p = path_of(name)
        if p.endswith(".js"):
            return "js"
        if p.endswith(".css"):
            return "css"
        if p.startswith("/thumb/"):
            return "thumb"
        if p.startswith("/image/"):
            return "image"
        if p.startswith("/api/"):
            return "api"
        return "other"

    agg = {}
    for r in nav["resources"]:
        b = bucket(r["name"])
        a = agg.setdefault(b, {"count": 0, "transfer": 0, "decoded": 0})
        a["count"] += 1
        a["transfer"] += r["transfer"] or 0
        a["decoded"] += r["decoded"] or 0
    folders = [r for r in responses if "/api/folders" in r["url"]]
    folders_perf = [r for r in nav["resources"] if "/api/folders" in r["name"]]
    result["top"] = {
        "ttfb_ms": nav["ttfb"], "dcl_ms": nav["dcl"], "load_ms": nav["load"],
        "first_tile_ms": nav["firstTile"], "tiles_settled": tiles_settled,
        "tile_count": nav["tileCount"], "tile_img_count": nav["tileImgCount"],
        "resources_total_count": len(nav["resources"]),
        "resources_total_transfer": sum((r["transfer"] or 0) for r in nav["resources"]),
        "resources_by_type": agg,
        "api_folders": folders,
        "api_folders_perf": folders_perf,
        "longtasks_count": len(nav["longtasks"]),
        "longtasks_total_ms": round(sum(l["dur"] for l in nav["longtasks"]), 1),
        "top_thumbs_sample": [path_of(r["name"]) + ("?" + urlparse(r["name"]).query if urlparse(r["name"]).query else "") for r in nav["resources"] if bucket(r["name"]) == "thumb"][:3],
    }

    # ---- Step 4: static assets ----
    static = {}
    for r in responses:
        p = path_of(r["url"])
        if p.startswith("/static/") or p == "/sw.js":
            static[p] = {k: r.get(k) for k in ("bytes", "total_ms", "cache_control", "etag", "content_encoding", "status")}
    static_perf = {path_of(r["name"]): {"transfer": r["transfer"], "decoded": r["decoded"], "dur": round(r["dur"], 1)}
                   for r in nav["resources"] if bucket(r["name"]) in ("js", "css")}
    result["static"] = {"responses": static, "perf": static_perf,
                        "js_decoded_total": sum(v["decoded"] or 0 for k, v in static_perf.items() if k.endswith(".js")),
                        "js_transfer_total": sum(v["transfer"] or 0 for k, v in static_perf.items() if k.endswith(".js"))}

    # ---- Step 2: folder chain -> viewer ----
    # Generalized (not tied to a specific fixture): click the first folder
    # tile repeatedly, up to 4 levels, until a decoded page image shows up in
    # the viewer strip.
    marker = len(responses)
    clicks = []

    def first_folder_tile():
        return page.locator("#tile-grid .tile[data-type=folder], #tile-grid [data-path][data-type=folder]").first

    def click_tile(locator, label):
        # avoid the offline button: click the tile name text area
        page.evaluate("window.__cc.tClick = performance.now(); window.__cc.gridBefore = document.querySelector('#tile-grid').innerHTML.length")
        locator.click(position={"x": 10, "y": 10})
        clicks.append(label)

    viewer_open = False
    for depth in range(4):
        tile = first_folder_tile()
        if tile.count() == 0:
            break
        label = tile.get_attribute("data-path")
        click_tile(tile, label)
        try:
            page.wait_for_function(
                "() => { const i=[...document.querySelectorAll('#img-track img')].find(x=>x.complete&&x.naturalWidth>0);"
                " if(i){ if(!window.__cc.tFirstImg) window.__cc.tFirstImg=performance.now(); return true;} "
                " const g=document.querySelector('#tile-grid');"
                " return g && g.innerHTML.length !== window.__cc.gridBefore && !location.hash.startsWith('#/view'); }",
                timeout=30000)
        except Exception:
            break
        if page.evaluate("() => !!window.__cc.tFirstImg"):
            viewer_open = True
            break
        page.wait_for_timeout(300)
    page.wait_for_timeout(1500)  # let the prefetch window land
    open_stats = page.evaluate("""() => {
      const imgs=[...document.querySelectorAll('#img-track img')].map(i=>({i: i.style.getPropertyValue('--i'), w:i.naturalWidth, h:i.naturalHeight, complete:i.complete, src:i.getAttribute('src')}));
      return {tClick: window.__cc.tClick, tFirstImg: window.__cc.tFirstImg, hash: location.hash, imgs, counter: (document.getElementById('counter')||{}).textContent};
    }""")
    chain_resps = responses[marker:]
    # Fill in durations from the Performance timeline (response handler fires before responseEnd).
    perf_now = page.evaluate("() => performance.getEntriesByType('resource').map(e => ({name: e.name, dur: e.duration, ttfb: e.responseStart - e.requestStart, transfer: e.transferSize, decoded: e.decodedBodySize}))")
    perf_by_url = {}
    for e in perf_now:
        perf_by_url.setdefault(e["name"], e)  # first occurrence
    for r in responses:
        e = perf_by_url.get(r["url"])
        if e:
            if r.get("total_ms") is None:
                r["total_ms"] = round(e["dur"], 1)
            if r.get("ttfb_ms") is None:
                r["ttfb_ms"] = round(e["ttfb"], 1)
            if r.get("bytes") is None:
                r["bytes"] = e["decoded"]
    for f, e in zip(result["top"]["api_folders"], result["top"]["api_folders_perf"]):
        if f.get("total_ms") is None:
            f["total_ms"] = round(e["dur"], 1)
    api_images = [r for r in chain_resps if "/api/images" in r["url"]]
    img_resps = [r for r in chain_resps if path_of(r["url"]).startswith("/image/")]
    first_img_resp = None
    if open_stats["imgs"]:
        # --i が "0" のセルが無い（進捗復元で途中ページから開いた）ときは、DOM 順ではなく最小 index を採る
        def _idx(i):
            try:
                return int(i["i"])
            except (TypeError, ValueError):
                return 1 << 30
        first_src = next((i["src"] for i in open_stats["imgs"] if i["i"] == "0"), min(open_stats["imgs"], key=_idx)["src"])
        first_img_resp = next((r for r in img_resps if path_of(r["url"]) == path_of(BASE + first_src)), None)
    # first 5 page images (--i 0..4)
    first5 = []
    for i in range(5):
        el = next((x for x in open_stats["imgs"] if x["i"] == str(i)), None)
        if not el:
            continue
        rr = next((r for r in img_resps if path_of(r["url"]) == path_of(BASE + el["src"])), None)
        first5.append({"idx": i, "natural": f"{el['w']}x{el['h']}", "bytes": rr["bytes"] if rr else None,
                       "ms": rr["total_ms"] if rr else None, "content_type": rr["content_type"] if rr else None,
                       "path": path_of(BASE + el["src"])})
    result["open"] = {
        "clicks": clicks, "viewer_open": viewer_open, "hash": open_stats["hash"],
        "click_to_first_img_ms": (open_stats["tFirstImg"] - open_stats["tClick"]) if open_stats["tFirstImg"] else None,
        "api_images": api_images,
        "image_requests": len(img_resps), "image_bytes": sum((r["bytes"] or 0) for r in img_resps),
        "first_image": first_img_resp, "first5": first5,
        "images_in_track": len(open_stats["imgs"]),
    }

    # ---- Step 3: page turns ----
    turns = []
    if viewer_open:
        page.evaluate("() => { window.__cc.longtasks = []; window.__cc.rafStart(); }")
        # ensure key focus on body (toolbar might have focus)
        page.evaluate("() => document.activeElement && document.activeElement.blur && document.activeElement.blur()")
        for n in range(TURNS):
            # `#counter` is written synchronously in snapTo ("<idx+1> / N"); the hash lags 200 ms (scheduleUrlSync debounce).
            before = page.evaluate("() => { const m=(document.getElementById('counter').textContent||'').match(/(\\d+)\\s*\\//); window.__cc.tKey=performance.now(); return m?Number(m[1])-1:null; }")
            page.keyboard.press("ArrowLeft")
            try:
                page.wait_for_function(
                    "(before) => { const m=(document.getElementById('counter').textContent||'').match(/(\\d+)\\s*\\//); const p=m?Number(m[1])-1:null; if(p===null||p===before) return false;"
                    " const img=[...document.querySelectorAll('#img-track img')].find(x=>x.style.getPropertyValue('--i')===String(p));"
                    " if(img && img.complete && img.naturalWidth>0){ window.__cc.tLoaded=performance.now(); window.__cc.pNew=p; return true;} return false; }",
                    arg=before, timeout=15000)
                t = page.evaluate("() => ({ms: window.__cc.tLoaded - window.__cc.tKey, p: window.__cc.pNew})")
                turns.append({"n": n, "from": before, "to": t["p"], "ms": round(t["ms"], 1)})
            except Exception as e:
                turns.append({"n": n, "from": before, "to": None, "ms": None, "error": str(e)[:80]})
            page.wait_for_timeout(TURN_SPACING_MS)
        page.wait_for_timeout(400)
        lt = page.evaluate("() => { window.__cc.rafStop(); return {longtasks: window.__cc.longtasks, rafGaps: window.__cc.rafGaps, rafMax: window.__cc.rafMax, cells: document.querySelectorAll('#img-track .page-cell').length}; }")
        result["turns"] = {
            "per_turn": turns,
            "median_ms": median([t["ms"] for t in turns]),
            "max_ms": max([t["ms"] for t in turns if t["ms"] is not None], default=None),
            "longtasks_count": len(lt["longtasks"]),
            "longtasks_total_ms": round(sum(l["dur"] for l in lt["longtasks"]), 1),
            "raf_gaps_gt50ms": lt["rafGaps"], "raf_max_gap_ms": round(lt["rafMax"], 1),
            "page_cells_after": lt["cells"],
            "image_requests_during_turns": len([r for r in responses[marker + len(chain_resps):] if path_of(r["url"]).startswith("/image/")]),
        }
    ctx.close()
    return result


def summarize(runs):
    """Median over runs for the headline numbers."""
    def med(path):
        vals = []
        for r in runs:
            v = r
            for k in path:
                v = v.get(k) if isinstance(v, dict) else None
                if v is None:
                    break
            vals.append(v)
        return median(vals)
    s = {
        "ttfb_ms": med(["top", "ttfb_ms"]), "dcl_ms": med(["top", "dcl_ms"]), "load_ms": med(["top", "load_ms"]),
        "first_tile_ms": med(["top", "first_tile_ms"]),
        "top_resources_count": med(["top", "resources_total_count"]),
        "top_resources_transfer": med(["top", "resources_total_transfer"]),
        "top_longtasks": med(["top", "longtasks_count"]),
        "api_folders_bytes": median([r["top"]["api_folders"][0]["bytes"] if r["top"]["api_folders"] else None for r in runs]),
        "api_folders_ttfb_ms": median([r["top"]["api_folders"][0]["ttfb_ms"] if r["top"]["api_folders"] else None for r in runs]),
        "api_folders_total_ms": median([r["top"]["api_folders"][0]["total_ms"] if r["top"]["api_folders"] else None for r in runs]),
        "click_to_first_img_ms": med(["open", "click_to_first_img_ms"]),
        "open_image_requests": med(["open", "image_requests"]), "open_image_bytes": med(["open", "image_bytes"]),
        "first_image_bytes": median([(r["open"].get("first_image") or {}).get("bytes") for r in runs]),
        "first_image_ms": median([(r["open"].get("first_image") or {}).get("total_ms") for r in runs]),
        "turn_median_ms": med(["turns", "median_ms"]), "turn_max_ms": med(["turns", "max_ms"]),
        "turn_longtasks": med(["turns", "longtasks_count"]), "turn_raf_gaps": med(["turns", "raf_gaps_gt50ms"]),
        "js_decoded_total": med(["static", "js_decoded_total"]),
    }
    return s


def fmt_kb(v):
    return f"{round(v / 1024):,} KB" if isinstance(v, (int, float)) else "-"


def fmt_ms(v):
    return f"{v:g}" if isinstance(v, (int, float)) else "-"


MD_ROWS = [
    ("Nav TTFB (ms)", "ttfb_ms", fmt_ms),
    ("DOMContentLoaded (ms)", "dcl_ms", fmt_ms),
    ("load (ms)", "load_ms", fmt_ms),
    ("First tile rendered (ms)", "first_tile_ms", fmt_ms),
    ("Top: resource count", "top_resources_count", fmt_ms),
    ("Top: total transfer", "top_resources_transfer", fmt_kb),
    ("Top: long tasks (count)", "top_longtasks", fmt_ms),
    ("/api/folders size", "api_folders_bytes", fmt_kb),
    ("/api/folders TTFB (ms)", "api_folders_ttfb_ms", fmt_ms),
    ("/api/folders total (ms)", "api_folders_total_ms", fmt_ms),
    ("Open leaf: click -> first img (ms)", "click_to_first_img_ms", fmt_ms),
    ("Open leaf: image requests", "open_image_requests", fmt_ms),
    ("Open leaf: image bytes", "open_image_bytes", fmt_kb),
    ("First page image bytes", "first_image_bytes", fmt_kb),
    ("First page image fetch (ms)", "first_image_ms", fmt_ms),
    ("Page turn: median (ms, to loaded img)", "turn_median_ms", fmt_ms),
    ("Page turn: max (ms)", "turn_max_ms", fmt_ms),
    ("Page turn: long tasks", "turn_longtasks", fmt_ms),
    ("Page turn: rAF gaps >50ms", "turn_raf_gaps", fmt_ms),
    ("JS total (decoded)", "js_decoded_total", fmt_kb),
]


def write_markdown(out, path):
    keys = list(out["variants"].keys())
    lines = [
        "# bookq performance baseline",
        "",
        f"Target: `{out['base']}` ({out['runs']} runs per variant, medians shown; Playwright chromium).",
        "",
        "| Metric | " + " | ".join(keys) + " |",
        "|---|" + "|".join(["---"] * len(keys)) + "|",
    ]
    for label, field, fmt in MD_ROWS:
        cells = []
        for k in keys:
            median_vals = (out["variants"][k] or {}).get("median") or {}
            cells.append(fmt(median_vals.get(field)))
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines.append("")
    path.write_text("\n".join(lines))


VARIANTS = [("A_iphone", None), ("B_tablet", None), ("A_iphone", 4)]
VARIANT_KEYS = [prof + (f"_cpu{cpu}x" if cpu else "") for prof, cpu in VARIANTS]
ONLY_ALIASES = {"A": "A_iphone", "B": "B_tablet", "A_cpu4x": "A_iphone_cpu4x"}


def resolve_only():
    """Resolve $ONLY (short alias or full profile key) before any browser work.

    Exits non-zero immediately on an unrecognized value so a typo doesn't
    silently produce an empty table.
    """
    raw = os.environ.get("ONLY")
    if not raw:
        return None
    only = ONLY_ALIASES.get(raw, raw)
    if only not in VARIANT_KEYS:
        sys.exit(f"ONLY={raw!r} matched no profile; choose one of A, B, A_cpu4x "
                  f"(or full keys: {', '.join(VARIANT_KEYS)})")
    return only


def main():
    out = {"base": BASE, "runs": RUNS, "variants": {}}
    only = resolve_only()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        baseline_json = OUT_DIR / "baseline.json"
        prev = json.loads(baseline_json.read_text()) if baseline_json.exists() else {}
        out["variants"] = prev.get("variants", {}) if only else {}
        for prof, cpu in VARIANTS:
            key = prof + (f"_cpu{cpu}x" if cpu else "")
            if only and key != only:
                continue
            runs = []
            for i in range(RUNS):
                print(f"[{key}] run {i+1}/{RUNS}", flush=True)
                try:
                    runs.append(run_once(browser, prof, cpu))
                except Exception as e:
                    print(f"  ERROR: {e!r}", flush=True)
                    runs.append({"error": repr(e)})
            good = [r for r in runs if "error" not in r]
            out["variants"][key] = {"runs": runs, "median": summarize(good) if good else None}
            baseline_json.write_text(json.dumps(out, indent=2, ensure_ascii=False))
        browser.close()
    baseline_json.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    write_markdown(out, OUT_DIR / "baseline.md")
    print("done")


if __name__ == "__main__":
    main()
