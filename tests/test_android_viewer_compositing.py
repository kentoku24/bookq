"""Android viewer controls must be opaque and unblurred.

`.ua-android` already disables the toolbar/sidebar `backdrop-filter`; these
tests pin the same treatment on the floating controls — `#tapmode-controls
button` and `#spread-controls>*` (whose children are #spread-shift /
#spread-toggle) — and preserve the non-Android frosted styling. Functional
assertions only — no timing thresholds.
"""
import pytest
from playwright.sync_api import expect

from conftest import IPAD_UA

ANDROID_UA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)

FOLDER = "long"  # 30 pages, single chapter

CONTROL_SELECTORS = [
    "#tapmode-left",
    "#tapmode-right",
    "#spread-shift",
    "#spread-toggle",
]


def _android_context(browser, width, height):
    return browser.new_context(
        viewport={"width": width, "height": height},
        user_agent=ANDROID_UA,
        has_touch=True,
        is_mobile=True,
        device_scale_factor=1.75,
    )


def _open_book(page, server):
    page.goto(f"{server}/#/view/{FOLDER}?p=2")
    page.wait_for_function("window.images && window.images.length === 30")
    expect(page.locator("#counter")).to_have_text("3 / 30")


def _computed(page, selector, prop):
    return page.evaluate(
        "sel => getComputedStyle(document.querySelector(sel))." + prop, selector
    )


@pytest.mark.parametrize(
    "width,height,landscape",
    [(686, 977, False), (1098, 686, True)],
    ids=["portrait", "landscape"],
)
def test_android_controls_opaque_unblurred(browser, server, width, height, landscape):
    context = _android_context(browser, width, height)
    try:
        page = context.new_page()
        _open_book(page, server)

        # Server emitted the class from the request UA header.
        assert page.evaluate(
            "document.documentElement.classList.contains('ua-android')"
        ) is True

        # Pre-existing treatment stays put.
        assert _computed(page, "#toolbar", "backdropFilter") == "none"
        assert _computed(page, "#sidebar", "backdropFilter") == "none"

        # Floating controls: blur off even when their control strip is hidden
        # (computed style resolves regardless of display).
        for sel in CONTROL_SELECTORS:
            assert _computed(page, sel, "backdropFilter") == "none", sel

        # The spread strip is landscape-only; the tap-mode buttons are always
        # present. Everything tappable keeps a >=44px hit area.
        assert (
            page.locator("#spread-controls").evaluate("el => getComputedStyle(el).display")
            == ("flex" if landscape else "none")
        )
        buttons = CONTROL_SELECTORS if landscape else CONTROL_SELECTORS[:2]
        for sel in buttons:
            rect = page.locator(sel).bounding_box()
            assert rect["width"] >= 44 and rect["height"] >= 44, sel

        # Opaque fallbacks replace the frosted rgba() fills: inactive #222,
        # selected #26364c, with the active text/border colours intact.
        assert _computed(page, "#tapmode-right", "backgroundColor") == "rgb(34, 34, 34)"
        assert _computed(page, "#tapmode-left", "backgroundColor") == "rgb(38, 54, 76)"
        assert page.locator("#tapmode-left").get_attribute("aria-pressed") == "true"
        assert _computed(page, "#tapmode-left", "color") == "rgb(138, 180, 248)"
        assert "74, 122, 196" in _computed(page, "#tapmode-left", "borderColor")

        # Switch mode: the pressed state follows the click, still opaque.
        page.locator("#tapmode-right").click()
        page.mouse.move(10, 500)  # pointer off the controls → stable, un-hovered read
        assert page.locator("#tapmode-right").get_attribute("aria-pressed") == "true"
        assert page.locator("#tapmode-left").get_attribute("aria-pressed") == "false"
        assert _computed(page, "#tapmode-right", "backgroundColor") == "rgb(38, 54, 76)"
        assert _computed(page, "#tapmode-left", "backgroundColor") == "rgb(34, 34, 34)"
    finally:
        context.close()


@pytest.mark.parametrize(
    "ua_label,context_kwargs",
    [
        ("desktop", {"viewport": {"width": 1280, "height": 800}}),
        (
            "ipad",
            {
                "viewport": {"width": 820, "height": 1180},
                "user_agent": IPAD_UA,
                "has_touch": True,
                "is_mobile": True,
                "device_scale_factor": 2,
            },
        ),
    ],
    ids=["desktop-default-ua", "ipad-ua"],
)
def test_non_android_controls_keep_blur(browser, server, ua_label, context_kwargs):
    context = browser.new_context(**context_kwargs)
    try:
        page = context.new_page()
        _open_book(page, server)
        assert page.evaluate(
            "document.documentElement.classList.contains('ua-android')"
        ) is False
        for sel in CONTROL_SELECTORS:
            assert _computed(page, sel, "backdropFilter") == "blur(12px)", sel
    finally:
        context.close()
