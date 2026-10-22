#!/usr/bin/env python3
"""Measure tile-grid vertical-flick frame gaps on the Android tablet.

Arms a rAF frame-gap + scroll-event probe via CDP on the device Chrome tab,
drives an `adb shell input swipe` series (or waits for a real finger with
--finger), then reports frame-gap percentiles sliced to scroll-active
windows (idle frames between swipes are excluded — they skew p50 toward
idle vsync cadence and hide real jank).

Usage:
    uv run --with websockets python3 flick-fps.py [--url dev-04] [--swipes 8]
    uv run --with websockets python3 flick-fps.py --finger 12   # human drag

Requires: adb forward tcp:9222 localabstract:chrome_devtools_remote
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import urllib.request

import websockets

CDP = "http://localhost:9222/json"

PROBE = r"""(() => {
  const ss = document.getElementById('sidebar-scroll');
  if (!ss) return 'NO-SCROLLER';
  const frames = [], scrolls = [];
  let last = performance.now(), run = true;
  (function loop(){
    const n = performance.now();
    frames.push([n, n - last]);
    last = n;
    if (run) requestAnimationFrame(loop);
  })();
  const onS = () => scrolls.push([performance.now(), ss.scrollTop]);
  ss.addEventListener('scroll', onS, {passive: true});
  window.__fgDump = () => { run = false; return {frames: frames.slice(1), scrolls}; };
  return 'armed ' + Math.round(ss.scrollTop) + '/' + ss.scrollHeight;
})()"""


def pick_serial():
    out = subprocess.run(["adb", "devices"], capture_output=True,
                         text=True).stdout
    devs = [l.split()[0] for l in out.splitlines()[1:]
            if l.split() and l.split()[-1] == "device"]
    if not devs:
        sys.exit("no adb devices — pair/connect wireless ADB first")
    if len(devs) > 1:
        print("note: %d adb devices, using %s (override with --serial)"
              % (len(devs), devs[0]), file=sys.stderr)
    return devs[0]


def adb(serial, *args):
    cmd = ["adb", "-s", serial] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=20)


def pick_tab(match):
    tabs = [t for t in json.load(urllib.request.urlopen(CDP))
            if t["type"] == "page"]
    if not tabs:
        sys.exit("no debuggable Chrome pages — is Chrome running + forwarded?")
    hits = [t for t in tabs if match in t.get("url", "")]
    if not hits:
        sys.exit("no tab matches %r\ntabs:\n%s" % (
            match, "\n".join("  " + t["url"][:100] for t in tabs)))
    if len(hits) > 1:
        print("warning: %d tabs match, using first:" % len(hits),
              file=sys.stderr)
        for t in hits:
            print("  " + t["url"][:100], file=sys.stderr)
    return hits[0]


async def ev(ws, expr, mid):
    await ws.send(json.dumps({
        "id": mid, "method": "Runtime.evaluate",
        "params": {"expression": expr, "returnByValue": True,
                   "awaitPromise": True}}))
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), 120))
        if m.get("id") == mid:
            return m["result"].get("result", {}).get("value")


def swipe_series(serial, n, x, y1, y2, dur_ms, gap_s):
    for _ in range(n):
        adb(serial, "shell", "input", "swipe",
            str(x), str(y1), str(x), str(y2), str(dur_ms))
        # Between-flick settle: fling decays here, so keep it short enough
        # that the next flick still sees a moving list (dense scroll data).
        import time as _t
        _t.sleep(gap_s)


def pct(sorted_vals, q):
    if not sorted_vals:
        return float("nan")
    return sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * q))]


def report(data, settle_ms):
    frames, scrolls = data["frames"], data["scrolls"]
    if not scrolls:
        print("NO SCROLL EVENTS — the swipe did not reach #sidebar-scroll "
              "(wrong tab? banner/dialog covering the grid?)")
        return 1
    st_lo = min(s[1] for s in scrolls)
    st_hi = max(s[1] for s in scrolls)
    print("scrolls: %d events, st %.0f -> %.0f (range %.0f px)"
          % (len(scrolls), st_lo, st_hi, st_hi - st_lo))
    if st_hi - st_lo < 400:
        print("WARN: <400px traversed — the gesture barely moved; "
              "numbers describe a bounce, not a flick")
    # Frames ending within `settle_ms` after a scroll event = scroll-active.
    sts = sorted(s[0] for s in scrolls)
    active = []
    i = 0
    for t_end, dt in frames:
        while i < len(sts) and sts[i] + settle_ms < t_end:
            i += 1
        if i < len(sts) and sts[i] <= t_end <= sts[i] + settle_ms:
            active.append(dt)
    allf = sorted(dt for _, dt in frames)
    act = sorted(active)
    print("scroll-active frames: %d (of %d total)" % (len(act), len(allf)))
    if not act:
        print("no scroll-adjacent frames")
        return 1
    print("  p50 %.1fms  p90 %.1fms  p99 %.1fms  max %.1fms"
          % (pct(act, .5), pct(act, .9), pct(act, .99), act[-1]))
    over33 = sum(1 for v in act if v > 33)
    over16 = sum(1 for v in act if v > 16.7)
    print("  >16.7ms: %d (%.1f%%)   >33ms: %d (%.1f%%)"
          % (over16, 100 * over16 / len(act),
             over33, 100 * over33 / len(act)))
    print("  ~%.0f fps at p50 (90Hz panel: 11.2ms = vsync-bound)"
          % (1000 / pct(act, .5)))
    print("  (all-frames p50 %.1fms — idle-inclusive, for contrast)"
          % pct(allf, .5))
    return 0


async def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=os.environ.get(
        "BOOKQ_TAB_MATCH", os.environ.get("BOOKQ_DEV_HOST", "dev-04")),
        help="substring of the device tab URL (default: BOOKQ_DEV_HOST/dev-04)")
    p.add_argument("--serial", default=None, help="adb device serial")
    p.add_argument("--swipes", type=int, default=8,
                   help="upward input-swipes in the measure pass")
    p.add_argument("--warmup", type=int, default=3,
                   help="warm-up swipes before arming (cold decode skews first pass; 0 to skip)")
    p.add_argument("--ms", type=int, default=700, help="swipe duration ms")
    p.add_argument("--gap", type=float, default=0.25,
                   help="seconds between swipes")
    p.add_argument("--x", type=int, default=700)
    p.add_argument("--y1", type=int, default=1100, help="swipe start y (bottom)")
    p.add_argument("--y2", type=int, default=200, help="swipe end y (top) — "
                   "always keep y1>y2: a DOWNWARD swipe at scrollTop=0 fires "
                   "pull-to-refresh, reloads the page, and kills the probe")
    p.add_argument("--settle", type=int, default=150,
                   help="ms after a scroll event counted as scroll-active")
    p.add_argument("--finger", type=float, default=0,
                   help="seconds to wait for a human drag instead of swiping")
    p.add_argument("--no-top", action="store_true",
                   help="don't reset scrollTop=0 before measuring")
    p.add_argument("--reload", action="store_true",
                   help="hard-reload the tab first (SW stale-JS safety)")
    args = p.parse_args()

    serial = args.serial or pick_serial()
    adb(serial, "forward", "tcp:9222", "localabstract:chrome_devtools_remote")
    tab = pick_tab(args.url)
    print("tab:", tab["url"][:90])
    async with websockets.connect(tab["webSocketDebuggerUrl"],
                                  max_size=50 * 1024 * 1024) as ws:
        mid = 0
        if args.reload:
            await ws.send(json.dumps({"id": 1, "method": "Page.reload",
                                      "params": {"ignoreCache": True}}))
            await asyncio.sleep(6)
        if not args.no_top:
            mid += 1
            await ev(ws, "var s=document.getElementById('sidebar-scroll');"
                     "s&&(s.scrollTop=0); 'top'", mid)
            await asyncio.sleep(0.3)
        if args.warmup:
            print("warm-up: %d swipes" % args.warmup)
            swipe_series(serial, args.warmup, args.x, args.y1, args.y2,
                         args.ms, args.gap)
            await asyncio.sleep(0.5)
            if not args.no_top:
                mid += 1
                await ev(ws, "var s=document.getElementById('sidebar-scroll');"
                         "s&&(s.scrollTop=0); 'top'", mid)
                await asyncio.sleep(0.3)
        mid += 1
        armed = await ev(ws, PROBE, mid)
        print("probe:", armed)
        if armed == "NO-SCROLLER":
            sys.exit("no #sidebar-scroll on this page — open a tile-grid "
                     "browse view first")
        if args.finger:
            print("DRAG NOW: flick the tile grid vertically for ~%.0fs"
                  % args.finger)
            await asyncio.sleep(args.finger)
        else:
            swipe_series(serial, args.swipes, args.x, args.y1, args.y2,
                         args.ms, args.gap)
            await asyncio.sleep(0.8)
        mid += 1
        data = await ev(ws, "window.__fgDump ? window.__fgDump() : null", mid)
        if data is None:
            sys.exit("probe lost — the page reloaded mid-gesture "
                     "(pull-to-refresh? keep swipes upward)")
        sys.exit(report(data, args.settle))


asyncio.run(main())
