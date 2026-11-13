"""expect() — Playwright-expect subset with retry-until-timeout semantics.

conftest monkeypatches ``playwright.sync_api.expect`` with this when
``BROWSER=safari``, so test files keep their ``from playwright.sync_api import
expect`` unchanged. Matchers cover only what the suite uses; anything else
raises a clear error.
"""

from __future__ import annotations

import re
import time
from typing import Any

from tests.safari.locator import SafariLocator
from tests.safari.page import SafariPage


def _matches(expected, actual: str | None) -> bool:
    actual = actual or ""
    if hasattr(expected, "search"):
        return bool(expected.search(actual))
    if isinstance(expected, (list, tuple)):
        return False
    return actual == expected


def _contains(expected, actual: str | None) -> bool:
    actual = actual or ""
    if hasattr(expected, "search"):
        return bool(expected.search(actual))
    return expected in actual


class _Expectation:
    def __init__(self, actual: Any):
        self._actual = actual

    # ── plumbing ────────────────────────────────────────────────────────────

    def _timeout(self, kw) -> float:
        return kw.get("timeout") or self._default_timeout()

    def _default_timeout(self) -> float:
        a = self._actual
        if isinstance(a, SafariLocator):
            return getattr(a._page, "_expect_timeout", a._page._default_timeout)
        if isinstance(a, SafariPage):
            return getattr(a, "_expect_timeout", a._default_timeout)
        return 5000.0

    def _retry(self, check, describe: str, timeout_ms: float, negate: bool):
        deadline = time.time() + timeout_ms / 1000.0
        last = None
        while True:
            try:
                last = check()
            except Exception as e:  # element not found yet, etc.
                last = e
            ok = (last is True) if not negate else (last is not True)
            if ok:
                return
            if time.time() >= deadline:
                neg = "not " if negate else ""
                raise AssertionError(
                    f"expect {neg}{describe} timed out ({timeout_ms}ms); last={last!r}"
                )
            time.sleep(0.05)

    # ── locator matchers ────────────────────────────────────────────────────

    def _loc(self) -> SafariLocator:
        if not isinstance(self._actual, SafariLocator):
            raise AssertionError(f"matcher needs a locator, got {type(self._actual)}")
        return self._actual

    def to_have_text(self, expected, timeout=None, **kw):
        loc = self._loc()

        def check():
            if isinstance(expected, (list, tuple)):
                return [e.text_content() or "" for e in loc.all()] == list(expected)
            return bool(loc._resolve()) and _matches(expected, loc.text_content())

        self._retry(check, f"to_have_text({expected!r})", self._timeout({"timeout": timeout}), False)

    def to_contain_text(self, expected, timeout=None, **kw):
        loc = self._loc()
        self._retry(
            lambda: bool(loc._resolve()) and _contains(expected, loc.text_content()),
            f"to_contain_text({expected!r})", self._timeout({"timeout": timeout}), False)

    def not_to_have_text(self, expected, timeout=None, **kw):
        loc = self._loc()

        def check():
            if isinstance(expected, (list, tuple)):
                return [e.text_content() or "" for e in loc.all()] == list(expected)
            return bool(loc._resolve()) and _matches(expected, loc.text_content())

        self._retry(check, f"to_have_text({expected!r})", self._timeout({"timeout": timeout}), True)

    def not_to_contain_text(self, expected, timeout=None, **kw):
        loc = self._loc()
        self._retry(
            lambda: bool(loc._resolve()) and _contains(expected, loc.text_content()),
            f"to_contain_text({expected!r})", self._timeout({"timeout": timeout}), True)

    def to_have_class(self, expected, timeout=None, **kw):
        loc = self._loc()
        self._retry(
            lambda: bool(loc._resolve()) and _matches(expected, loc.get_attribute("class")),
            f"to_have_class({expected!r})", self._timeout({"timeout": timeout}), False)

    def not_to_have_class(self, expected, timeout=None, **kw):
        loc = self._loc()
        self._retry(
            lambda: bool(loc._resolve()) and _matches(expected, loc.get_attribute("class")),
            f"to_have_class({expected!r})", self._timeout({"timeout": timeout}), True)

    def to_have_count(self, n: int, timeout=None, **kw):
        loc = self._loc()
        self._retry(
            lambda: loc.count() == n,
            f"to_have_count({n})", self._timeout({"timeout": timeout}), False)

    def to_have_attribute(self, name: str, value, timeout=None, **kw):
        loc = self._loc()
        self._retry(
            lambda: bool(loc._resolve()) and _matches(value, loc.get_attribute(name)),
            f"to_have_attribute({name!r}, {value!r})", self._timeout({"timeout": timeout}), False)

    def to_have_css(self, name: str, value, timeout=None, **kw):
        loc = self._loc()

        def css():
            els = loc._resolve()
            if not els:
                return False
            v = loc._eval(
                "return getComputedStyle(arguments[0]).getPropertyValue(arguments[1])",
                els[0], name)
            return _matches(value, v)

        self._retry(css, f"to_have_css({name!r}, {value!r})", self._timeout({"timeout": timeout}), False)

    def to_be_visible(self, timeout=None, **kw):
        loc = self._loc()
        self._retry(lambda: loc.is_visible(), "to_be_visible", self._timeout({"timeout": timeout}), False)

    def to_be_hidden(self, timeout=None, **kw):
        loc = self._loc()
        self._retry(lambda: loc.is_hidden(), "to_be_hidden", self._timeout({"timeout": timeout}), False)

    def not_to_be_visible(self, timeout=None, **kw):
        loc = self._loc()
        self._retry(lambda: loc.is_visible(), "to_be_visible", self._timeout({"timeout": timeout}), True)

    def to_be_enabled(self, timeout=None, **kw):
        loc = self._loc()
        self._retry(lambda: loc.is_enabled(), "to_be_enabled", self._timeout({"timeout": timeout}), False)

    def to_be_disabled(self, timeout=None, **kw):
        loc = self._loc()
        self._retry(lambda: loc.is_disabled(), "to_be_disabled", self._timeout({"timeout": timeout}), False)

    def to_be_empty(self, timeout=None, **kw):
        loc = self._loc()
        self._retry(
            lambda: bool(loc._resolve()) and not (loc.text_content() or "").strip(),
            "to_be_empty", self._timeout({"timeout": timeout}), False)

    def not_to_be_empty(self, timeout=None, **kw):
        loc = self._loc()
        self._retry(
            lambda: bool(loc._resolve()) and not (loc.text_content() or "").strip(),
            "to_be_empty", self._timeout({"timeout": timeout}), True)

    def to_be_checked(self, timeout=None, **kw):
        loc = self._loc()
        self._retry(lambda: loc.is_checked(), "to_be_checked", self._timeout({"timeout": timeout}), False)

    def to_have_value(self, expected, timeout=None, **kw):
        loc = self._loc()
        self._retry(
            lambda: _matches(expected, loc.input_value()),
            f"to_have_value({expected!r})", self._timeout({"timeout": timeout}), False)

    # ── page matchers ─────────────────────────────────────────────────────────

    def _page(self) -> SafariPage:
        if not isinstance(self._actual, SafariPage):
            raise AssertionError(f"matcher needs a page, got {type(self._actual)}")
        return self._actual

    def to_have_url(self, expected, timeout=None, **kw):
        page = self._page()

        def check():
            u = page.url
            if hasattr(expected, "search"):
                return bool(expected.search(u))
            if isinstance(expected, str) and "*" in expected:
                return re.fullmatch(re.escape(expected).replace(r"\*\*", ".*").replace(r"\*", "[^/]*"), u) is not None
            return u == expected

        self._retry(check, f"to_have_url({expected!r})", self._timeout({"timeout": timeout}), False)

    def to_have_title(self, expected, timeout=None, **kw):
        page = self._page()
        self._retry(lambda: _matches(expected, page.title()), f"to_have_title({expected!r})",
                    self._timeout({"timeout": timeout}), False)

    # ── generic ───────────────────────────────────────────────────────────────

    def to_equal(self, expected, **kw):
        assert self._actual == expected, f"expected {expected!r}, got {self._actual!r}"

    def to_be(self, expected, **kw):
        assert self._actual == expected, f"expected {expected!r}, got {self._actual!r}"


def expect(actual: Any) -> _Expectation:
    return _Expectation(actual)
