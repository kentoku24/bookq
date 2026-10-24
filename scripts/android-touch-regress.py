#!/usr/bin/env python3
"""Black-box Android Chrome tap/swipe regression using device-level ADB input."""
from __future__ import annotations
import argparse, asyncio, json, re, shutil, subprocess, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path
try:
    import websockets
except ImportError:
    sys.exit("websockets missing; run with uv --with websockets")

CDP_HTTP = "http://127.0.0.1:9222/json"

def run(cmd, timeout=20, binary=False):
    return subprocess.run(cmd, capture_output=True, text=not binary, timeout=timeout, check=False)

def adb(serial, *args, binary=False):
    return run(["adb", "-s", serial, *args], binary=binary)

def choose_device(requested):
    if not shutil.which("adb"):
        raise RuntimeError("adb is not on PATH; install Android platform-tools")
    out = run(["adb", "devices"], timeout=15).stdout
    devices = [line.split()[0] for line in out.splitlines()[1:] if line.split() and line.split()[-1] == "device"]
    if requested:
        if requested not in devices:
            raise RuntimeError(f"requested device {requested!r} is not connected/authorized; devices={devices}")
        return requested
    if not devices:
        raise RuntimeError("no authorized Android device; connect/unlock it and check adb devices")
    if len(devices) > 1:
        raise RuntimeError(f"multiple adb devices {devices}; pass --serial to avoid testing the wrong device")
    return devices[0]

def screen_size(serial):
    out = adb(serial, "shell", "wm", "size").stdout
    matches = re.findall(r"(?:Physical|Override) size: (\d+)x(\d+)", out)
    if not matches:
        raise RuntimeError(f"could not parse Android screen size: {out.strip()!r}")
    return tuple(map(int, matches[-1]))

def screenshot(serial, path):
    out = adb(serial, "exec-out", "screencap", "-p", binary=True)
    if out.returncode or not out.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("adb screencap failed")
    path.write_bytes(out.stdout)

class CDP:
    def __init__(self, ws):
        self.ws, self.next_id, self.events = ws, 0, []
    async def call(self, method, params=None):
        self.next_id += 1
        call_id = self.next_id
        await self.ws.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=20))
            if msg.get("id") == call_id:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method} failed: {msg['error']}")
                return msg.get("result", {})
            if msg.get("method") == "Runtime.exceptionThrown":
                self.events.append(msg)
            elif msg.get("method") == "Log.entryAdded":
                entry = msg.get("params", {}).get("entry", {})
                if entry.get("level") == "error":
                    self.events.append(msg)
    async def evaluate(self, expression):
        result = await self.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True, "userGesture": True
        })
        if result.get("exceptionDetails"):
            raise RuntimeError(f"browser JS exception: {result['exceptionDetails']}")
        return result.get("result", {}).get("value")

async def wait_until(cdp, expression, timeout=12):
    end, last = time.monotonic() + timeout, None
    while time.monotonic() < end:
        last = await cdp.evaluate(expression)
        if last:
            return last
        await asyncio.sleep(0.2)
    raise RuntimeError(f"timed out waiting for {expression}; last={last!r}")

async def navigate(cdp, url):
    await cdp.call("Page.navigate", {"url": url})
    await wait_until(cdp, "document.readyState === 'complete'", 20)

async def run_checks(args, serial, out_dir):
    try:
        pages = json.load(urllib.request.urlopen(CDP_HTTP, timeout=8))
    except Exception as exc:
        raise RuntimeError("cannot reach Android Chrome CDP at localhost:9222; check Chrome and adb forward") from exc
    tabs = [p for p in pages if p.get("type") == "page" and args.url_match in p.get("url", "")]
    if len(tabs) != 1:
        available = [p.get("url", "") for p in pages if p.get("type") == "page"]
        raise RuntimeError(f"url-match must identify exactly one Chrome tab; matches={len(tabs)}, tabs={available}")
    async with websockets.connect(tabs[0]["webSocketDebuggerUrl"], max_size=None) as ws:
        cdp = CDP(ws)
        for method in ("Page.enable", "Runtime.enable", "Log.enable"):
            await cdp.call(method)
        width, height = screen_size(serial)
        result = {
            "schema_version": 1, "status": "failed",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "device": {"serial": serial, "screen_px": [width, height],
                       "model": adb(serial, "shell", "getprop", "ro.product.model").stdout.strip(),
                       "android_release": adb(serial, "shell", "getprop", "ro.build.version.release").stdout.strip(),
                       "sdk": adb(serial, "shell", "getprop", "ro.build.version.sdk").stdout.strip()},
            "input": "adb shell input (device-level; not CDP synthetic touch)", "checks": [], "console_exceptions": []
        }
        check = {"name": "reader_tap_and_swipe", "url": args.reader_url, "status": "running"}
        result["checks"].append(check)
        await navigate(cdp, args.reader_url)
        await wait_until(cdp, "document.querySelector('#counter') && Number.isFinite(window.currentIdx)")
        initial = int(await cdp.evaluate("window.currentIdx"))
        check["before_idx"] = initial
        tap_result = run(["adb", "-s", serial, "shell", "input", "tap", str(round(width * .35)), str(round(height * .5))])
        if tap_result.returncode:
            raise RuntimeError(f"adb tap failed: {tap_result.stderr.strip()}")
        await wait_until(cdp, f"window.currentIdx === {initial + 1}", args.action_timeout)
        check["after_tap_idx"] = int(await cdp.evaluate("window.currentIdx"))
        screenshot(serial, out_dir / "reader-after-tap.png")
        swipe_result = run(["adb", "-s", serial, "shell", "input", "swipe", str(round(width * .75)), str(round(height * .5)),
                            str(round(width * .25)), str(round(height * .5)), str(args.swipe_ms)])
        if swipe_result.returncode:
            raise RuntimeError(f"adb reader swipe failed: {swipe_result.stderr.strip()}")
        await wait_until(cdp, f"window.currentIdx === {initial}", args.action_timeout)
        check["after_swipe_idx"] = int(await cdp.evaluate("window.currentIdx"))
        check["status"] = "passed"
        screenshot(serial, out_dir / "reader-after-swipe.png")

        check = {"name": "browse_vertical_flick", "url": args.browse_url, "status": "running"}
        result["checks"].append(check)
        await navigate(cdp, args.browse_url)
        await wait_until(cdp, "(()=>{const e=document.querySelector('#sidebar-scroll');return !!e && e.scrollHeight>e.clientHeight})()")
        before = float(await cdp.evaluate("document.querySelector('#sidebar-scroll').scrollTop"))
        check["before_scroll_top"] = before
        flick_result = run(["adb", "-s", serial, "shell", "input", "swipe", str(round(width * .5)), str(round(height * .82)),
                            str(round(width * .5)), str(round(height * .25)), str(args.flick_ms)])
        if flick_result.returncode:
            raise RuntimeError(f"adb browse flick failed: {flick_result.stderr.strip()}")
        await wait_until(cdp, f"document.querySelector('#sidebar-scroll').scrollTop > {before + args.min_scroll_px}", args.action_timeout)
        after = float(await cdp.evaluate("document.querySelector('#sidebar-scroll').scrollTop"))
        check.update({"after_scroll_top": after, "scroll_delta_px": after - before, "status": "passed"})
        screenshot(serial, out_dir / "browse-after-flick.png")
        # Drain events queued during the final input/screenshot before deciding.
        await cdp.evaluate("1")
        result["console_exceptions"] = cdp.events
        if cdp.events:
            result["checks"].append({"name": "browser_console", "status": "failed", "reason": f"{len(cdp.events)} exception/log events"})
        result["status"] = "failed" if cdp.events else "passed"
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        return result

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--reader-url", required=True, help="viewer URL with at least two pages")
    p.add_argument("--browse-url", required=True, help="browse URL with a vertically scrollable grid")
    p.add_argument("--url-match", required=True, help="substring uniquely matching the already-open Chrome tab")
    p.add_argument("--serial", help="adb serial; required if multiple devices are connected")
    p.add_argument("--artifacts", default=".omo/evidence/android-touch-regress")
    p.add_argument("--swipe-ms", type=int, default=350)
    p.add_argument("--flick-ms", type=int, default=300)
    p.add_argument("--min-scroll-px", type=int, default=40)
    p.add_argument("--action-timeout", type=float, default=8)
    args = p.parse_args()
    out_dir = Path(args.artifacts) / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=False)
    result = {"schema_version": 1, "status": "failed", "artifacts_dir": str(out_dir),
              "started_at": datetime.now(timezone.utc).isoformat()}
    try:
        serial = choose_device(args.serial)
        forward = run(["adb", "-s", serial, "forward", "tcp:9222", "localabstract:chrome_devtools_remote"])
        if forward.returncode:
            raise RuntimeError(f"adb port forward failed: {forward.stderr.strip()}")
        result = asyncio.run(run_checks(args, serial, out_dir))
        result["artifacts_dir"] = str(out_dir)
    except Exception as exc:
        msg = str(exc)
        result.update({"status": "environment_unavailable" if any(s in msg.lower() for s in
            ("no authorized android device", "cannot reach android chrome cdp", "not on path", "port forward")) else "failed",
            "error": msg, "artifacts_dir": str(out_dir)})
        try:
            if "serial" in locals():
                screenshot(serial, out_dir / "failure.png")
        except Exception as screen_exc:
            result["screenshot_error"] = str(screen_exc)
    finally:
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        (out_dir / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("status") == "passed" else 2

if __name__ == "__main__":
    raise SystemExit(main())
