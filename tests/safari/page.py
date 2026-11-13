"""SafariPage / SafariContext / SafariBrowser — Playwright-subset over WebDriver.

``browser``-style object graph so tests calling ``browser.new_context()`` /
``context.new_page()`` work unchanged. Each context is a fresh WebDriver
session (new Safari window). Sessions share the Safari profile, so the first
``goto`` of a session scrubs storage/cookies to mimic context isolation.
"""

from __future__ import annotations

import json as _json
import re
import time
from typing import TYPE_CHECKING

from selenium.common.exceptions import (
    NoSuchWindowException,
    WebDriverException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.actions.pointer_input import PointerInput
from selenium.webdriver.common.actions.action_builder import ActionBuilder
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webelement import WebElement

from tests.safari.bidi import BidiChannel, BidiTransportError
from tests.safari.exceptions import SafariUnsupported
from tests.safari.locator import SafariLocator, _KEY_MAP, _split_steps

if TYPE_CHECKING:
    from tests.safari.driver import SafariDriverManager

def _is_function(script: str) -> bool:
    """Is *script* a function literal (vs an expression to evaluate)?

    ``() => x`` and ``function(){}`` are; IIFEs ``(() => {})()`` and bare
    expressions are not. Detects the arrow by finding the matching close
    paren of the leading param list rather than a loose regex.
    """
    s = script.lstrip()
    if re.match(r"^(async\s+)?function\b", s):
        return True
    if re.match(r"^(async\s+)?[A-Za-z_$][\w$]*\s*=>", s):
        return True
    rest = s[6:].lstrip() if s.startswith("async ") else s
    if not rest.startswith("("):
        return False
    depth = 0
    for i, ch in enumerate(rest):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return rest[i + 1 :].lstrip().startswith("=>")
    return False


# First-goto profile scrub: Playwright contexts are isolated; Safari sessions
# share one profile, so localStorage/cookies/SW/caches/IDB must be cleared to
# approximate a fresh context.
_SCRUB_JS = """
(async () => {
  try { localStorage.clear(); sessionStorage.clear(); } catch (e) {}
  try {
    const regs = await navigator.serviceWorker.getRegistrations();
    await Promise.all(regs.map(r => r.unregister()));
  } catch (e) {}
  try {
    const keys = await caches.keys();
    await Promise.all(keys.map(k => caches.delete(k)));
  } catch (e) {}
  try {
    if (indexedDB.databases) {
      const dbs = await indexedDB.databases();
      await Promise.all((dbs || []).map(d => new Promise(res => {
        const rq = indexedDB.deleteDatabase(d.name);
        rq.onsuccess = rq.onerror = rq.onblocked = res;
      })));
    }
  } catch (e) {}
  return true;
})()
"""

# safaridriver's automation window is permanently occluded from WebKit's view:
# document.hidden stays true, requestAnimationFrame never fires, and CSS
# animations/transitions freeze at currentTime 0 (transitionend never fires).
# A real user's Safari tab is visible — emulate that: rAF via setTimeout,
# frozen animations reported finished, foreground visibility getters.
_VISIBILITY_JS = r"""
if (!window.__bookqVisEmu) {
  window.__bookqVisEmu = true;
  window.requestAnimationFrame = function (cb) {
    return setTimeout(function () { cb(performance.now()); }, 16);
  };
  window.cancelAnimationFrame = function (id) { clearTimeout(id); };
  var wrapAnims = function (list) {
    return Array.from(list).map(function (a) {
      if (a.playState === 'running' && a.currentTime === 0) {
        return new Proxy(a, { get: function (t, p) {
          return p === 'playState' ? 'finished' : t[p];
        }});
      }
      return a;
    });
  };
  var dga = Document.prototype.getAnimations;
  var ega = Element.prototype.getAnimations;
  Document.prototype.getAnimations = function (o) { return wrapAnims(dga.call(this, o)); };
  Element.prototype.getAnimations = function (o) { return wrapAnims(ega.call(this, o)); };
  // Frozen transitions otherwise pin elements at their start value (an
  // opacity fade-in leaves the element at 0 forever — which safaridriver
  // also reports as not-displayed), and they arrive BORN finished so
  // transitionend never fires. Two complementary fixes: (a) poll-finish
  // every animation so computed styles reach their end value; (b) a
  // MutationObserver on `style`/`class` that synthesizes transitionend
  // with the real property name + duration for inline transitions.
  setInterval(function () {
    try {
      dga.call(document).forEach(function (a) {
        try { if (a.playState !== 'finished') a.finish(); } catch (e) {}
      });
    } catch (e) {}
  }, 50);
  var propsOf = function (s) {
    var o = {};
    s.split(';').forEach(function (d) {
      var i = d.indexOf(':');
      if (i > 0) o[d.slice(0, i).trim()] = d.slice(i + 1).trim();
    });
    return o;
  };
  var mo = new MutationObserver(function (muts) {
    muts.forEach(function (m) {
      var el = m.target;
      try {
        if (m.attributeName === 'class') {
          // class-triggered transitions: un-pin this element's anims now
          ega.call(el).forEach(function (a) {
            try { if (a.playState !== 'finished') a.finish(); } catch (e) {}
          });
          return;
        }
        if (m.attributeName !== 'style') return;
        var tr = (el.style && el.style.transition) || '';
        if (!tr || tr === 'none') return;
        var A = propsOf(m.oldValue || ''), B = propsOf(el.getAttribute('style') || '');
        var changed = [];
        for (var k in B) if (!(k in A) || A[k] !== B[k]) changed.push(k);
        for (var k2 in A) if (!(k2 in B)) changed.push(k2);
        tr.split(',').forEach(function (decl) {
          var parts = decl.trim().split(/\s+/);
          var prop = parts[0], dur = 0, delay = 0;
          for (var i = 1; i < parts.length; i++) {
            if (/^[0-9.]+m?s$/.test(parts[i])) {
              var v = parseFloat(parts[i]) * (parts[i].slice(-2) === 'ms' ? 1 : 1000);
              if (!dur) dur = v; else delay = v;
            }
          }
          if (!prop || prop === 'none') return;
          (prop === 'all' ? changed : [prop]).forEach(function (p) {
            if (changed.indexOf(p) < 0) return;
            setTimeout(function () {
              el.dispatchEvent(new TransitionEvent('transitionend',
                { propertyName: p, elapsedTime: dur / 1000, bubbles: true }));
            }, dur + delay);
          });
        });
      } catch (e) {}
    });
  });
  mo.observe(document, {
    attributes: true, attributeOldValue: true,
    attributeFilter: ['style', 'class'], subtree: true,
  });
  try {
    Object.defineProperty(document, 'hidden', { get: function () { return false; }, configurable: true });
    Object.defineProperty(document, 'visibilityState', { get: function () { return 'visible'; }, configurable: true });
    document.hasFocus = function () { return true; };
  } catch (e) {}
}
"""

# Synthetic-touch helpers for touchscreen.tap and the CDP-touch shim. Real
# TouchEvent objects so the app's gesture handlers see real `touches` lists.
_TOUCH_JS = """
window.__bookqTouch = {
  _active: {},
  _touch(id, x, y, target) {
    return new Touch({
      identifier: id, target,
      clientX: x, clientY: y,
      screenX: x, screenY: y,
      pageX: x, pageY: y,
    });
  },
  _all() {
    return Object.entries(this._active).map(([id, a]) =>
      this._touch(+id, a.x, a.y, a.target));
  },
  _mk(type, pts) {
    // Touch targets are captured at touchstart per spec — touchend on an
    // empty point list must still reach the start target (viewer gestures
    // listen on #img-container, not body) with released fingers in
    // changedTouches.
    let changed = [], target = null;
    if (type === 'touchstart' || type === 'touchmove') {
      for (const p of pts) {
        const id = p.id || 1;
        const a = this._active[id] = this._active[id] || {x: 0, y: 0, target: null};
        if (type === 'touchstart') {
          a.target = document.elementFromPoint(p.x, p.y) || document.body;
        }
        a.x = p.x; a.y = p.y;
        changed.push(this._touch(id, a.x, a.y, a.target));
      }
    } else {
      // touchend / touchcancel — release the given ids, or all when empty
      const ids = pts.length
        ? pts.map(p => p.id || 1)
        : Object.keys(this._active).map(Number);
      for (const id of ids) {
        const a = this._active[id] || {x: 0, y: 0, target: document.body};
        changed.push(this._touch(id, a.x, a.y, a.target));
        delete this._active[id];
      }
    }
    target = (changed[0] && changed[0].target) || document.body;
    const remaining = this._all();
    const ev = new TouchEvent(type, {
      touches: remaining,
      targetTouches: remaining,
      changedTouches: changed,
      bubbles: true, cancelable: true,
    });
    target.dispatchEvent(ev);
  },
  start(pts) { this._mk('touchstart', pts); },
  move(pts)  { this._mk('touchmove', pts); },
  end(pts)   { this._mk('touchend', pts || []); },
  tap(x, y)  { const p = [{x, y, id: 1}]; this.start(p); this.end([]); },
};
"""


def _is_statements(script: str) -> bool:
    """Does *script* have top-level statements (vs a single expression)?

    WebKit's ``eval`` rejects top-level ``return`` even inside a function, so
    statement bodies must be wrapped in a real function — but IIFE
    ``(async () => {…;…})()`` expressions must NOT be, or the promise is lost.
    Scans for ``;``/statement keywords at bracket depth 0.
    """
    s = script.strip()
    if re.match(
        r"^(return|var|let|const|if|for|while|do|try|throw|switch|class|import|export|await)\b", s
    ):
        return True
    depth = 0
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c in "'\"`":
            j = s.find(c, i + 1)
            i = n if j == -1 else j
        elif c == "/" and i + 1 < n and s[i + 1] == "/":
            j = s.find("\n", i)
            i = n if j == -1 else j
        elif c == ";" and depth == 0:
            return True
        i += 1
    return False


def _script_body(script: str) -> str:
    """JS expression evaluating a Playwright-style script against ``__args``.

    Playwright accepts function literals (``() => ...``, ``(x) => ...``,
    ``function...``) — invoked with the test's args — bare expressions —
    returned directly — and statement bodies — wrapped in a function so a
    leading ``return`` works and ``arguments`` is bound.
    """
    s = script.strip()
    if _is_function(s):
        return f"({s}).apply(null, __args)"
    if _is_statements(s):
        return f"(function() {{ {s} }}).apply(null, __args)"
    return f"({s})"


_RESULT_KEY = "__bookqResults"


def _kickoff(inner: str, key: str) -> str:
    """Sync-script body: evaluate *inner*, await thenables by parking the
    resolution under ``window.__bookqResults[key]`` for a follow-up poll.

    safaridriver's ``execute_async_script`` cannot serialize WebElement args,
    so promise support has to ride on sync ``execute_script`` entirely.
    """
    k = _json.dumps(key)
    return (
        "var __args = arguments;"
        "try { var __r = " + inner + ";"
        " if (__r && typeof __r.then === 'function') {"
        f"   (window.{_RESULT_KEY} = window.{_RESULT_KEY} || {{}});"
        f"   __r.then(function(v){{ window.{_RESULT_KEY}[{k}] = {{v: v}}; }},"
        f"            function(e){{ window.{_RESULT_KEY}[{k}] = {{e: String(e && e.stack || e)}}; }});"
        "   return '__async';"
        " }"
        " return {v: __r};"
        "} catch (e) { return {e: String(e && e.stack || e)}; }"
    )


class _JSHandle:
    """A real-realm object reference (element, etc.) kept in
    ``window.__bqHandles[]`` — survives detachment like Playwright's
    JSHandle, unlike WebDriver element refs which go stale."""

    __slots__ = ("idx",)

    def __init__(self, idx: int):
        self.idx = idx


def _has_element_arg(args) -> bool:
    for a in args:
        if isinstance(a, WebElement):
            return True
        if isinstance(a, (list, tuple)) and _has_element_arg(a):
            return True
        if isinstance(a, dict) and _has_element_arg(tuple(a.values())):
            return True
    return False


class SafariPage:
    def __init__(self, context: "SafariContext", handle: str):
        self._ctx = context
        self._driver = context._driver
        self._handle = handle
        self._default_timeout = 5000.0
        self.keyboard = _Keyboard(self)
        self.mouse = _Mouse(self)
        self.touchscreen = _Touchscreen(self)
        self._listeners: dict[str, list] = {}
        self._touch_installed = False
        self._init_scripts: list[str] = []

    # ── plumbing ──────────────────────────────────────────────────────────────

    def _activate(self):
        try:
            if self._driver.current_window_handle != self._handle:
                self._driver.switch_to.window(self._handle)
        except NoSuchWindowException:
            raise SafariUnsupported("page window was closed")

    def _eval(self, script: str, *args):
        """Playwright-semantic evaluate: returns the resolved value.

        Real-realm BiDi ``script.evaluate`` for element-free calls (storage
        persists, injected globals are app-visible, awaitPromise native);
        the classic isolated-world path only for WebElement args, which
        BiDi cannot serialize."""
        if self._ctx._bidi is not None and not _has_element_arg(args):
            try:
                return self._bidi_eval(script, *args)
            except BidiTransportError:
                self._ctx._bidi = None  # socket dead — classic-only from here
        self._reqid = getattr(self, "_reqid", 0) + 1
        key = f"{id(self)}:{self._reqid}"
        out = self._sync_eval(_kickoff(_script_body(script), key), *args)
        if out == "__async":
            return self._await_result(key)
        if isinstance(out, dict) and "e" in out:
            raise WebDriverException(out["e"])
        return out.get("v") if isinstance(out, dict) else out

    def _bidi_eval(self, script: str, *args):
        # BiDi script.evaluate cannot serialize a returned function object,
        # so the function must be invoked in the expression itself. Args are
        # primitive-only here (WebElement args stay on classic eval), so
        # inlining them as a JSON literal is safe and cheaper than
        # script.callFunction's argument channel.
        #
        # safaridriver's RemoteValue serialization is shallow and
        # non-standard (objects arrive as {key: RemoteValue} maps, arrays as
        # undefined, nested objects lose contents) — so the result is
        # JSON.stringify'd in-page and decoded here, giving deep values for
        # free. undefined stays undefined through stringify, mapping to
        # None; functions/symbols likewise.
        s = script.strip()
        if any(isinstance(a, _JSHandle) for a in args):
            parts = []
            for a in args:
                if isinstance(a, _JSHandle):
                    parts.append(f"window.__bqHandles[{a.idx}]")
                else:
                    parts.append(_json.dumps(a, default=lambda _x: None))
            arglist = "[" + ",".join(parts) + "]"
        else:
            arglist = _json.dumps(list(args), default=lambda _x: None)
        if _is_function(s):
            invoke = f"( {s} ).apply(null, {arglist})"
        elif _is_statements(s):
            invoke = f"(function(){{ {s} }}).apply(null, {arglist})"
        else:
            invoke = f"( {s} )"
        expr = (
            "(async()=>{"
            f" const __v = await ({invoke});"
            " return JSON.stringify(__v)"
            "})()"
        )
        raw = self._ctx._bidi.evaluate(self._handle, expr, await_promise=True)
        return None if raw is None else _json.loads(raw)

    def _inject(self, script: str):
        """Install a shim/init script in the page's real realm (classic
        fallback so a dead BiDi socket degrades rather than fails)."""
        if self._ctx._bidi is not None:
            try:
                self._bidi_eval(script)
                return
            except BidiTransportError:
                self._ctx._bidi = None
            except WebDriverException:
                return  # in-page failure — classic realm is no better
        try:
            self._sync_eval(script)
        except WebDriverException:
            pass

    def _await_result(self, key: str, timeout: float = 30.0):
        deadline = time.time() + timeout
        poll = (
            f"var r = (window.{_RESULT_KEY} || {{}})[{_json.dumps(key)}];"
            f" if (r !== undefined) delete window.{_RESULT_KEY}[{_json.dumps(key)}];"
            " return r;"
        )
        while time.time() < deadline:
            r = self._sync_eval(poll)
            if r is not None:
                if isinstance(r, dict) and "e" in r:
                    raise WebDriverException(r["e"])
                return r.get("v") if isinstance(r, dict) else r
            time.sleep(0.02)
        raise WebDriverException("async evaluate timed out")

    def _sync_eval(self, script: str, *args):
        """Direct execute_script for statement-shaped snippets (no promise)."""
        self._activate()
        return self._driver.execute_script(script, *args)

    def _install_touch(self):
        self._inject(_TOUCH_JS)
        self._touch_installed = True

    def _poll(self, pred, timeout: float, what: str):
        deadline = time.time() + timeout / 1000.0
        while True:
            try:
                if pred():
                    return
            except (WebDriverException, AssertionError):
                pass
            if time.time() >= deadline:
                raise AssertionError(f"timeout {timeout}ms waiting for {what}")
            time.sleep(0.05)

    # ── frame/window ──────────────────────────────────────────────────────────

    @property
    def context(self) -> "SafariContext":
        return self._ctx

    @property
    def url(self) -> str:
        self._activate()
        return self._driver.current_url

    @property
    def viewport_size(self) -> dict:
        return self._ctx._viewport or {"width": 0, "height": 0}

    @property
    def main_frame(self):
        return self

    def close(self, **kw):
        self._ctx._close_page(self)

    def bring_to_front(self):
        self._activate()

    def set_default_timeout(self, ms: float):
        self._default_timeout = float(ms)

    def set_default_navigation_timeout(self, ms: float):
        pass  # WebDriver navigation timeout is session-level; ignore

    # ── navigation ────────────────────────────────────────────────────────────

    def _set_nav_token(self) -> str:
        tok = f"t{time.time_ns()}"
        try:
            self._sync_eval("window.__bqNavTok = arguments[0]", tok)
        except WebDriverException:
            pass
        return tok

    def _doc_changed(self, tok: str) -> bool:
        try:
            return (
                self._driver.execute_script(
                    "return window.__bqNavTok !== arguments[0]", tok
                )
                is True
            )
        except WebDriverException:
            return False

    def _await_new_doc(self, tok: str, quick: bool = False) -> bool:
        # pageLoadStrategy 'none' returns at commit; the JS realm can still
        # be the previous document for a moment — the token stamped on the
        # old window disappearing marks the new document's existence (and
        # injects early enough to precede the page's deferred scripts).
        deadline = time.time() + (1.5 if quick else 10.0)
        while time.time() < deadline:
            if self._doc_changed(tok):
                return True
            time.sleep(0.01 if quick else 0.02)
        if quick:
            return False
        raise WebDriverException("navigation did not commit a new document")

    def goto(self, url: str, wait_until=None, timeout=None, **kw):
        self._activate()
        self._driver.get(url)
        if self._ctx._needs_scrub:
            self._ctx._needs_scrub = False
            self._eval(_SCRUB_JS)
            self._driver.delete_all_cookies()
            self._driver.get(url)
        self._touch_installed = False
        if self._init_scripts or self._ctx._init_scripts:
            # Init-script storage side-effects (stash bridges, seeds) must
            # exist before the app's boot scripts read them — run them once
            # on the just-loaded document, then re-navigate so the app
            # boots against that state. window-global mocks die with the
            # document but _install_shims re-injects + attaches post-load.
            for js in (*self._ctx._init_scripts, *self._init_scripts):
                self._inject(js)
            self._driver.get(url)
        self._install_shims()
        if "#" in url and (self._init_scripts or self._ctx._init_scripts):
            # One-shot hash deep links (e.g. #/join/<code>) are consumed
            # during page load — before WebDriver lets us inject
            # add_init_script mocks. Re-asserting the fragment replays the
            # hashchange with the mock-bound controller now in place.
            frag = url[url.index("#"):]
            try:
                cur = self._sync_eval("return location.hash")
                if cur != frag:
                    self._sync_eval("location.hash = arguments[0]", frag)
            except WebDriverException:
                pass
        return None

    def reload(self, **kw):
        self._activate()
        self._driver.refresh()
        self._touch_installed = False
        self._install_shims()

    def _traverse_history(self, which: str):
        # driver.back()/forward() in safaridriver jumps to the previous FULL
        # navigation — it skips same-document entries entirely. history.go()
        # does the spec-correct single-entry traversal, which is what the
        # app's pushState/hash browse stack needs.
        try:
            before = self._sync_eval("return location.href")
        except WebDriverException:
            before = None
        tok = self._set_nav_token()
        self._sync_eval(f"history.{which}()")
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if self._doc_changed(tok):
                # Cross-document traversal — full load on the new document.
                self._touch_installed = False
                self._install_shims()
                self.wait_for_load_state("load")
                return
            try:
                if before is not None and self._sync_eval("return location.href") != before:
                    return  # same-document traversal (pushState/hash)
            except WebDriverException:
                pass
            time.sleep(0.02)

    def go_back(self, **kw):
        self._activate()
        self._traverse_history("back")

    def go_forward(self, **kw):
        self._activate()
        self._traverse_history("forward")

    def wait_for_load_state(self, state: str = "load", timeout=None, **kw):
        ready = {"load": "complete", "domcontentloaded": "complete", "commit": "loading"}.get(state, "complete")
        if state == "domcontentloaded":
            self._poll(
                lambda: self._sync_eval("return document.readyState") in ("interactive", "complete"),
                timeout or self._default_timeout, f"load_state {state}")
        else:
            self._poll(
                lambda: self._sync_eval("return document.readyState") == ready,
                timeout or self._default_timeout, f"load_state {state}")

    def wait_for_url(self, expected, timeout=None, **kw):
        def ok():
            u = self._driver.current_url
            if hasattr(expected, "search"):
                return bool(expected.search(u))
            if callable(expected):
                return bool(expected(u))
            if isinstance(expected, str):
                if "*" in expected:
                    return re.fullmatch(re.escape(expected).replace(r"\*\*", ".*").replace(r"\*", "[^/]*"), u) is not None
                return u == expected
            return False

        self._poll(ok, timeout or self._default_timeout, f"url {expected!r}")

    # ── evaluation ────────────────────────────────────────────────────────────

    def evaluate(self, script: str, arg=None):
        return self._eval(script, arg)

    def evaluate_handle(self, script: str, arg=None):
        # Playwright JSHandles keep a live JS reference — including detached
        # nodes WebDriver can no longer resolve. Park the value in a real-realm
        # registry; _bidi_eval revives it when passed back as an argument.
        if self._ctx._bidi is not None:
            s = script.strip()
            arglist = _json.dumps([arg], default=lambda _x: None)
            if _is_function(s):
                invoke = f"( {s} ).apply(null, {arglist})"
            elif _is_statements(s):
                invoke = f"(function(){{ {s} }}).apply(null, {arglist})"
            else:
                invoke = f"( {s} )"
            idx = self._ctx._bidi.evaluate(
                self._handle,
                "(async()=>{"
                " const __v = await (" + invoke + ");"
                " window.__bqHandles = window.__bqHandles || [];"
                " window.__bqHandles.push(__v);"
                " return window.__bqHandles.length - 1"
                "})()",
                await_promise=True,
            )
            return _JSHandle(idx)
        self._reqid = getattr(self, "_reqid", 0) + 1
        key = f"{id(self)}:{self._reqid}"
        out = self._sync_eval(_kickoff(_script_body(script), key), arg)
        if out == "__async":
            return self._await_result(key)
        if isinstance(out, dict) and "e" in out:
            raise WebDriverException(out["e"])
        return out.get("v") if isinstance(out, dict) else out

    def eval_on_selector(self, selector: str, expression: str, arg=None, **kw):
        els = self._find_css(selector)
        if not els:
            raise AssertionError(f"eval_on_selector: no element for {selector!r}")
        return self._eval(expression, els[0], arg)

    def eval_on_selector_all(self, selector: str, expression: str, arg=None, **kw):
        return self._eval(expression, self._find_css(selector), arg)

    def _find_css(self, selector: str) -> list:
        self._activate()
        return self._driver.find_elements("css selector", selector)

    # ── locators ──────────────────────────────────────────────────────────────

    def locator(self, selector: str, has_text=None, **kw) -> SafariLocator:
        return SafariLocator(self, _split_steps(selector), has_text=has_text)

    def get_by_role(self, *a, **kw):
        raise SafariUnsupported("get_by_role has no WebDriver equivalent")

    def get_by_text(self, *a, **kw):
        raise SafariUnsupported("get_by_text has no WebDriver equivalent")

    def get_by_label(self, *a, **kw):
        raise SafariUnsupported("get_by_label has no WebDriver equivalent")

    # ── element shortcuts ─────────────────────────────────────────────────────

    def click(self, selector: str, **kw):
        self.locator(selector).click(**{k: v for k, v in kw.items() if k != "button"})

    def dblclick(self, selector: str, **kw):
        self.locator(selector).dblclick(**kw)

    def fill(self, selector: str, value: str, **kw):
        self.locator(selector).fill(value, **kw)

    def press(self, selector: str, key: str, **kw):
        self.locator(selector).press(key, **kw)

    def type(self, selector: str, text: str, **kw):
        self.locator(selector).type(text, **kw)

    def tap(self, selector: str, **kw):
        self.locator(selector).tap(**kw)

    def hover(self, selector: str, **kw):
        self.locator(selector).hover(**kw)

    def focus(self, selector: str, **kw):
        self.locator(selector).focus(**kw)

    def check(self, selector: str, **kw):
        self.locator(selector).check(**kw)

    def uncheck(self, selector: str, **kw):
        self.locator(selector).uncheck(**kw)

    def select_option(self, selector: str, values=None, **kw):
        self.locator(selector).select_option(values, **kw)

    def dispatch_event(self, selector: str, type: str, event_init=None, **kw):
        self.locator(selector).dispatch_event(type, event_init, **kw)

    def text_content(self, selector: str, **kw):
        return self.locator(selector).text_content(**kw)

    def inner_text(self, selector: str, **kw):
        return self.locator(selector).inner_text(**kw)

    def get_attribute(self, selector: str, name: str, **kw):
        return self.locator(selector).get_attribute(name, **kw)

    def input_value(self, selector: str, **kw):
        return self.locator(selector).input_value(**kw)

    def is_visible(self, selector: str, **kw) -> bool:
        return self.locator(selector).is_visible()

    def is_hidden(self, selector: str, **kw) -> bool:
        return not self.is_visible(selector)

    def is_enabled(self, selector: str, **kw) -> bool:
        return self.locator(selector).is_enabled()

    def is_checked(self, selector: str, **kw) -> bool:
        return self.locator(selector).is_checked()

    def query_selector(self, selector: str):
        els = self._find_css(selector)
        return els[0] if els else None

    def query_selector_all(self, selector: str) -> list:
        return self._find_css(selector)

    # ── waiting ───────────────────────────────────────────────────────────────

    def wait_for_selector(self, selector: str, state: str = "visible", timeout=None, **kw):
        return self.locator(selector).wait_for(state=state, timeout=timeout)

    def wait_for_function(self, expression: str, arg=None, timeout=None, polling=None, **kw):
        timeout = timeout if timeout is not None else self._default_timeout
        deadline = time.time() + timeout / 1000.0
        while True:
            try:
                if self._eval(expression, arg):
                    return
            except WebDriverException:
                pass
            if time.time() >= deadline:
                raise AssertionError(f"wait_for_function timeout ({timeout}ms): {expression[:120]!r}")
            time.sleep((polling or 50) / 1000.0 if isinstance(polling, (int, float)) else 0.05)

    def wait_for_timeout(self, ms: float):
        time.sleep(ms / 1000.0)

    def wait_for_event(self, *a, **kw):
        raise SafariUnsupported("wait_for_event is not supported on Safari")

    # ── misc page api ─────────────────────────────────────────────────────────

    def screenshot(self, path=None, full_page=None, **kw):
        self._activate()
        if path:
            self._driver.save_screenshot(path)
            return path
        return self._driver.get_screenshot_as_png()

    def set_viewport_size(self, size: dict):
        self._ctx._set_viewport(size["width"], size["height"])

    def set_content(self, html: str, **kw):
        self._sync_eval(
            "document.open(); document.write(arguments[0]); document.close()", html
        )
        self._touch_installed = False
        self._install_shims()

    def content(self) -> str:
        self._activate()
        return self._driver.page_source

    def title(self) -> str:
        self._activate()
        return self._driver.title

    def add_style_tag(self, *a, **kw):
        raise SafariUnsupported("add_style_tag is not supported on Safari")

    def add_script_tag(self, *a, **kw):
        raise SafariUnsupported("add_script_tag is not supported on Safari")

    # ── events ────────────────────────────────────────────────────────────────

    def on(self, event: str, callback):
        if event != "pageerror":
            raise SafariUnsupported(f"page.on({event!r}) is not supported on Safari")
        self._listeners.setdefault(event, []).append(callback)
        self._ctx._ensure_error_drain()

    def once(self, event: str, callback):
        self.on(event, callback)

    def remove_listener(self, event: str, callback=None):
        self._listeners.get(event, []).clear()

    def _drain_page_errors(self):
        if "pageerror" not in self._listeners:
            return
        try:
            errs = self._eval(
                "(() => { const e = window.__pageErrors || []; window.__pageErrors = []; return e; })()"
            ) or []
        except WebDriverException:
            return
        for err in errs:
            for cb in self._listeners.get("pageerror", []):
                cb(Exception(err))

    def _install_error_hook(self):
        self._install_shims()

    def _install_shims(self):
        for js in (*self._ctx._init_scripts, *self._init_scripts):
            self._inject(js)
        # add_init_script lands post-load on WebDriver — too late for
        # consumers that bound a controller at DOMContentLoaded. The app's
        # PairUI exposes attach() exactly for late binding; nudge it when a
        # script-provided controller appeared.
        self._inject(
            "if (window.PairUI && window.PairUI.attach && window.PairController)"
            " window.PairUI.attach(window.PairController)"
        )
        self._inject(_VISIBILITY_JS)
        self._inject(
            "if (!window.__pageErrHook) { window.__pageErrHook = 1;"
            " window.addEventListener('error', e => (window.__pageErrors = window.__pageErrors || []).push(String(e.message)));"
            " window.addEventListener('unhandledrejection', e => (window.__pageErrors = window.__pageErrors || []).push('rejection: ' + String(e.reason))); }"
        )

    # ── unsupported (raise → test skips) ──────────────────────────────────────

    def route(self, *a, **kw):
        raise SafariUnsupported("page.route network interception needs the proxy — unsupported")

    def unroute(self, *a, **kw):
        raise SafariUnsupported("page.unroute is not supported on Safari")

    def add_init_script(self, script: str, *a, **kw):
        # No true pre-parse hook exists over WebDriver — the script is
        # re-injected right after every navigation (see _install_shims).
        self._init_scripts.append(script)

    def emulate_media(self, *a, **kw):
        raise SafariUnsupported("emulate_media is not supported on Safari")

    def set_extra_http_headers(self, *a, **kw):
        raise SafariUnsupported("set_extra_http_headers is not supported on Safari")

    def expose_function(self, *a, **kw):
        raise SafariUnsupported("expose_function is not supported on Safari")

    def expose_binding(self, *a, **kw):
        raise SafariUnsupported("expose_binding is not supported on Safari")

    def expect_response(self, predicate=None, timeout=None):
        return _ExpectResponse(self, predicate, timeout)

    def expect_download(self, *a, **kw):
        raise SafariUnsupported("expect_download is not supported on Safari")

    def expect_popup(self, *a, **kw):
        raise SafariUnsupported("expect_popup is not supported on Safari")

    def pause(self, *a, **kw):
        raise SafariUnsupported("page.pause is not supported on Safari")

    @property
    def video(self):
        return None


class _ExpectResponse:
    """Polls the resource-timing buffer for a matching entry around an action.

    WebDriver has no network events; ``performance.getEntriesByType('resource')``
    gives URL + (on modern Safari) responseStatus.
    """

    _LIST_JS = (
        "performance.getEntriesByType('resource').map(e => ({"
        " url: e.name, status: e.responseStatus || 0}))"
    )

    def __init__(self, page: SafariPage, predicate, timeout):
        self._page = page
        self._pred = predicate or (lambda r: True)
        self._timeout = timeout if timeout is not None else page._default_timeout
        self._seen = 0
        self.value = None

    def __enter__(self):
        self._seen = len(self._page._eval(self._LIST_JS) or [])
        return self

    def __exit__(self, *exc):
        if exc[0] is not None:
            return
        deadline = time.time() + self._timeout / 1000.0
        while time.time() < deadline:
            entries = self._page._eval(self._LIST_JS) or []
            for e in entries[self._seen :]:
                r = _Response(e["url"], e["status"])
                if self._pred(r):
                    self.value = r
                    return
            time.sleep(0.05)
        raise AssertionError("expect_response timed out")


class _Response:
    def __init__(self, url: str, status: int):
        self.url = url
        self.status = status

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400


class _Keyboard:
    def __init__(self, page: SafariPage):
        self._page = page

    def press(self, key: str, **kw):
        # The actions API synthesizes real key events; element.send_keys on a
        # non-editable active element (BODY) can silently drop the keydown.
        page = self._page
        page._activate()
        chains = ActionChains(page._driver)
        send = key
        if "+" in key:
            *mods, send = key.split("+")
            for m in mods:
                chains.key_down(_KEY_MAP.get(m.strip(), m.strip()))
        chains.send_keys(_KEY_MAP.get(send, send))
        if "+" in key:
            for m in reversed(mods):
                chains.key_up(_KEY_MAP.get(m.strip(), m.strip()))
        chains.perform()

    def type(self, text: str, **kw):
        self._page._activate()
        ActionChains(self._page._driver).send_keys(text).perform()

    def insert_text(self, text: str):
        self.type(text)


class _Mouse:
    def __init__(self, page: SafariPage):
        self._page = page
        self._x = 0
        self._y = 0

    # W3C pointer actions do not synthesize real DOM mouse events under
    # safaridriver (same reason element.click() is a no-op) — dispatch the
    # standard event sequence at the hit point in-page instead.
    _SEQ_JS = (
        "const [x, y, types] = arguments;"
        "const el = document.elementFromPoint(x, y);"
        "if (!el) return;"
        "for (const t of types) {"
        "  el.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true,"
        "    clientX: x, clientY: y, view: window, button: 0}));"
        "}"
    )

    def _at(self, x=None, y=None):
        # Playwright's mouse.down()/up() fire at the current position —
        # callers like swipe_root_icon move to the release point then call
        # up() bare, so a fixed (0,0) would never satisfy release-position
        # handlers.
        if x is not None:
            self._x = x
        if y is not None:
            self._y = y
        return int(self._x), int(self._y)

    def click(self, x: float, y: float, **kw):
        self._page._eval(
            self._SEQ_JS, *self._at(x, y),
            ["pointerdown", "mousedown", "pointerup", "mouseup", "click"],
        )

    def dblclick(self, x: float, y: float, **kw):
        self._page._eval(
            self._SEQ_JS, *self._at(x, y),
            ["pointerdown", "mousedown", "pointerup", "mouseup", "click",
             "pointerdown", "mousedown", "pointerup", "mouseup", "click",
             "dblclick"],
        )

    def move(self, x: float, y: float, steps: int = 1, **kw):
        x0, y0 = self._x, self._y
        n = max(int(steps or 1), 1)
        for i in range(1, n + 1):
            xi = x0 + (x - x0) * i / n
            yi = y0 + (y - y0) * i / n
            self._page._eval(
                self._SEQ_JS, *self._at(xi, yi), ["pointermove", "mousemove"]
            )

    def down(self, x: float = None, y: float = None, **kw):
        self._page._eval(
            self._SEQ_JS, *self._at(x, y), ["pointerdown", "mousedown"]
        )

    def up(self, x: float = None, y: float = None, **kw):
        self._page._eval(
            self._SEQ_JS, *self._at(x, y), ["pointerup", "mouseup"]
        )

    def wheel(self, delta_x: float, delta_y: float, **kw):
        self._page._eval(
            "window.dispatchEvent(new WheelEvent('wheel',"
            " {deltaX: arguments[0], deltaY: arguments[1], bubbles: true}))",
            delta_x, delta_y,
        )


class _Touchscreen:
    def __init__(self, page: SafariPage):
        self._page = page

    def tap(self, x: float, y: float, **kw):
        page = self._page
        if not page._touch_installed:
            page._install_touch()
        page._eval("window.__bookqTouch.tap(arguments[0], arguments[1])", x, y)


class SafariContext:
    """One WebDriver session ≈ one Playwright context."""

    def __init__(self, browser: "SafariBrowser", driver, viewport: dict | None):
        self._browser = browser
        self._driver = driver
        self._viewport = viewport
        self._needs_scrub = True
        self._pages: list[SafariPage] = []
        self._closed = False
        self._init_scripts: list[str] = []
        self._bidi = BidiChannel.connect(driver.capabilities)
        if viewport:
            self._set_viewport(viewport["width"], viewport["height"])
        self._driver.timeouts.script = 30.0

    def _set_viewport(self, w: int, h: int):
        """Resize so the *inner* viewport lands on (w, h) — window chrome is
        corrected by measuring the inner/outer delta."""
        d = self._driver
        rect = d.get_window_rect()
        d.set_window_rect(width=w, height=h)
        # The fresh session's browsing context can take a moment before it
        # accepts execute_script — retry the inner-size read briefly.
        inner = None
        deadline = time.time() + 3.0
        while time.time() < deadline:
            try:
                inner = d.execute_script("return [innerWidth, innerHeight]")
                break
            except WebDriverException:
                time.sleep(0.1)
        if inner is None:
            self._viewport = {"width": w, "height": h}
            return
        d.set_window_rect(
            x=rect["x"], y=rect["y"],
            width=w + (w - inner[0]), height=h + (h - inner[1]),
        )
        self._viewport = {"width": w, "height": h}

    def new_page(self) -> SafariPage:
        if self._closed:
            raise SafariUnsupported("context is closed")
        if not self._pages:
            handle = self._driver.current_window_handle
        else:
            self._driver.switch_to.new_window("tab")
            handle = self._driver.current_window_handle
        page = SafariPage(self, handle)
        self._pages.append(page)
        page._install_error_hook()
        return page

    def _close_page(self, page: SafariPage):
        try:
            page._activate()
            self._driver.close()
        except WebDriverException:
            pass
        if page in self._pages:
            self._pages.remove(page)

    def pages(self) -> list:
        return list(self._pages)

    @property
    def browser(self):
        return self._browser

    def _ensure_error_drain(self):
        import threading

        if getattr(self, "_drain_thread", None):
            return
        stop = self._drain_stop = threading.Event()

        def loop():
            while not stop.is_set():
                for p in list(self._pages):
                    try:
                        p._drain_page_errors()
                    except Exception:
                        pass
                stop.wait(0.1)

        t = self._drain_thread = threading.Thread(target=loop, daemon=True)
        t.start()

    def add_init_script(self, script: str, *a, **kw):
        self._init_scripts.append(script)

    def set_offline(self, *a, **kw):
        raise SafariUnsupported("context.set_offline needs the proxy — unsupported")

    def route(self, *a, **kw):
        raise SafariUnsupported("context.route needs the proxy — unsupported")

    def new_cdp_session(self, *a, **kw):
        raise SafariUnsupported("CDP is Chromium-only — use the JS touch shim")

    def close(self, **kw):
        if self._closed:
            return
        self._closed = True
        if getattr(self, "_drain_stop", None):
            self._drain_stop.set()
        if self._bidi:
            self._bidi.close()  # before quit — an open WS can keep Safari paired
        try:
            self._driver.quit()
        except WebDriverException:
            pass
        self._browser._manager.session_released()


class SafariBrowser:
    """Drop-in for the playwright `browser` fixture under BROWSER=safari."""

    def __init__(self, manager: "SafariDriverManager"):
        self._manager = manager

    def new_context(self, viewport=None, is_mobile=False, has_touch=False,
                    device_scale_factor=1, user_agent=None,
                    record_video_dir=None, record_video_size=None, **kw) -> SafariContext:
        if self._manager.live_sessions > 0:
            raise SafariUnsupported(
                "Safari.app pairs with a single WebDriver session — "
                "multiple concurrent contexts are impossible"
            )
        if is_mobile or user_agent or has_touch:
            # Desktop Safari cannot emulate mobile UA/touch hardware. We still
            # create the session (viewport sizing works) — the caller decides
            # whether the semantics hold.
            pass
        last_exc = None
        for attempt in range(4):
            try:
                driver = self._manager.new_session()
            except WebDriverException as exc:
                # Safari.app can still hold the previous session's pairing for
                # a beat after quit() — or a prior quit() silently failed,
                # leaving a live session paired forever — or the driver has
                # hit its ~20-session pairing limit. Escalate: force-quit the
                # stale handle first, then recycle the driver process.
                last_exc = exc
                if attempt < 3:
                    if attempt >= 1:
                        self._manager.recycle()
                    else:
                        self._manager.force_quit_stale()
                    continue
                raise
            try:
                return SafariContext(self, driver, viewport)
            except Exception as exc:
                # Context init failed (e.g. session browsing-context race,
                # or a session Safari dropped while a prior window was still
                # tearing down) — release the session so the next attempt /
                # test isn't locked out; Safari pairs exactly one WebDriver
                # session globally.
                last_exc = exc
                try:
                    driver.quit()
                except WebDriverException:
                    pass
                self._manager.session_released()
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
        raise last_exc

    def close(self):
        pass
