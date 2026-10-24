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
        self.kind = "web"       # 'web' chrome | 'safari'|'pwa' simulator
        self.page = None        # chrome driver handle
        self.udid = None        # simulator driver handle
        self.launch_url = None  # the URL the device opened (reload path)

    def __repr__(self):  # pragma: no cover
        return f"<ClientSpec {self.id}/{self.role}>"


# ─────────────────────────── chrome driver ────────────────────────────


# Injected before every page script: records live WebSockets so a scenario
# can hard-drop the client's own socket from the runner side (the real
# disconnect/resume path — not a synthetic flag).
_WS_SPY = """(() => {
  window.__wsConns = window.__wsConns || [];
  if (window.WebSocket && window.WebSocket.__fxspied) return;
  const Orig = window.WebSocket;
  const Wrapped = function (url, protos) {
    const ws = protos === undefined ? new Orig(url) : new Orig(url, protos);
    window.__wsConns.push(ws);
    return ws;
  };
  Wrapped.__fxspied = true;
  Wrapped.prototype = Orig.prototype;
  Wrapped.CONNECTING = 0; Wrapped.OPEN = 1;
  Wrapped.CLOSING = 2; Wrapped.CLOSED = 3;
  window.WebSocket = Wrapped;
})();"""


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

    def launch(self, clients: dict, urls: dict, fx=None) -> None:
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(headless=not self.headed)
        except Exception as e:
            raise EnvError(f"chromium launch failed: {e}") from e
        for cid, spec in clients.items():
            ctx = self._browser.new_context(
                viewport={"width": 390, "height": 844})
            ctx.add_init_script(_WS_SPY)
            page = ctx.new_page()
            spec.page = page
            spec.launch_url = urls[cid]
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

    def close_ws(self, spec: ClientSpec) -> None:
        """Hard-drop every live WebSocket this page owns — the real
        disconnect path (client sees onclose, not a flag)."""
        spec.page.evaluate(
            "() => (window.__wsConns || []).forEach(w => {"
            "  try { w.close(); } catch (e) {} })")

    def reload(self, spec: ClientSpec) -> None:
        spec.page.reload(wait_until="domcontentloaded")

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

    def launch(self, clients: dict, urls: dict, fx=None) -> None:
        for cid, spec in clients.items():
            udid = self.udids[spec.role]
            spec.udid = udid
            spec.launch_url = urls[cid]
            if udid not in self._pre_booted:
                _simctl("boot", udid, check=False)   # already-booted → rc!=0
            self._wait_booted(udid)
        for cid, spec in clients.items():
            _simctl("openurl", spec.udid, urls[cid])
            if spec.kind == "pwa":
                if fx is None:
                    raise EnvError(
                        "pwa client kind needs the fixture server")
                fx.wait_hello(cid, timeout=60)          # Safari bootstrap
                self.install_webapp(spec)
                self.launch_webclip(spec)
                hello = fx.wait_hello(cid, timeout=90, min_seq=2)
                if not hello.get("standalone"):
                    raise EnvError(
                        f"pwa client {cid}: relaunched page is not "
                        "standalone (webclip did not take over)")

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

    # -- PWA web clip (#86: Safari×Safari / PWA×PWA / Safari×PWA) -----

    WEBCLIP_LABELS = ("bookq reader-sync fixture", "reader-sync", "fx-")

    def describe_point(self, spec: ClientSpec, x: float, y: float) -> dict:
        """AX hit-test at a screen point. Unlike describe-all this sees
        OUT-OF-PROCESS UI — the share sheet (UIActivityViewController) and
        the Add-to-Home confirm panel never appear in the app's own tree,
        but describe-point resolves them."""
        out = _idb("ui", "describe-point", str(int(x)), str(int(y)),
                   "--udid", spec.udid, "--json", timeout=30, check=False)
        try:
            return json.loads(out.stdout) if out.stdout else {}
        except json.JSONDecodeError:
            return {}

    def _probe_point_label(self, spec: ClientSpec, needles,
                           xs, ys, exact: bool = False) -> tuple | None:
        """Scan a grid of screen points; return (x, y, label) of the first
        hit whose element label contains (or, with exact, IS) any needle —
        case-insensitive. exact matters for short labels like '追加':
        substring matching also claims the sheet TITLE 'ホーム画面に追加'
        and taps the title instead of the real Add button."""
        for y in ys:
            for x in xs:
                el = self.describe_point(spec, x, y)
                vals = [str(el.get(k)).strip() for k in _AX_MATCH_KEYS
                        if isinstance(el.get(k), str)]
                for n in needles:
                    for v in vals:
                        if (v.lower() == n.lower() if exact
                                else n.lower() in v.lower()):
                            return (x, y, v)
        return None

    def install_webapp(self, spec: ClientSpec) -> None:
        """Scripted Add-to-Home-Screen via the idb AX tree (no human step).

        iOS 26+/27 Safari flow (verified by hand on iPhone 17 sim):
        page menu (MoreMenuButton) → 共有/Share → out-of-process share
        sheet → expand the actions ('表示を増やす') → vertical action
        list → 'ホーム画面に追加' → confirm '追加' top-right.
        The share sheet is another process — only describe-point sees it,
        so rows are located by scanning probe points, never by tree."""
        def find_retry(needles, tries=6, delay=0.9):
            tree = None
            for _ in range(tries):
                tree = self.describe_all(spec)
                for n in needles:
                    hit = _ax_find_sub(tree, n) or _find_ax(tree, n)
                    if hit:
                        return hit, tree
                time.sleep(delay)
            return None, tree

        # Safari must be foreground on the fixture URL before the page
        # menu exists — a mid-run surface switch can find SpringBoard or
        # another app in the AX tree instead. openurl refocuses Safari on
        # the right page (a duplicate tab is harmless for install).
        _simctl("openurl", spec.udid, spec.launch_url)
        time.sleep(2.0)
        menu, _ = find_retry(["MoreMenuButton", "ページメニュー", "Menu"],
                             tries=6)
        if menu is None:
            raise EnvError("pwa install: no page-menu button in AX tree")
        self.tap(spec, menu["x"] + menu["w"] / 2, menu["y"] + menu["h"] / 2)
        time.sleep(1.0)

        # fast path: some iOS versions expose the Add-to-Home row directly
        # in the page menu — tap it and skip the share sheet entirely.
        direct, tree = find_retry(
            ["ホーム画面に追加", "Add to Home Screen"], tries=2, delay=0.4)
        if direct is not None:
            self.tap(spec, direct["x"] + direct["w"] / 2,
                     direct["y"] + direct["h"] / 2)
        else:
            share, _tree = find_retry(
                ["共有", "Share", "ShareButton"], tries=4)
            if share is None:
                raise EnvError(
                    "pwa install: no Add-to-Home/Share row in page menu")
            self.tap(spec, share["x"] + share["w"] / 2,
                     share["y"] + share["h"] / 2)
            time.sleep(2.0)
            self._share_add_to_home(spec)

        # confirm sheet: 追加/Add sits top-right — describe-point scan.
        # exact match: '追加' substring also matches the sheet title
        # 'ホーム画面に追加' — that bug tapped the title and installed
        # nothing while reporting success.
        w, h = self.screen_size(spec)
        hit = None
        for _try in range(8):
            hit = self._probe_point_label(
                spec, ["追加", "Add"],
                xs=[w * f / 8 for f in (5, 6, 7)],
                ys=[h * f / 16 for f in (1, 2, 3)],
                exact=True)
            if hit:
                break
            time.sleep(0.8)
        if hit is None:
            raise EnvError("pwa install: no confirm button")
        self.tap(spec, hit[0], hit[1])
        time.sleep(1.5)   # webclip materializes on SpringBoard

    _ADD_HOME_NEEDLES = ("ホーム画面に追加", "Add to Home Screen",
                         "Add to Home")
    _EXPAND_NEEDLES = ("表示を増やす", "Show More", "More Actions",
                       "アクションを編集", "Edit Actions")

    def _share_add_to_home(self, spec: ClientSpec) -> None:
        """Inside the out-of-process share sheet: expand the action list
        if needed, scroll it until ホーム画面に追加 is hit-testable, tap
        it. Everything here is describe_point probing — the sheet has no
        describe-all tree."""
        w, h = self.screen_size(spec)
        # 1) the row may already be visible in the expanded activity list
        hit = self._probe_point_label(
            spec, self._ADD_HOME_NEEDLES,
            xs=[w / 2], ys=[h * f / 20 for f in range(11, 19)])
        if hit is None:
            # 2) expand the favorites action row ('表示を増やす') — it
            #    turns into a vertically scrollable list of every action.
            exp = self._probe_point_label(
                spec, self._EXPAND_NEEDLES,
                xs=[w * f / 8 for f in range(1, 8)],
                ys=[h * f / 20 for f in range(14, 19)])
            if exp:
                self.tap(spec, exp[0], exp[1])
                time.sleep(1.2)
            # 3) scroll the action list upward until the row appears —
            #    it sits deep below the favorites.
            for _scroll in range(6):
                hit = self._probe_point_label(
                    spec, self._ADD_HOME_NEEDLES,
                    xs=[w / 2], ys=[h * f / 20 for f in range(10, 19)])
                if hit:
                    break
                self.swipe(spec, w / 2, h * 0.85, w / 2, h * 0.5,
                           duration=0.5)
                time.sleep(0.8)
        if hit is None:
            raise EnvError("pwa install: ホーム画面に追加 row not found "
                           "in share sheet")
        self.tap(spec, hit[0], hit[1])
        time.sleep(1.5)

    def _find_webclip(self, spec: ClientSpec):
        """The installed web clip's icon on SpringBoard."""
        tree = self.describe_all(spec)
        for needle in self.WEBCLIP_LABELS:
            hit = _ax_find_sub(tree, needle)
            if hit:
                return hit
        return None

    def launch_webclip(self, spec: ClientSpec,
                       timeout: float = 30.0) -> None:
        """Home → find the fixture web clip icon → tap it (a REAL
        standalone PWA launch, navigator.standalone == true inside)."""
        self.background(spec)
        deadline = time.monotonic() + timeout
        hit = None
        while time.monotonic() < deadline and hit is None:
            hit = self._find_webclip(spec)
            if hit is None:
                time.sleep(1.0)
        if hit is None:
            raise EnvError("pwa webclip icon not found on SpringBoard")
        self.tap(spec, hit["x"] + hit["w"] / 2, hit["y"] + hit["h"] / 2)

    def ensure_surface(self, spec: ClientSpec, kind: str, fx,
                     client_id: str) -> None:
        """Switch this client's surface between safari and pwa mid-run —
        the spec's Safari×Safari / PWA×PWA / Safari×PWA matrix (#86).
        pwa: install + webclip launch; safari: re-open the fixture URL in
        Safari. Either way a NEW page load posts a new hello."""
        if spec.kind == kind:
            return
        if kind == "pwa":
            self.install_webapp(spec)
            seq = fx.hello_seq(client_id)
            self.launch_webclip(spec)
            hello = fx.wait_hello(client_id, timeout=90, min_seq=seq + 1)
            if not hello.get("standalone"):
                raise EnvError(
                    f"pwa client {client_id}: relaunch not standalone")
        elif kind == "safari":
            seq = fx.hello_seq(client_id)
            _simctl("terminate", spec.udid, self.SAFARI, check=False)
            time.sleep(1.0)
            _simctl("openurl", spec.udid, spec.launch_url)
            fx.wait_hello(client_id, timeout=90, min_seq=seq + 1)
        else:
            raise UnsupportedError(f"client kind {kind!r}")
        spec.kind = kind

    # -- reload --------------------------------------------------------

    def reload(self, spec: ClientSpec) -> None:
        """Reload the page under test.

        Safari surface: tap the toolbar reload button when it is in the AX
        tree (same tab → sessionStorage persists → credential resume path).
        PWA surface: relaunch the web clip (a defined terminal either way —
        web-clip storage may or may not restore credentials, and the ops
        layer asserts the terminal reached)."""
        if spec.kind == "pwa":
            self.launch_webclip(spec, timeout=10.0)
            return
        tree = self.describe_all(spec)
        hit = None
        for needle in ("再読み込み", "Reload", "Reload Page", "更新"):
            hit = _ax_find_sub(tree, needle)
            if hit:
                break
        if hit is not None:
            self.tap(spec, hit["x"] + hit["w"] / 2, hit["y"] + hit["h"] / 2)
            return
        # fallback: terminate + reopen — sessionStorage is gone, so the
        # client lands at the defined solo/credentials_lost terminal.
        _simctl("terminate", spec.udid, self.SAFARI, check=False)
        time.sleep(1.0)
        _simctl("openurl", spec.udid, spec.launch_url)

    def devices(self, clients: dict) -> dict:
        return {cid: {"kind": "ios-simulator", "udid": spec.udid,
                      "surface": spec.kind}
                for cid, spec in clients.items()}


# ─────────────────────────── real-device driver ─────────────────────
# S12 (#87) — the human run: two PHYSICAL devices a person holds. The
# driver automates nothing on the devices themselves; launch() prints
# the fixture URLs and the run gates on the humans' page loads (fixture
# hellos). Every method that would need device automation raises
# UnsupportedError so ops declaring device-input capabilities classify
# env_unavailable at plan time instead of crashing mid-run.


class RealDriver:
    """Physical devices operated by a human (S12 acceptance run)."""

    name = "real"
    # generous bound on human reaction time — opening a URL / installing
    # a web clip by hand must not time out like a scripted device would.
    MANUAL_TIMEOUT = 300.0

    def __init__(self):
        self._fx = None

    # -- capability / preflight -------------------------------------

    def preflight(self) -> dict:
        return {"manual": True,
                "note": "human opens the printed fixture URLs; "
                        "the run gates on their hellos"}

    @property
    def caps(self) -> set:
        # ws_bystander only — webrtc is added by the hello.hasRTC probe.
        # NO device_inputs/pwa/ui/screenshot: physical actions stay
        # honestly classified env_unavailable.
        return {CAP_BYSTANDER}

    # -- launch ------------------------------------------------------

    def launch(self, clients: dict, urls: dict, fx=None) -> None:
        self._fx = fx
        bar = "=" * 74
        print(f"\n[real] {bar}")
        print("[real] Open these URLs in Safari on each PHYSICAL device")
        print("[real] (share → Add to Home Screen instead for the PWA"
              " pass):")
        for cid, spec in clients.items():
            spec.launch_url = urls[cid]
            print(f"[real]   {spec.role.upper():>8}: {urls[cid]}")
        if fx is not None:
            for cid, spec in clients.items():
                print(f"[real]   {spec.role.upper():>8} (short): "
                      f"{fx.short_url(cid)}")
        print("[real] The sync WebSocket still targets the --url env —")
        print("[real] only this fixture/control page is LAN-served.")
        print(f"[real] Waiting up to {int(self.MANUAL_TIMEOUT)}s for "
              "each device hello…")
        print(f"[real] {bar}\n")

    def stop(self) -> None:
        # nothing was launched — physical devices are never touched.
        return

    def devices(self, clients: dict) -> dict:
        return {cid: {"kind": "real-device", "surface": spec.kind}
                for cid, spec in clients.items()}

    # -- mid-run surface switches (manual prompts) -------------------

    def ensure_surface(self, spec: ClientSpec, kind: str, fx,
                       client_id: str) -> None:
        """A scenario ``clients`` switch the human performs by hand: the
        new page load's hello (seq+1, standalone flag) is the proof."""
        if spec.kind == kind:
            return
        seq = fx.hello_seq(client_id)
        if kind == "pwa":
            self._prompt(
                f"{spec.role}: share → Add to Home Screen on the "
                f"physical device, then LAUNCH the new web clip "
                f"(waiting {int(self.MANUAL_TIMEOUT)}s for a "
                "standalone hello)…")
        elif kind == "safari":
            self._prompt(
                f"{spec.role}: open the fixture URL in Safari on the "
                f"physical device: {spec.launch_url} "
                f"(waiting {int(self.MANUAL_TIMEOUT)}s)…")
        else:
            raise UnsupportedError(f"client kind {kind!r}")
        hello = fx.wait_hello(client_id, timeout=self.MANUAL_TIMEOUT,
                              min_seq=seq + 1)
        if kind == "pwa" and not hello.get("standalone"):
            raise EnvError(
                f"real client {client_id}: relaunched page is not "
                "standalone (webclip did not take over)")
        spec.kind = kind

    def reload(self, spec: ClientSpec) -> None:
        """Real navigation inside the same browsing context — the page
        reloads itself on runner command; sessionStorage persists, so
        the credential RESUME path is exercised exactly like a manual
        pull-to-reload."""
        if self._fx is None:
            raise EnvError("real driver: no fixture server handle")
        self._fx.cmd(spec.id, "reload_page", timeout=15)

    # -- no device automation: every input path raises Unsupported ---

    @staticmethod
    def _prompt(msg: str) -> None:
        print(f"\n[real] {'!' * 60}\n[real] MANUAL STEP — {msg}\n"
              f"[real] {'!' * 60}\n")

    def _noauto(self, what: str):
        raise UnsupportedError(
            f"real driver: {what} needs device automation — run it as a "
            "manual step (see REAL-DEVICES.md)")

    def screenshot(self, spec, path):
        self._noauto("screenshot — capture the device screen by hand")

    def tap(self, spec, x, y): self._noauto("tap")
    def input_text(self, spec, text): self._noauto("input_text")

    def swipe(self, spec, x1, y1, x2, y2, duration=0.15):
        self._noauto("swipe")

    def pinch(self, spec, x, y, scale): self._noauto("pinch")
    def rotate(self, spec, orientation): self._noauto("rotate")
    def button(self, spec, name): self._noauto("button")
    def describe_all(self, spec): self._noauto("describe_all")
    def screen_size(self, spec): self._noauto("screen_size")
    def tap_element(self, spec, label): self._noauto("tap_element")
    def background(self, spec): self._noauto("background")
    def foreground(self, spec): self._noauto("foreground")
    def close_ws(self, spec): self._noauto("close_ws")
    def install_webapp(self, spec): self._noauto("install_webapp")
    def launch_webclip(self, spec, timeout=30.0):
        self._noauto("launch_webclip")


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
