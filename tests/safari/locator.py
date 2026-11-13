"""SafariLocator — Playwright-locator subset over WebDriver elements.

Selectors are resolved lazily on every call (fresh ``find_elements``), matching
Playwright's live-resolution semantics. Element operations re-resolve on
``StaleElementReferenceException`` — the app's DOM churns constantly during
render, so a resolved element can go stale mid-call.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from selenium.common.exceptions import (
    StaleElementReferenceException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from tests.safari.exceptions import SafariUnsupported

if TYPE_CHECKING:
    from tests.safari.page import SafariPage

_KEY_MAP = {
    "ArrowLeft": Keys.ARROW_LEFT,
    "ArrowRight": Keys.ARROW_RIGHT,
    "ArrowUp": Keys.ARROW_UP,
    "ArrowDown": Keys.ARROW_DOWN,
    "Escape": Keys.ESCAPE,
    "Enter": Keys.ENTER,
    "End": Keys.END,
    "Home": Keys.HOME,
    "Tab": Keys.TAB,
    "Backspace": Keys.BACKSPACE,
    "Delete": Keys.DELETE,
    "Space": Keys.SPACE,
    " ": " ",
}

_DISPLAYED_JS = (
    "const el=arguments[0],cs=getComputedStyle(el);"
    "if(cs.display==='none'||cs.visibility==='hidden'||cs.visibility==='collapse')"
    "return false;const r=el.getBoundingClientRect();return r.width>0&&r.height>0"
)


def _disp(page, el) -> bool:
    """Playwright-semantic displayedness: non-empty box + not display:none /
    visibility:hidden. safaridriver's is_displayed additionally requires the
    element to be inside the viewport, which fails off-canvas UI (closed
    sidebar) and anything overlapped by the occluded automation window."""
    try:
        return bool(page._sync_eval(_DISPLAYED_JS, el))
    except WebDriverException:
        return False



def _split_steps(selector: str) -> list[str]:
    return [s.strip() for s in selector.split(">>")]


def _text_xpath(text: str) -> str:
    lit = repr(text)
    return f".//*[contains(normalize-space(.), {lit})]"


class _Retry(Exception):
    """Internal: re-resolve and retry the element operation."""


class SafariLocator:
    def __init__(self, page: "SafariPage", chain: list[str], *, has_text=None):
        self._page = page
        self._chain = chain
        self._has_text = has_text

    # ── resolution ────────────────────────────────────────────────────────────

    def _resolve(self) -> list:
        roots = [self._page._driver]
        for step in self._chain:
            found: list = []
            if step.startswith("text="):
                for root in roots:
                    found += root.find_elements(By.XPATH, _text_xpath(step[5:].strip("\"'")))
            elif step.startswith("xpath="):
                for root in roots:
                    found += root.find_elements(By.XPATH, step[6:])
            elif step.startswith("css="):
                for root in roots:
                    found += root.find_elements(By.CSS_SELECTOR, step[4:])
            else:
                for root in roots:
                    found += root.find_elements(By.CSS_SELECTOR, step)
            roots = found
        if self._has_text is not None:
            needle = self._has_text
            out = []
            for e in roots:
                try:
                    t = e.text or ""
                except WebDriverException:
                    continue
                if (needle.search(t) if hasattr(needle, "search") else needle in t):
                    out.append(e)
            roots = out
        return roots

    def _t(self, timeout):
        return time.time() + (timeout if timeout is not None else self._page._default_timeout) / 1000.0

    def _first(self, timeout: float | None = None):
        deadline = self._t(timeout)
        while True:
            els = self._resolve()
            if els:
                return els[0]
            if time.time() >= deadline:
                raise AssertionError(f"locator {self._chain!r} resolved to 0 elements")
            time.sleep(0.05)

    def _visible(self, timeout: float | None = None):
        deadline = self._t(timeout)
        while True:
            try:
                els = self._resolve()
                if els and _disp(self._page, els[0]):
                    return els[0]
            except StaleElementReferenceException:
                pass
            if time.time() >= deadline:
                raise AssertionError(f"locator {self._chain!r} not visible")
            time.sleep(0.05)

    def _act(self, fn, timeout=None):
        """Re-resolve + retry *fn(el)* on staleness until timeout."""
        deadline = self._t(timeout)
        while True:
            try:
                el = self._visible(timeout)
                return fn(el)
            except StaleElementReferenceException:
                pass
            if time.time() >= deadline:
                raise AssertionError(f"locator {self._chain!r} stale past timeout")
            time.sleep(0.05)

    def _eval(self, script: str, *args):
        return self._page._eval(script, *args)

    # ── structure ─────────────────────────────────────────────────────────────

    def count(self) -> int:
        return len(self._resolve())

    def nth(self, i: int) -> "SafariLocator":
        return _IndexedLocator(self._page, self._chain, i, has_text=self._has_text)

    @property
    def first(self) -> "SafariLocator":
        return self.nth(0)

    @property
    def last(self) -> "SafariLocator":
        return self.nth(-1)

    def all(self) -> list["SafariLocator"]:
        return [self.nth(i) for i in range(self.count())]

    def locator(self, selector: str, has_text=None) -> "SafariLocator":
        return SafariLocator(self._page, self._chain + _split_steps(selector), has_text=has_text)

    def get_by_role(self, *a, **kw):
        raise SafariUnsupported("get_by_role has no WebDriver equivalent")

    # ── actions ───────────────────────────────────────────────────────────────

    def click(self, timeout=None, force=None, **kw):
        # safaridriver's synthesized click is unreliable (observed no-op on
        # tiles); a JS .click() fires the same handlers and never misses.
        # Playwright clicks also focus the target — synthetic .click() does
        # not, so keyboard input would still go to <body>.
        self._act(
            lambda el: self._eval(
                "arguments[0].focus && arguments[0].focus(); arguments[0].click()", el
            ),
            timeout,
        )

    def dblclick(self, timeout=None, **kw):
        from selenium.webdriver.common.action_chains import ActionChains

        def go(el):
            ActionChains(self._page._driver).double_click(el).perform()

        self._act(go, timeout)

    def tap(self, timeout=None, **kw):
        if not self._page._touch_installed:
            self._page._install_touch()
        self._act(
            lambda el: self._eval(
                "const r = arguments[0].getBoundingClientRect();"
                "window.__bookqTouch.tap(r.left + r.width/2, r.top + r.height/2)",
                el,
            ),
            timeout,
        )

    def fill(self, value: str, timeout=None, **kw):
        def go(el):
            el.clear()
            el.send_keys(value)
            # safaridriver's clear()/send_keys("") don't reliably emit 'input'
            # — Playwright's fill does, and debounced app handlers depend on it.
            self._eval(
                "arguments[0].dispatchEvent(new Event('input', {bubbles: true}))",
                el,
            )

        self._act(go, timeout)

    def type(self, text: str, timeout=None, **kw):
        self._act(lambda el: el.send_keys(text), timeout)

    def press(self, key: str, timeout=None, **kw):
        def go(el):
            tag = el.tag_name.lower()
            if tag in ("input", "textarea") or el.get_attribute("contenteditable"):
                el.send_keys(_KEY_MAP.get(key, key))
            else:
                # element.send_keys drops keydown on non-editable targets —
                # focus, then use the actions input pipeline instead.
                self._eval("arguments[0].focus()", el)
                from selenium.webdriver.common.action_chains import ActionChains
                ActionChains(self._page._driver).send_keys(_KEY_MAP.get(key, key)).perform()

        self._act(go, timeout)

    def hover(self, timeout=None, **kw):
        from selenium.webdriver.common.action_chains import ActionChains

        self._act(lambda el: ActionChains(self._page._driver).move_to_element(el).perform(), timeout)

    def focus(self, timeout=None, **kw):
        self._eval("arguments[0].focus()", self._first(timeout))

    def blur(self, timeout=None, **kw):
        self._eval("arguments[0].blur()", self._first(timeout))

    def scroll_into_view_if_needed(self, timeout=None, **kw):
        self._eval(
            "arguments[0].scrollIntoView({block:'nearest', inline:'nearest'})",
            self._first(timeout),
        )

    def select_option(self, values=None, timeout=None, **kw):
        from selenium.webdriver.support.select import Select

        el = self._first(timeout)
        sel = Select(el)
        vals = values if isinstance(values, list) else [values]
        for v in vals:
            sel.select_by_value(str(v))

    def check(self, timeout=None, **kw):
        def go(el):
            if not el.is_selected():
                el.click()

        self._act(go, timeout)

    def uncheck(self, timeout=None, **kw):
        def go(el):
            if el.is_selected():
                el.click()

        self._act(go, timeout)

    def set_checked(self, checked: bool, timeout=None, **kw):
        (self.check if checked else self.uncheck)(timeout=timeout)

    def dispatch_event(self, type: str, event_init=None, timeout=None, **kw):
        el = self._first(timeout)
        if event_init:
            self._eval(
                "arguments[0].dispatchEvent(new Event(arguments[1], arguments[2]))",
                el, type, event_init,
            )
        else:
            self._eval("arguments[0].dispatchEvent(new Event(arguments[1]))", el, type)

    def select_text(self, timeout=None, **kw):
        self._eval(
            "const e=arguments[0], r=document.createRange();"
            "r.selectNodeContents(e); const s=getSelection();"
            "s.removeAllRanges(); s.addRange(r)",
            self._first(timeout),
        )

    # ── state ─────────────────────────────────────────────────────────────────

    def get_attribute(self, name: str, timeout=None):
        return self._eval("return arguments[0].getAttribute(arguments[1])", self._first(timeout), name)

    def text_content(self, timeout=None):
        return self._eval("return arguments[0].textContent", self._first(timeout))

    def inner_text(self, timeout=None):
        return self._eval("return arguments[0].innerText", self._first(timeout))

    def inner_html(self, timeout=None):
        return self._eval("return arguments[0].innerHTML", self._first(timeout))

    def input_value(self, timeout=None):
        return self._eval("return arguments[0].value", self._first(timeout))

    def bounding_box(self, timeout=None):
        return self._eval(
            "const r = arguments[0].getBoundingClientRect();"
            "return {x: r.x, y: r.y, width: r.width, height: r.height}",
            self._first(timeout),
        )

    def is_visible(self) -> bool:
        try:
            els = self._resolve()
            return bool(els) and _disp(self._page, els[0])
        except WebDriverException:
            return False

    def is_hidden(self) -> bool:
        return not self.is_visible()

    def is_enabled(self) -> bool:
        try:
            els = self._resolve()
            return bool(els) and els[0].is_enabled()
        except WebDriverException:
            return False

    def is_disabled(self) -> bool:
        return not self.is_enabled()

    def is_checked(self) -> bool:
        try:
            els = self._resolve()
            return bool(els) and els[0].is_selected()
        except WebDriverException:
            return False

    def is_editable(self) -> bool:
        return self._eval(
            "const e=arguments[0]; return !e.readOnly && !e.disabled", self._first()
        )

    def evaluate(self, script: str, arg=None, timeout=None):
        return self._eval(script, self._first(timeout), arg)

    def evaluate_handle(self, script: str, arg=None, timeout=None):
        return self._eval(script, self._first(timeout), arg)

    def element_handle(self, timeout=None):
        return self._first(timeout)

    def screenshot(self, path=None, timeout=None, **kw):
        el = self._first(timeout)
        return el.screenshot(path) if path else el.screenshot_as_png

    def wait_for(self, state: str = "visible", timeout=None, **kw):
        if state == "visible":
            self._visible(timeout)
        elif state == "attached":
            self._first(timeout)
        elif state in ("hidden", "detached"):
            deadline = self._t(timeout)
            while time.time() < deadline:
                try:
                    els = self._resolve()
                    if not els or (state == "hidden" and not _disp(self._page, els[0])):
                        return
                except WebDriverException:
                    if state == "detached":
                        return
                time.sleep(0.05)
            raise AssertionError(f"locator {self._chain!r} did not reach state {state}")
        else:
            raise SafariUnsupported(f"locator.wait_for state={state}")


class _IndexedLocator(SafariLocator):
    def __init__(self, page, chain, index: int, *, has_text=None):
        super().__init__(page, chain, has_text=has_text)
        self._index = index

    def _resolve(self) -> list:
        els = super()._resolve()
        return [els[self._index]] if -len(els) <= self._index < len(els) else []
