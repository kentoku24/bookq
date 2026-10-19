#!/usr/bin/env python3
"""Sample the bookq perf HUD (#perf-fps textContent) while input runs.

A single post-gesture read misses the fling — Chrome settles back to ~90fps
in under a second. This polls the HUD ~every 550ms in-page and returns the
series, so run it in the background while `adb shell input swipe` fires.

Usage:
    uv run --with websockets python3 cdp-hud-sample.py [n_samples] [url-substring] &
    sleep 1
    for i in 1 2 3 4 5 6 7 8; do
        adb shell input swipe 600 1300 600 400 90; sleep 0.45
    done

Output: JSON array of HUD strings, one per ~550ms window.
"""
import asyncio, json, os, sys, urllib.request
import websockets

CDP = "http://localhost:9222/json"


def pick_tab(match):
    tabs = [t for t in json.load(urllib.request.urlopen(CDP))
            if t["type"] == "page"]
    hits = [t for t in tabs if match in t.get("url", "")]
    if not hits:
        sys.exit("no tab matches %r" % match)
    return hits[0]


async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    match = sys.argv[2] if len(sys.argv) > 2 else os.environ.get(
        "BOOKQ_TAB_MATCH", os.environ.get("BOOKQ_DEV_HOST", "dev-04"))
    tab = pick_tab(match)
    expr = (
        "new Promise(function(res){var out=[];var i=0;var iv="
        "setInterval(function(){var f=document.getElementById('perf-fps');"
        "out.push(f?f.textContent:'no-hud');if(++i>=%d){clearInterval(iv);"
        "res(out)}},550)})" % n)
    async with websockets.connect(tab["webSocketDebuggerUrl"],
                                  max_size=50_000_000) as ws:
        await ws.send(json.dumps({
            "id": 1, "method": "Runtime.evaluate",
            "params": {"expression": expr, "returnByValue": True,
                       "awaitPromise": True}}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), n * 2 + 30))
            if m.get("id") == 1:
                print(json.dumps(m["result"].get("result", {}).get("value"),
                                 ensure_ascii=False))
                return


asyncio.run(main())
