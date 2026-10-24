"""S10 (#85) — device-surface drivers for the verification runner.

Two interchangeable drivers behind one interface, plus a runner-side
"bystander" WS pair used to prove injected faults stay scoped to the
fixture session.

- ``ChromeDriver`` — one Playwright process, two BrowserContexts (separate
  storage; headful mode shows two real windows — a same-context two-tab
  stand-in is never used). All client interaction goes through the shared
  control channel + page.evaluate where a real DOM check is wanted.
- ``SimulatorDriver`` — two explicitly-UDID'd iOS Simulators driven via
  ``xcrun simctl`` (boot/openurl/io screenshot/io recordVideo) and
  ``idb ui`` (tap/text/swipe/pinch/rotate/button/describe-all). Devices
  the run did not boot are never shut down or erased.
- ``BystanderPair`` — two plain Python WS clients on the same env; their
  pairing is the control group for fault-isolation assertions.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from verify_core import (
    CAP_BYSTANDER,
    CAP_DEVICE_INPUTS,
    CAP_PWA,
    EnvError,
    UnsupportedError,
)


class ClientSpec:
    def __init__(self, client_id: str, role: str):
        self.id = client_id
        self.role = role
        self.page = None        # chrome driver handle
        self.udid = None        # simulator driver handle

    def __repr__(self):  # pragma: no cover
        return f"<ClientSpec {self.id}/{self.role}>"


# ─────────────────────────── chrome driver ────────────────────────────


class ChromeDriver:
    name = "chrome"

    def __init__(self, headed: bool = False):
        self.headed = headed
        self._pw = None
        self._browser = None
        self._contexts = []

    # -- capability / preflight -------------------------------------

    def preflight(self) -> dict:
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError as e:
            raise EnvError(
                "playwright not importable — run with "
                "`uv run --with playwright`") from e
        return {"playwright": True}

    @property
    def caps(self) -> set:
        return {CAP_BYSTANDER}          # webrtc probed per-run via fixture

    # -- launch ------------------------------------------------------

    def launch(self, clients: dict, urls: dict) -> None:
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(headless=not self.headed)
        except Exception as e:
            raise EnvError(f"chromium launch failed: {e}") from e
        for cid, spec in clients.items():
            ctx = self._browser.new_context(
                viewport={"width": 390, "height": 844})
            page = ctx.new_page()
            spec.page = page
            self._contexts.append(ctx)
            page.goto(urls[cid], wait_until="domcontentloaded")

    def stop(self) -> None:
        for c in self._contexts:
            try:
                c.close()
            except Exception:
                pass
        self._contexts = []
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None

    # -- evidence / input -------------------------------------------

    def screenshot(self, spec: ClientSpec, path: Path) -> Path:
        spec.page.screenshot(path=str(path), full_page=False)
        return path

    def tap(self, spec: ClientSpec, x: float, y: float) -> None:
        spec.page.mouse.click(x, y)

    def input_text(self, spec: ClientSpec, text: str) -> None:
        spec.page.keyboard.type(text)

    def swipe(self, spec: ClientSpec, x1: float, y1: float,
              x2: float, y2: float, duration: float = 0.15) -> None:
        m = spec.page.mouse
        m.move(x1, y1)
        m.down()
        steps = 8
        for i in range(1, steps + 1):
            m.move(x1 + (x2 - x1) * i / steps,
                   y1 + (y2 - y1) * i / steps)
            spec.page.wait_for_timeout(int(duration * 1000 / steps))
        m.up()

    def pinch(self, spec: ClientSpec, x: float, y: float,
              scale: float) -> None:
        raise UnsupportedError("chrome driver: pinch not implemented")

    def rotate(self, spec: ClientSpec, orientation: str) -> None:
        vp = spec.page.viewport_size or {"width": 390, "height": 844}
        if orientation.upper().startswith("LANDSCAPE"):
            spec.page.set_viewport_size(
                {"width": vp["height"], "height": vp["width"]})
        else:
            spec.page.set_viewport_size(
                {"width": min(vp), "height": max(vp)})

    def button(self, spec: ClientSpec, name: str) -> None:
        raise UnsupportedError(f"chrome driver: hardware button {name} n/a")

    def describe_all(self, spec: ClientSpec) -> list:
        """AX-tree equivalent: read the fixture's exposed state via DOM."""
        return spec.page.evaluate(
            """() => [...document.querySelectorAll('[aria-label]')].map(
                   el => ({label: el.getAttribute('aria-label'),
                           text: (el.textContent || '').trim()}))""")

    def tap_element(self, spec: ClientSpec, label: str) -> None:
        spec.page.click(f"[aria-label='{label}']", timeout=5000)

    def background(self, spec: ClientSpec) -> None:
        spec.page.evaluate("() => window.dispatchEvent(new Event('pagehide'))")

    def foreground(self, spec: ClientSpec) -> None:
        spec.page.evaluate("() => window.dispatchEvent(new Event('pageshow'))")

    def devices(self, clients: dict) -> dict:
        return {cid: {"kind": "chromium-context",
                      "headed": self.headed} for cid in clients}


# ─────────────────────────── simulator driver ─────────────────────────


def _run(cmd: list, timeout: float = 60.0, check: bool = True) -> subprocess.CompletedProcess:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout)
    except FileNotFoundError as e:
        raise EnvError(f"{cmd[0]} not found: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise EnvError(f"{cmd[0]} timed out: {' '.join(cmd[:3])}") from e
    if check and out.returncode != 0:
        raise EnvError(f"{cmd[0]} rc={out.returncode}: "
                       f"{(out.stderr or out.stdout)[:300]}")
    return out


def _simctl(*args, timeout: float = 60.0, check: bool = True) -> subprocess.CompletedProcess:
    return _run(["xcrun", "simctl", *args], timeout=timeout, check=check)


def _idb(*args, timeout: float = 60.0, check: bool = True) -> subprocess.CompletedProcess:
    return _run(["idb", *args], timeout=timeout, check=check)


class SimulatorDriver:
    """Two explicit-UDID simulators. Ownership rule (#85): never shutdown /
    erase a device this run did not itself boot; booted-at-entry devices
    are used but left running."""

    name = "simulator"
    SAFARI = "com.apple.mobilesafari"
    SPRINGBOARD = "com.apple.springboard"
    PREFS = "com.apple.Preferences"

    def __init__(self, leader_udid: str, follower_udid: str):
        self.udids = {"leader": leader_udid, "follower": follower_udid}
        self._pre_booted: set[str] = set()
        self._recorders: dict[str, subprocess.Popen] = {}

    # -- capability / preflight -------------------------------------

    def preflight(self) -> dict:
        _run(["xcrun", "--find", "simctl"], timeout=15)
        _idb("list-targets", timeout=30)          # idb present + reachable
        out = _simctl("list", "devices", "-j")
        devs = json.loads(out.stdout)
        found = {}
        for rt, lst in (devs.get("devices") or {}).items():
            for d in lst:
                if d.get("udid") in self.udids.values():
                    found[d["udid"]] = {
                        "name": d.get("name"), "state": d.get("state"),
                        "runtime": rt.split(".")[-1],
                        "available": d.get("isAvailable", True)}
        missing = [u for u in self.udids.values() if u not in found]
        if missing:
            raise EnvError(f"simulator UDID(s) not found: {missing}")
        for u, d in found.items():
            if not d["available"]:
                raise EnvError(f"simulator {u} unavailable: {d}")
            if d["state"] == "Booted":
                self._pre_booted.add(u)
        # idb must actually reach the companion; describe is probed lazily
        return {"devices": found, "pre_booted": sorted(self._pre_booted)}

    @property
    def caps(self) -> set:
        return {CAP_BYSTANDER, CAP_DEVICE_INPUTS, CAP_PWA}

    # -- launch ------------------------------------------------------

    def _wait_booted(self, udid: str, timeout: float = 90.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            out = _simctl("bootstatus", udid, check=False, timeout=30)
            if out.returncode == 0:
                return
            time.sleep(1.0)
        raise EnvError(f"simulator {udid} did not boot in {timeout}s")

    def launch(self, clients: dict, urls: dict) -> None:
        for cid, spec in clients.items():
            udid = self.udids[spec.role]
            spec.udid = udid
            if udid not in self._pre_booted:
                _simctl("boot", udid, check=False)   # already-booted → rc!=0
            self._wait_booted(udid)
            _simctl("openurl", udid, urls[cid])

    def stop(self) -> None:
        for udid, proc in list(self._recorders.items()):
            self.stop_video(udid)
        for udid in self.udids.values():
            if udid not in self._pre_booted:
                _simctl("shutdown", udid, check=False)

    # -- evidence / input -------------------------------------------

    def screenshot(self, spec: ClientSpec, path: Path) -> Path:
        _simctl("io", spec.udid, "screenshot", str(path), timeout=60)
        return path

    def start_video(self, spec: ClientSpec, path: Path) -> None:
        proc = subprocess.Popen(
            ["xcrun", "simctl", "io", spec.udid, "recordVideo", str(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._recorders[spec.udid] = proc

    def stop_video(self, udid: str) -> None:
        proc = self._recorders.pop(udid, None)
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()

    def tap(self, spec: ClientSpec, x: float, y: float) -> None:
        _idb("ui", "tap", str(int(x)), str(int(y)),
             "--duration", "0.15", "--udid", spec.udid)

    def input_text(self, spec: ClientSpec, text: str) -> None:
        _idb("ui", "text", text, "--udid", spec.udid)

    def swipe(self, spec: ClientSpec, x1: float, y1: float,
              x2: float, y2: float, duration: float = 0.15) -> None:
        _idb("ui", "swipe", str(int(x1)), str(int(y1)),
             str(int(x2)), str(int(y2)),
             "--duration", str(duration), "--udid", spec.udid)

    def pinch(self, spec: ClientSpec, x: float, y: float,
              scale: float) -> None:
        _idb("ui", "pinch", str(int(x)), str(int(y)), str(scale),
             "--udid", spec.udid)

    def rotate(self, spec: ClientSpec, orientation: str) -> None:
        _idb("ui", "rotate", orientation, "--udid", spec.udid)

    def button(self, spec: ClientSpec, name: str) -> None:
        _idb("ui", "button", name, "--udid", spec.udid)

    def describe_all(self, spec: ClientSpec):
        out = _idb("ui", "describe-all", "--udid", spec.udid, "--json",
                   timeout=120)
        try:
            return json.loads(out.stdout)
        except json.JSONDecodeError as e:
            raise EnvError(f"idb describe-all unparsable: {e}") from e

    def screen_size(self, spec: ClientSpec) -> tuple:
        """Logical screen size in points (describe-all root frame)."""
        f = _ax_root_frame(self.describe_all(spec))
        if not f:
            raise EnvError("AX root frame missing — cannot get screen size")
        return float(f["width"]), float(f["height"])

    def tap_element(self, spec: ClientSpec, label: str) -> bool:
        """Native-AX tap (Safari chrome / menus). Returns False when the
        label is not in the AX tree — web content needs the calibrated
        coordinate path instead (ops layer)."""
        hit = _find_ax(self.describe_all(spec), label)
        if hit is None:
            return False
        self.tap(spec, hit["x"] + hit["w"] / 2, hit["y"] + hit["h"] / 2)
        return True

    def background(self, spec: ClientSpec) -> None:
        """Send Safari to background. Face-ID devices have no HOME button —
        fall back to the bottom-edge home swipe (a real gesture)."""
        try:
            self.button(spec, "HOME")
            return
        except EnvError:
            pass
        w, h = self.screen_size(spec)
        self.swipe(spec, w / 2, h - 2, w / 2, h * 0.35, duration=0.25)

    def foreground(self, spec: ClientSpec) -> None:
        _simctl("launch", spec.udid, self.SAFARI)

    def devices(self, clients: dict) -> dict:
        return {cid: {"kind": "ios-simulator", "udid": spec.udid}
                for cid, spec in clients.items()}


_AX_MATCH_KEYS = ("AXUniqueId", "AXLabel", "label", "name", "identifier",
                  "title", "value")


def _find_ax(node, label: str):
    """Walk an idb describe-all tree; returns {x,y,w,h} for the element
    whose AXUniqueId/AXLabel/… matches. Web content inside Safari is NOT
    in this tree (idb limitation) — callers fall back to the calibrated
    coordinate path for page elements."""
    if isinstance(node, dict):
        for key in _AX_MATCH_KEYS:
            if node.get(key) == label:
                f = node.get("frame") or {}
                if f:
                    return {"x": f.get("x", 0), "y": f.get("y", 0),
                            "w": f.get("width", 1), "h": f.get("height", 1)}
                return node
        for key in ("children", "elements", "subelements"):
            for ch in node.get(key) or []:
                hit = _find_ax(ch, label)
                if hit:
                    return hit
    elif isinstance(node, list):
        for el in node:
            hit = _find_ax(el, label)
            if hit:
                return hit
    return None


def _ax_root_frame(node) -> dict | None:
    """Screen size in points from the top-level Application element."""
    if isinstance(node, dict):
        if node.get("type") == "Application" and node.get("frame"):
            return node["frame"]
        for ch in node.get("children") or []:
            r = _ax_root_frame(ch)
            if r:
                return r
    elif isinstance(node, list):
        for el in node:
            r = _ax_root_frame(el)
            if r:
                return r
    return None


def _ax_find_sub(node, needle: str):
    """Substring match over AX labels (localized rows like ホーム画面に追加)."""
    if isinstance(node, dict):
        for key in _AX_MATCH_KEYS:
            v = node.get(key)
            if isinstance(v, str) and needle in v:
                f = node.get("frame") or {}
                return {"x": f.get("x", 0), "y": f.get("y", 0),
                        "w": f.get("width", 1), "h": f.get("height", 1)}
        for key in ("children", "elements", "subelements"):
            for ch in node.get(key) or []:
                hit = _ax_find_sub(ch, needle)
                if hit:
                    return hit
    elif isinstance(node, list):
        for el in node:
            hit = _ax_find_sub(el, needle)
            if hit:
                return hit
    return None


# ─────────────────────────── bystander pair ───────────────────────────


class BystanderPair:
    """Runner-side second session over raw WS — the isolation control group
    for fault-injection scenarios (driver-independent, so identical for the
    chrome and simulator runs)."""

    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self._conns = []

    def _connect(self):
        try:
            from websockets.sync.client import connect  # noqa: PLC0415
        except ImportError as e:
            raise EnvError("websockets package unavailable — run with "
                           "`uv run --with websockets`") from e
        ws = connect(self.ws_url, open_timeout=10)
        self._conns.append(ws)
        return ws

    @staticmethod
    def _recv(ws, timeout: float = 10.0) -> dict:
        raw = ws.recv(timeout=timeout)
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        return json.loads(raw)

    @staticmethod
    def _req(rtype: str, payload=None, session_id=None, rid="b1") -> str:
        return json.dumps({
            "v": 1, "requestId": rid, "sessionId": session_id,
            "workEpoch": 0, "baseRevision": 0, "type": rtype,
            "payload": payload or {}})

    def _expect(self, ws, rtype: str, timeout: float = 10.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            f = self._recv(ws, timeout=max(0.1, deadline - time.monotonic()))
            if f.get("type") == rtype:
                return f
        raise EnvError(f"bystander: no {rtype} within {timeout}s")

    def pair_and_probe(self) -> dict:
        """Full pair + ping + signal echo + leave. Returns facts; the ops
        layer turns them into assertions."""
        wa = self._connect()
        wb = self._connect()
        hello_a = self._recv(wa)
        hello_b = self._recv(wb)
        wa.send(self._req("CREATE", rid="bc"))
        ack = self._expect(wa, "ACK")
        sid = ack["data"]["sessionId"]
        wb.send(self._req("JOIN", {"code": ack["data"]["code"]}, rid="bj"))
        jack = self._expect(wb, "ACK")
        self._expect(wa, "JOIN_REQUEST")
        wa.send(self._req("APPROVE",
                          {"memberId": jack["data"]["memberId"]},
                          session_id=sid, rid="ba"))
        st_a = self._expect(wa, "STATE")
        st_b = self._expect(wb, "STATE")
        wa.send(self._req("PING", {"seq": 99}, session_id=sid, rid="bp"))
        pong = self._expect(wa, "PONG")
        wb.send(self._req("LEAVE", session_id=sid, rid="bl"))
        end_a = self._expect(wa, "END")
        end_b = self._expect(wb, "END")
        return {
            "hello": hello_a.get("type") == "HELLO" and
                     hello_b.get("type") == "HELLO",
            "paired": len(st_a.get("presence", {})) == 2 and
                      len(st_b.get("presence", {})) == 2,
            "same_session": st_a.get("sessionId") == st_b.get("sessionId"),
            "pong": pong.get("seq") == 99,
            "ended": end_a.get("reason") == "left" and
                     end_b.get("reason") == "left",
        }

    def close(self) -> None:
        for c in self._conns:
            try:
                c.close()
            except Exception:
                pass
        self._conns = []
