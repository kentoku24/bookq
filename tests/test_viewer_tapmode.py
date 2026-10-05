"""Tap direction swap regression (#tapmode-left / #tapmode-right).

Default (left mode): left tap turns forward, right tap turns back.
Right mode: sides are swapped. The mode lives in an IIFE variable only
(no persistence), the center tap toggles the toolbar and the tapmode
controls together, and out-of-range turns are silent no-ops (clamp).

New tapmode contract: both #tapmode-left/right are ALWAYS enabled
(disabled=false); the active mode is indicated by aria-pressed="true"
+ active styling. Every tap shows a "タップ送り: 左/右" toast (#toast
.visible): re-affirming the active mode keeps the mapping, tapping the
other mode switches it.

Both the touch path (CDP touchStart/touchEnd, acted on at touchend) and
the click path (mouse click on the zones) are asserted for (a) and (b).
A 500ms gap separates taps so `suppressClickFor(400)` never leaks one
tap's synthesized click into the next assertion.
"""
from pathlib import Path

from playwright.sync_api import expect

from viewer_touch import open_book, touch_session, touch_start, touch_end

FOLDER = "long"

LX, CX, RX, Y = 60, 195, 330, 500

EVIDENCE_PNG = Path(__file__).resolve().parent.parent / ".omo" / "evidence" / "tapmode.png"


def _touch_tap(page, x, y):
    s = touch_session(page)
    touch_start(s, [{"x": x, "y": y, "id": 1}])
    touch_end(s, [{"x": x, "y": y, "id": 1}])
    s.detach()


def _idx(page):
    return page.evaluate("window.currentIdx")


def _counter(page):
    return page.evaluate("document.querySelector('#counter').textContent.trim()")


def _tap_forward(page):
    return page.evaluate("window.__tapForward()")


def _btn_disabled(page, sel):
    return page.evaluate(f"document.querySelector('{sel}').disabled")


def _btn_opacity(page, sel):
    return page.evaluate(
        f"parseFloat(getComputedStyle(document.querySelector('{sel}')).opacity)"
    )


def _aria_pressed(page, sel):
    return page.evaluate(
        f"document.querySelector('{sel}').getAttribute('aria-pressed')"
    )


def _toast_text(page):
    return page.evaluate("document.querySelector('#toast').textContent")


def _toast_visible(page):
    return page.evaluate(
        "document.querySelector('#toast').classList.contains('visible')"
    )


def _goto_p(page, server, p, total):
    """Same-document hash navigation (no reload): the tap mode persists."""
    page.goto(f"{server}/#/view/{FOLDER}?p={p}")
    expect(page.locator("#counter")).to_have_text(f"{p + 1} / {total}")


def _assert_left_mode(page):
    assert _tap_forward(page) == "left"
    assert _btn_disabled(page, "#tapmode-left") is False
    assert _btn_disabled(page, "#tapmode-right") is False
    assert _aria_pressed(page, "#tapmode-left") == "true"
    assert _aria_pressed(page, "#tapmode-right") == "false"


def _assert_right_mode(page):
    assert _tap_forward(page) == "right"
    assert _btn_disabled(page, "#tapmode-right") is False
    assert _btn_disabled(page, "#tapmode-left") is False
    assert _aria_pressed(page, "#tapmode-right") == "true"
    assert _aria_pressed(page, "#tapmode-left") == "false"
    # every tap shows a toast: tapping #tapmode-right announces the right mode
    assert _toast_visible(page) is True
    assert _toast_text(page) == "タップ送り: 右"


def _hidden(page, sel):
    return page.evaluate(f"document.querySelector('{sel}').classList.contains('hidden')")


def test_default_left_mode_touch_turns(ios_page, server):
    """(a) touch path: left tap advances, right tap goes back."""
    page = ios_page
    open_book(page, server, FOLDER, 0, 30)
    assert _tap_forward(page) == "left"
    assert _btn_disabled(page, "#tapmode-left") is False
    assert _btn_disabled(page, "#tapmode-right") is False
    assert _aria_pressed(page, "#tapmode-left") == "true"
    assert _aria_pressed(page, "#tapmode-right") == "false"
    # initial setTapForward('left', {notify:false}): no toast on load
    assert _toast_visible(page) is False
    _touch_tap(page, LX, Y)
    assert _counter(page) == "2 / 30"
    assert _idx(page) == 1
    page.wait_for_timeout(500)
    _touch_tap(page, RX, Y)
    assert _idx(page) == 0
    expect(page.locator("#counter")).to_have_text("1 / 30")


def test_default_left_mode_click_turns(ios_page, server):
    """(a) click path: left click advances, right click goes back."""
    page = ios_page
    open_book(page, server, FOLDER, 0, 30)
    assert _tap_forward(page) == "left"
    page.mouse.click(LX, Y)
    page.wait_for_timeout(500)
    expect(page.locator("#counter")).to_have_text("2 / 30")
    assert _idx(page) == 1
    page.mouse.click(RX, Y)
    page.wait_for_timeout(500)
    assert _idx(page) == 0
    expect(page.locator("#counter")).to_have_text("1 / 30")


def test_right_mode_touch_turns(ios_page, server):
    """(b) touch path: after #tapmode-right, right tap advances, left tap goes back."""
    page = ios_page
    open_book(page, server, FOLDER, 0, 30)
    page.click("#tapmode-right")
    _assert_right_mode(page)
    _touch_tap(page, RX, Y)
    assert _counter(page) == "2 / 30"
    assert _idx(page) == 1
    page.wait_for_timeout(500)
    _touch_tap(page, LX, Y)
    assert _idx(page) == 0
    EVIDENCE_PNG.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(EVIDENCE_PNG))


def test_right_mode_click_turns(ios_page, server):
    """(b) click path: after #tapmode-right, right click advances, left click goes back."""
    page = ios_page
    open_book(page, server, FOLDER, 0, 30)
    page.click("#tapmode-right")
    _assert_right_mode(page)
    page.mouse.click(RX, Y)
    page.wait_for_timeout(500)
    expect(page.locator("#counter")).to_have_text("2 / 30")
    assert _idx(page) == 1
    page.mouse.click(LX, Y)
    page.wait_for_timeout(500)
    assert _idx(page) == 0


def test_left_button_restores_left_mode(ios_page, server):
    """(c) re-affirming the active mode keeps the mapping + toast; #tapmode-left switches back (aria-pressed left + toast)."""
    page = ios_page
    open_book(page, server, FOLDER, 0, 30)
    page.click("#tapmode-right")
    _assert_right_mode(page)
    # tapping the already-active mode re-affirms: mapping kept + toast
    page.click("#tapmode-right")
    assert _tap_forward(page) == "right"
    assert _toast_visible(page) is True
    assert _toast_text(page) == "タップ送り: 右"
    page.click("#tapmode-left")
    assert _tap_forward(page) == "left"
    _assert_left_mode(page)
    assert _toast_visible(page) is True
    assert _toast_text(page) == "タップ送り: 左"
    _touch_tap(page, LX, Y)
    assert _idx(page) == 1


def test_center_tap_toggles_toolbar_and_tapmode_together(ios_page, server):
    """(d) center tap hides/shows #toolbar and #tapmode-controls together."""
    page = ios_page
    open_book(page, server, FOLDER, 0, 30)
    assert _hidden(page, "#toolbar") is False
    assert _hidden(page, "#tapmode-controls") is False
    page.touchscreen.tap(CX, Y)
    page.wait_for_timeout(500)
    assert _hidden(page, "#toolbar") is True
    assert _hidden(page, "#tapmode-controls") is True
    expect(page.locator("#counter")).to_have_text("1 / 30")
    page.touchscreen.tap(CX, Y)
    page.wait_for_timeout(500)
    assert _hidden(page, "#toolbar") is False
    assert _hidden(page, "#tapmode-controls") is False
    expect(page.locator("#counter")).to_have_text("1 / 30")


def test_reload_resets_to_default_left_mode(ios_page, server):
    """(e) reload forgets the mode: back to left mode (aria-pressed left, both enabled, no toast)."""
    page = ios_page
    open_book(page, server, FOLDER, 0, 30)
    page.click("#tapmode-right")
    assert _tap_forward(page) == "right"
    page.reload()
    expect(page.locator("#counter")).to_have_text("1 / 30")
    assert _tap_forward(page) == "left"
    expect(page.locator("#tapmode-left")).to_be_enabled()
    expect(page.locator("#tapmode-right")).to_be_enabled()
    assert _aria_pressed(page, "#tapmode-left") == "true"
    assert _aria_pressed(page, "#tapmode-right") == "false"
    assert _toast_visible(page) is False


def test_boundary_taps_are_noops(ios_page, server):
    """(f) silent clamp: backward tap at first spread / forward tap at last spread keep idx."""
    page = ios_page
    # Left mode: backward is the right tap, forward is the left tap.
    open_book(page, server, FOLDER, 0, 30)
    _touch_tap(page, RX, Y)
    assert _idx(page) == 0
    expect(page.locator("#counter")).to_have_text("1 / 30")
    page.wait_for_timeout(500)
    _goto_p(page, server, 29, 30)
    _touch_tap(page, LX, Y)
    assert _idx(page) == 29
    expect(page.locator("#counter")).to_have_text("30 / 30")
    # Right mode: backward is the left tap, forward is the right tap.
    # _goto_p is same-document navigation, so the mode set here persists.
    page.wait_for_timeout(500)
    page.click("#tapmode-right")
    _assert_right_mode(page)
    _goto_p(page, server, 0, 30)
    _touch_tap(page, LX, Y)
    assert _idx(page) == 0
    page.wait_for_timeout(500)
    _goto_p(page, server, 29, 30)
    _touch_tap(page, RX, Y)
    assert _idx(page) == 29
    expect(page.locator("#counter")).to_have_text("30 / 30")
