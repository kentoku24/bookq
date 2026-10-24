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

Interpretation: 90Hz panel = 11.1ms vsync. Gap >16.7ms = >=1 missed vsync,
>27.8ms = >=2 (midpoints between the 11.1/22.2/33.3ms gap plateaus). rAF
gaps measure produced-frame cadence (renderer side); compositor-only drops
still need screenrecord — see SKILL.md step 5.
"""
import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import urllib.request

try:
    import websockets
except ImportError:
    sys.exit("websockets missing — run via: "
             "uv run --with websockets python3 turn-fps.py ...")

CDP = "http://localhost:9222/json"

PROBE = r"""(() => {
  const track = document.getElementById('img-container');
  try {
    // #img-container is static markup — it exists on browse pages too, so
    // it is not a viewer check. Require the view hash AND loaded images.
    if (!track || !location.hash.startsWith('#/view/') || !images.length)
      return 'NO-VIEWER';
  } catch (e) { return 'NO-VIEWER'; }
  // Re-arm cleanly on repeat runs — a stale probe would report last run's
  // frames plus both runs' touches.
  try { if (window.__tf && window.__tf.unarm) window.__tf.unarm(); } catch(e) {}
  delete window.__tf;
  const frames = [], touches = [], idxs = [], fns = [];
  let last = null, run = true, lastIdx = currentIdx;
  function loop(ts){
    if (last !== null) frames.push([ts, ts - last]);
    last = ts;
    if (currentIdx !== lastIdx) { idxs.push([ts, lastIdx, currentIdx]); lastIdx = currentIdx; }
    if (run) requestAnimationFrame(loop);
  }
  requestAnimationFrame(loop);
  for (const type of ['touchstart','touchmove','touchend','touchcancel']) {
    const fn = e => { if (track.contains(e.target))
                        touches.push({type, t: e.timeStamp}); };
    track.addEventListener(type, fn, {passive:true, capture:true});
    fns.push([type, fn]);
  }
  window.__tf = {
    dump(){ run = false; return {frames, touches, idxs}; },
    unarm(){ run = false; for (const [t, f] of fns) track.removeEventListener(t, f, true); }
  };
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
        sys.exit(
            "no adb devices.\n"
            "  If wireless ADB was paired before: check the tablet is awake "
            "and on the same LAN, then `adb connect <tablet-ip>:<port>`.\n"
            "  If never paired: on the tablet — 開発者向けオプション → "
            "ワイヤレスデバッグ → ペア設定, then `adb pair <ip:port>` + the "
            "on-screen code. Ask the user for the code — never guess.")
    if len(devs) > 1:
        print("note: %d adb transports, using %s (override with --serial)"
              % (len(devs), devs[0]), file=sys.stderr)
    return devs[0]


def adb(serial, *args):
    return subprocess.run(["adb", "-s", serial] + list(args),
                          capture_output=True, text=True, timeout=20)


def pick_tab(match):
    try:
        pages = json.load(urllib.request.urlopen(CDP, timeout=10))
    except Exception:
        sys.exit("cannot reach http://localhost:9222/json — Chrome must be "
                 "running on the tablet AND `adb forward tcp:9222 "
                 "localabstract:chrome_devtools_remote` must succeed "
                 "(script tries it, but the socket name only exists while "
                 "Chrome is running)")
    tabs = [t for t in pages if t["type"] == "page"]
    if not tabs:
        sys.exit("no debuggable Chrome pages — is Chrome running on the "
                 "tablet and on the bookq tab?")
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
        if m.get("id") != mid:
            continue
        # Protocol-level failure ("Execution context was destroyed" while a
        # navigation is in flight, etc.) — a bare KeyError on m["result"]
        # would otherwise escape cli()'s RuntimeError catch as a traceback.
        if "error" in m:
            err = m["error"]
            raise RuntimeError("CDP error %s: %s"
                               % (err.get("code"), err.get("message")))
        r = m["result"]
        if "exceptionDetails" in r:
            ed = r["exceptionDetails"]
            desc = (ed.get("exception") or {}).get("description") \
                or ed.get("text") or json.dumps(ed)
            raise RuntimeError("page eval failed: %s" % desc[:300])
        return r.get("result", {}).get("value")


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


def report(data, settle_ms, expect_turns):
    gs = gestures(data)
    if not gs:
        print("NO GESTURES — input did not reach #img-container")
        return 1
    frames, idxs = data["frames"], data["idxs"]
    print("gestures: %d, idx changes: %s"
          % (len(gs), [(a, b) for _, a, b in idxs]))
    groups = {"tap": [], "swipe": []}
    skipped = 0
    for g in gs:
        w0, w1 = g["start"], (g["end"] or g["start"]) + settle_ms
        # A gesture that produced no page turn must not count — its window
        # would show clean ~11ms vsync gaps and flatter the tail. Synthetic
        # runs command a turn per gesture, so a miss means bad conditions
        # (missed zone, chapter boundary, viewer exited).
        if not any(w0 - 50 <= t <= w1 for t, _, _ in idxs):
            skipped += 1
            continue
        kind = "swipe" if g.get("moves", 0) > 0 else "tap"
        gaps = [dt for t, dt in frames if w0 <= t <= w1]
        if len(gaps) >= 3:
            groups[kind].append({"dur": w1 - w0, "gaps": gaps})
    if skipped:
        print("skipped: %d gestures produced no page turn" % skipped)
    for kind, rows in groups.items():
        if not rows:
            continue
        all_gaps = sorted(dt for r in rows for dt in r["gaps"])
        over1 = sum(1 for v in all_gaps if v > 16.7)
        over2 = sum(1 for v in all_gaps if v > 27.8)
        n = len(all_gaps)
        print("%s: %d gestures, %d frames" % (kind, len(rows), n))
        print("  p50 %.1fms  p90 %.1fms  p99 %.1fms  max %.1fms"
              % (pct(all_gaps, .5), pct(all_gaps, .9),
                 pct(all_gaps, .99), all_gaps[-1]))
        print("  >16.7ms(>=1 miss): %d (%.1f%%)  >27.8ms(>=2): %d (%.1f%%)"
              % (over1, 100*over1/n, over2, 100*over2/n))
        print("  ~%.0f fps at p50" % (1000/pct(all_gaps, .5)))
    if skipped and expect_turns:
        print("FAIL: synthetic gestures did not all turn — conditions bad, "
              "results incomplete")
        return 1
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
    # A swipe commit lands ~260ms after touchend — a shorter settle window
    # would call committed swipes "no turn" and drop them from the results.
    if args.settle < 300:
        print("note: --settle %dms is below the ~260ms swipe commit — "
              "clamping to 300ms" % args.settle, file=sys.stderr)
        args.settle = 300

    serial = args.serial or pick_serial()
    check_awake(serial)
    fw = adb(serial, "forward", "tcp:9222",
             "localabstract:chrome_devtools_remote")
    if fw.returncode != 0:
        sys.exit("adb forward failed: " + (fw.stderr or fw.stdout).strip() +
                 "\n  the chrome_devtools_remote socket exists only while "
                 "Chrome is running on the tablet — start Chrome and retry")
    tab = pick_tab(args.url)
    print("tab:", tab["url"][:90])
    async with websockets.connect(tab["webSocketDebuggerUrl"],
                                  max_size=50*1024*1024) as ws:
        mid = [0]
        async def ev_(expr):
            mid[0] += 1
            return await ev(ws, expr, mid[0])

        async def wait_viewer(reader=None):
            """Poll until a book is open (and, if given, on `reader`)."""
            want = ("(window.__bookqReader||'strip')==='%s'" % reader
                    if reader else "true")
            for _ in range(60):
                try:
                    ready = await ev_(
                        "(()=>{try{return location.hash.startsWith('#/view/')"
                        "&&images.length>0&&%s}catch(e){return false}})()"
                        % want)
                except RuntimeError:
                    # The eval raced the navigation — the execution context
                    # was destroyed mid-poll. That IS the "still loading"
                    # state; keep polling instead of dying on a traceback.
                    ready = False
                if ready:
                    return True
                await asyncio.sleep(0.5)
            return False

        if args.reader:
            # Always pass an explicit reader= value — without one the app's
            # localStorage bookq_reader flag decides, which would silently
            # A/B against the wrong renderer after a harness toggle.
            await ev_("location.href = location.pathname + '?perf=0&reader="
                      "%s&r=' + Date.now() + location.hash" % args.reader)
            if not await wait_viewer(args.reader):
                sys.exit("viewer did not come up as %r within 30s "
                         "(SW stale JS? book hash 404?)" % args.reader)

        # Gate on a real viewer BEFORE any input — warmup taps on a browse
        # page would open tiles or trigger other side effects.
        if not await ev_("location.hash.startsWith('#/view/')"):
            sys.exit("tab is not on a #/view/ page — open a book first")

        state = json.loads(await ev_(
            "JSON.stringify({idx:currentIdx,count:images.length,"
            "w:innerWidth,h:innerHeight,dpr:devicePixelRatio,"
            "screenH:screen.height,reader:window.__bookqReader||'strip'})"))
        if not state["count"]:
            sys.exit("viewer open but 0 images — wait for the book to load")

        # The HUD measurably changes results and ?perf=0 cannot override a
        # persisted localStorage flag — clear it once and reload.
        if await ev_("!!document.getElementById('perf-overlay')"):
            print("note: perf HUD active — clearing bookq_perf, reloading",
                  file=sys.stderr)
            await ev_("localStorage.removeItem('bookq_perf'); "
                      "location.reload(); 'reloading'")
            if not await wait_viewer(args.reader):
                sys.exit("viewer did not return after HUD-clear reload")
            if await ev_("!!document.getElementById('perf-overlay')"):
                sys.exit("perf HUD still present — cannot measure clean")
            state = json.loads(await ev_(
                "JSON.stringify({idx:currentIdx,count:images.length,"
                "w:innerWidth,h:innerHeight,dpr:devicePixelRatio,"
                "screenH:screen.height,reader:window.__bookqReader||'strip'})"))
        print("state:", {k: state[k] for k in ("idx", "count", "reader")})

        # screen.height - innerHeight includes BOTH top and bottom system
        # bars, so the naive offset taps too low. Calibrate with one tap and
        # read back clientY (fired before the probe arms — not recorded).
        y = (state["screenH"] - state["h"] + state["h"] * 0.52) * state["dpr"]
        x_tap = [state["w"]*0.17*state["dpr"], state["w"]*0.83*state["dpr"]]
        x_sw = [state["w"]*0.30*state["dpr"], state["w"]*0.70*state["dpr"]]

        def send(*a):
            r = adb(serial, "shell", "input", *a)
            if r.returncode != 0:
                sys.exit("adb input failed: " + (r.stderr or r.stdout).strip())

        await ev_("window.__cal = new Promise(r => {"
                  "const f = e => {removeEventListener('touchstart', f, true);"
                  "r({x: e.touches[0].clientX, y: e.touches[0].clientY})};"
                  "addEventListener('touchstart', f, {once:true, capture:true, passive:true});"
                  "setTimeout(() => r(null), 4000)}); 'cal-armed'")
        send("tap", str(round(x_tap[0])), str(round(y)))
        cal = await ev_("window.__cal")
        if cal:
            dy = (state["h"] * 0.52 - cal["y"]) * state["dpr"]
            if abs(dy) > 1:
                print("note: tap-y corrected by %.0fpx" % dy, file=sys.stderr)
                y += dy
        else:
            print("WARN: calibration tap did not land — y may be off",
                  file=sys.stderr)

        def tap(x):
            # `input tap` takes no duration arg (extras are ignored on
            # Android 11+, an error before). Same-point swipe gives a real
            # 65ms hold and still emits no touchmove — verified on CP08-J1.
            send("swipe", str(round(x)), str(round(y)),
                 str(round(x)), str(round(y)), "65")
        def swipe(x0, x1):
            send("swipe", str(round(x0)), str(round(y)),
                 str(round(x1)), str(round(y)), "150")

        print("warm-up: %d turns" % args.warmup)
        for i in range(args.warmup):
            tap(x_tap[i % 2])
            await asyncio.sleep(1.1)
        armed = await ev_(PROBE)
        print("probe:", armed)
        if armed in ("NO-VIEWER", None):
            sys.exit("viewer lost before probing — open a book and retry")
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
        sys.exit(report(data, args.settle, not args.finger))


def cli():
    try:
        asyncio.run(main())
    except RuntimeError as e:
        sys.exit(str(e))


cli()
