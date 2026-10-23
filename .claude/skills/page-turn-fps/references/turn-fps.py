#!/usr/bin/env python3
"""Measure page-turn animation frame gaps on the Android tablet — no hands.

Arms a rAF frame-gap + touch/idx probe inside the bookq viewer via CDP,
fires synthetic `adb shell input` taps/swipes, then reports frame-gap
percentiles sliced to per-gesture turn windows (touchstart -> touchend +
settle). Works for both readers: pass --reader strip|ring to navigate and
verify `window.__bookqReader` before measuring.

Usage:
    uv run --with websockets python3 turn-fps.py --url dev-01.javanese \
        --reader ring
    uv run --with websockets python3 turn-fps.py --url dev-01.javanese \
        --reader strip --taps 5 --swipes 5 --finger 15

Interpretation: 90Hz panel = 11.1ms vsync. Gap >22.3ms = >=1 missed vsync,
>33.4ms = >=2. rAF gaps measure produced-frame cadence (renderer side);
compositor-only drops still need screenrecord — see SKILL.md step 5.
"""
import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

try:
    import websockets
except ImportError:
    sys.exit("websockets missing — run via: "
             "uv run --with websockets python3 turn-fps.py ...")

CDP = "http://localhost:9222/json"

PROBE = r"""(() => {
  const track = document.getElementById('img-container');
  if (!track) return 'NO-VIEWER';
  if (window.__tf) return 'already-armed';
  const frames = [], touches = [], idxs = [];
  let last = performance.now(), run = true, lastIdx = currentIdx;
  (function loop(){
    const n = performance.now();
    frames.push([n, n - last]);
    if (currentIdx !== lastIdx) { idxs.push([n, lastIdx, currentIdx]); lastIdx = currentIdx; }
    last = n;
    if (run) requestAnimationFrame(loop);
  })();
  for (const type of ['touchstart','touchmove','touchend','touchcancel']) {
    const fn = e => { if (document.getElementById('img-container').contains(e.target))
                        touches.push({type, t: e.timeStamp}); };
    track.addEventListener(type, fn, {passive:true, capture:true});
  }
  window.__tf = { dump(){ run=false; return {frames:frames.slice(1), touches, idxs}; } };
  return 'armed idx=' + currentIdx;
})()"""


def _adb_devices():
    out = subprocess.run(["adb", "devices"], capture_output=True,
                         text=True, timeout=15).stdout
    return [l.split()[0] for l in out.splitlines()[1:]
            if l.split() and l.split()[-1] == "device"]


def _mdns_reconnect():
    try:
        out = subprocess.run(["adb", "mdns", "services"], capture_output=True,
                             text=True, timeout=15).stdout
    except Exception:
        return 0
    n = 0
    for line in out.splitlines():
        cols = line.split()
        if len(cols) >= 3 and "_adb-tls-connect._tcp" in cols[-2]:
            try:
                subprocess.run(["adb", "connect", cols[-1]],
                               capture_output=True, timeout=15)
                n += 1
            except Exception:
                pass
    return n


def pick_serial():
    if not shutil.which("adb"):
        sys.exit("adb not on PATH — install platform-tools")
    devs = _adb_devices()
    if not devs:
        if _mdns_reconnect():
            print("note: reconnected via mDNS", file=sys.stderr)
            devs = _adb_devices()
    if not devs:
        sys.exit("no adb devices — pair wireless debugging on the tablet")
    if len(devs) > 1:
        print("note: %d adb transports, using %s (override with --serial)"
              % (len(devs), devs[0]), file=sys.stderr)
    return devs[0]


def adb(serial, *args):
    return subprocess.run(["adb", "-s", serial] + list(args),
                          capture_output=True, text=True, timeout=20)


def pick_tab(match):
    pages = json.load(urllib.request.urlopen(CDP, timeout=10))
    tabs = [t for t in pages if t["type"] == "page"]
    hits = [t for t in tabs if match in t.get("url", "")]
    if not hits:
        sys.exit("no tab matches %r\ntabs:\n%s" % (
            match, "\n".join("  " + t["url"][:100] for t in tabs)))
    if len(hits) > 1:
        print("warning: %d tabs match, using first" % len(hits),
              file=sys.stderr)
    return hits[0]


def check_awake(serial):
    try:
        out = adb(serial, "shell", "dumpsys", "power").stdout
        for line in out.splitlines():
            if "mWakefulness=" in line and line.split("=")[-1].strip() != "Awake":
                print("WARN: tablet wakefulness=%s — input may hit the lock "
                      "screen" % line.split("=")[-1].strip(), file=sys.stderr)
                return
    except Exception:
        pass


async def ev(ws, expr, mid):
    await ws.send(json.dumps({
        "id": mid, "method": "Runtime.evaluate",
        "params": {"expression": expr, "returnByValue": True,
                   "awaitPromise": True}}))
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), 120))
        if m.get("id") == mid:
            return m["result"].get("result", {}).get("value")


def pct(sorted_vals, q):
    if not sorted_vals:
        return float("nan")
    return sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * q))]


def gestures(data):
    """Pair touchstart -> touchend/cancel into gesture windows."""
    out, cur = [], None
    for ev_ in sorted(data["touches"], key=lambda e: e["t"]):
        if ev_["type"] == "touchstart":
            if cur:
                out.append(cur)
            cur = {"start": ev_["t"], "end": None, "moves": 0}
        elif cur and cur["end"] is None:
            if ev_["type"] == "touchmove":
                cur["moves"] += 1
            else:
                cur["end"] = ev_["t"]
                cur["cancelled"] = ev_["type"] == "touchcancel"
                out.append(cur)
                cur = None
    if cur:
        out.append(cur)
    return out


def report(data, settle_ms):
    gs = gestures(data)
    if not gs:
        print("NO GESTURES — input did not reach #img-container")
        return 1
    frames, idxs = data["frames"], data["idxs"]
    print("gestures: %d, idx changes: %s"
          % (len(gs), [(a, b) for _, a, b in idxs]))
    if len(idxs) < len(gs) // 2:
        print("WARN: few page turns registered — gestures may have missed "
              "the tap zones or landed on a non-viewer surface")
    groups = {"tap": [], "swipe": []}
    for g in gs:
        kind = "swipe" if g.get("moves", 0) > 0 else "tap"
        w0, w1 = g["start"], (g["end"] or g["start"]) + settle_ms
        gaps = [dt for t, dt in frames if w0 <= t <= w1]
        if len(gaps) >= 3:
            groups[kind].append({"dur": w1 - w0, "gaps": gaps})
    for kind, rows in groups.items():
        if not rows:
            continue
        all_gaps = sorted(dt for r in rows for dt in r["gaps"])
        over22 = sum(1 for v in all_gaps if v > 22.3)
        over33 = sum(1 for v in all_gaps if v > 33.4)
        n = len(all_gaps)
        print("%s: %d gestures, %d frames" % (kind, len(rows), n))
        print("  p50 %.1fms  p90 %.1fms  p99 %.1fms  max %.1fms"
              % (pct(all_gaps, .5), pct(all_gaps, .9),
                 pct(all_gaps, .99), all_gaps[-1]))
        print("  >22.3ms(>=1 miss): %d (%.1f%%)  >33.4ms(>=2): %d (%.1f%%)"
              % (over22, 100*over22/n, over33, 100*over33/n))
        print("  ~%.0f fps at p50" % (1000/pct(all_gaps, .5)))
    return 0


async def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=os.environ.get(
        "BOOKQ_TAB_MATCH", os.environ.get("BOOKQ_DEV_HOST", "dev-01")),
        help="tab URL substring (default: BOOKQ_DEV_HOST/dev-01)")
    p.add_argument("--serial", default=None)
    p.add_argument("--reader", choices=["strip", "ring"], default=None,
                   help="navigate to force this reader and verify")
    p.add_argument("--taps", type=int, default=5, help="taps per direction")
    p.add_argument("--swipes", type=int, default=5, help="swipes per direction")
    p.add_argument("--warmup", type=int, default=4, help="warm-up turns")
    p.add_argument("--settle", type=int, default=400,
                   help="ms after touchend counted as turn-active")
    p.add_argument("--finger", type=float, default=0,
                   help="seconds to wait for a real finger instead")
    args = p.parse_args()

    serial = args.serial or pick_serial()
    check_awake(serial)
    fw = adb(serial, "forward", "tcp:9222",
             "localabstract:chrome_devtools_remote")
    if fw.returncode != 0:
        sys.exit("adb forward failed: " + (fw.stderr or fw.stdout).strip())
    tab = pick_tab(args.url)
    print("tab:", tab["url"][:90])
    async with websockets.connect(tab["webSocketDebuggerUrl"],
                                  max_size=50*1024*1024) as ws:
        mid = [0]
        async def ev_(expr):
            mid[0] += 1
            return await ev(ws, expr, mid[0])
        if args.reader == "ring":
            await ev_("location.href = location.pathname + "
                      "'?perf=0&reader=ring&r=' + Date.now() + location.hash")
            await asyncio.sleep(6)
        elif args.reader == "strip":
            await ev_("location.href = location.pathname + '?perf=0&r=' "
                      "+ Date.now() + location.hash")
            await asyncio.sleep(6)
        if args.reader:
            mode = await ev_("window.__bookqReader||'strip'")
            if mode != args.reader:
                sys.exit("reader mode %r != requested %r (SW stale JS?)"
                         % (mode, args.reader))
        state = json.loads(await ev_(
            "JSON.stringify({idx:currentIdx,count:images.length,"
            "w:innerWidth,h:innerHeight,dpr:devicePixelRatio,"
            "screenH:screen.height,reader:window.__bookqReader||'strip'})"))
        print("state:", {k: state[k] for k in ("idx", "count", "reader")})
        y = (state["screenH"] - state["h"] + state["h"] * 0.52) * state["dpr"]
        x_tap = [state["w"]*0.17*state["dpr"], state["w"]*0.83*state["dpr"]]
        x_sw = [state["w"]*0.30*state["dpr"], state["w"]*0.70*state["dpr"]]

        def tap(x):
            adb(serial, "shell", "input", "tap",
                str(round(x)), str(round(y)), "65")
        def swipe(x0, x1):
            adb(serial, "shell", "input", "swipe",
                str(round(x0)), str(round(y)), str(round(x1)),
                str(round(y)), "150")

        print("warm-up: %d turns" % args.warmup)
        for i in range(args.warmup):
            tap(x_tap[i % 2])
            await asyncio.sleep(1.1)
        armed = await ev_(PROBE)
        print("probe:", armed)
        if armed in ("NO-VIEWER", None):
            sys.exit("no #img-container — open a book in the viewer first")
        if args.finger:
            print("DRAG NOW: %ss" % args.finger)
            await asyncio.sleep(args.finger)
        else:
            for _ in range(args.taps):
                tap(x_tap[0]); await asyncio.sleep(1.3)
            for _ in range(args.taps):
                tap(x_tap[1]); await asyncio.sleep(1.3)
            for _ in range(args.swipes):
                swipe(x_sw[0], x_sw[1]); await asyncio.sleep(1.5)
            for _ in range(args.swipes):
                swipe(x_sw[1], x_sw[0]); await asyncio.sleep(1.5)
            await asyncio.sleep(0.8)
        data = await ev_("window.__tf ? window.__tf.dump() : null")
        if data is None:
            sys.exit("probe lost — page reloaded mid-run")
        sys.exit(report(data, args.settle))


asyncio.run(main())
