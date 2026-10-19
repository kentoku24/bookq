#!/usr/bin/env python3
"""Evaluate a JS expression in a Chrome tab on the Android device over CDP.

Usage:
    uv run --with websockets python3 cdp-eval.py '<js expression>' [url-substring]

Tab selection: picks the first /json page whose URL contains the substring
arg (default: the BOOKQ_TAB_MATCH env var, or the dev host if unset — set
BOOKQ_DEV_HOST, e.g. "dev-04.javanese-goblin.ts.net"). When several tabs
match, narrow with a more specific substring (e.g. 'r=24' or '#/browse').

Requires: adb forward tcp:9222 localabstract:chrome_devtools_remote
"""
import asyncio, json, os, sys, urllib.request
import websockets

CDP = "http://localhost:9222/json"


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


async def main():
    expr = sys.argv[1]
    match = sys.argv[2] if len(sys.argv) > 2 else os.environ.get(
        "BOOKQ_TAB_MATCH", os.environ.get("BOOKQ_DEV_HOST", "dev-04"))
    tab = pick_tab(match)
    async with websockets.connect(tab["webSocketDebuggerUrl"],
                                  max_size=50_000_000) as ws:
        await ws.send(json.dumps({
            "id": 1, "method": "Runtime.evaluate",
            "params": {"expression": expr, "returnByValue": True,
                       "awaitPromise": True}}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), 30))
            if m.get("id") == 1:
                res = m["result"]
                if "exceptionDetails" in res:
                    print("EXCEPTION:", json.dumps(
                        res["exceptionDetails"], ensure_ascii=False)[:500])
                    sys.exit(1)
                print(json.dumps(res.get("result", {}).get("value"),
                                 ensure_ascii=False))
                return


asyncio.run(main())
