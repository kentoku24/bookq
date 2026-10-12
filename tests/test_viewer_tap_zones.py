"""Tap-zone drift tests: the drift-tap (F2) path and its boundary cases.

Everything here drives Chromium's real input pipeline via CDP
(`Input.dispatchTouchEvent`, see `viewer_touch.py`), because the classification
lives in the touchend handler and depends on gesture-shaped input. Cases:

- 12px drift tap  → exactly one turn (F2)
- pull-back cancel (long drag, release near the start) → no turn (bounce branch)
- center-zone drift tap → toolbar toggles AND the dragged strip offset is restored
- 20–24px fast flick from the right zone → advances FORWARD by velocity (not
  the tap-of-the-starting-zone), regression for the direction inversion
"""
from playwright.sync_api import expect  # noqa: F401 (kept for future assertions)

from viewer_touch import open_book, touch_session, touch_start, touch_move, touch_end

FOLDER = "long"

VTHRESH = 39  # min(innerWidth*0.10, VIEWER_SWIPE_THRESHOLD) for 390px
DEACT = VTHRESH - 12


def _css_int(page, expr):
    return page.evaluate(expr)


def strip_transform(page):
    return page.evaluate("getComputedStyle(document.querySelector('#img-track')).transform")


def test_drift_tap_advances_exactly_once(ios_page, server):
    page = ios_page
    open_book(page, server, FOLDER, 0, 30)
    s = touch_session(page)
    touch_start(s, [{"x": 78, "y": 500, "id": 1}])
    touch_move(s, [{"x": 90, "y": 502, "id": 1}])
    touch_end(s, [{"x": 90, "y": 502, "id": 1}])
    assert page.evaluate("window.currentIdx") == 1
    touch_start(s, [{"x": 78, "y": 500, "id": 2}])
    touch_move(s, [{"x": 90, "y": 502, "id": 2}])
    touch_end(s, [{"x": 90, "y": 502, "id": 2}])
    assert page.evaluate("window.currentIdx") == 2
    s.detach()


def test_pull_back_cancel_does_not_turn(ios_page, server):
    """Long forward drag (over vThresh, haptic armed) pulled back near the start."""
    page = ios_page
    open_book(page, server, FOLDER, 0, 30)
    base = strip_transform(page)
    s = touch_session(page)
    touch_start(s, [{"x": 78, "y": 500, "id": 1}])
    touch_move(s, [{"x": 78 + VTHRESH + 40, "y": 500, "id": 1}])   # commit-ready
    touch_move(s, [{"x": 78 + DEACT - 6, "y": 500, "id": 1}])      # pull back below deactivation
    touch_end(s, [{"x": 78 + 10, "y": 500, "id": 1}])              # release 10px from start
    assert page.evaluate("window.currentIdx") == 0
    # the drag wrote a live offset; touchend must settle it back
    page.wait_for_function(f"getComputedStyle(document.querySelector('#img-track')).transform === {base!r}", timeout=3000)
    s.detach()


def test_center_drift_tap_restores_strip_and_toggles_toolbar(ios_page, server):
    page = ios_page
    open_book(page, server, FOLDER, 0, 30)
    hidden0 = page.evaluate("document.querySelector('#toolbar').classList.contains('hidden')")
    base = strip_transform(page)
    s = touch_session(page)
    touch_start(s, [{"x": 130, "y": 500, "id": 1}])
    touch_move(s, [{"x": 142, "y": 503, "id": 1}])   # 12px drift inside center zone
    touch_end(s, [{"x": 142, "y": 503, "id": 1}])
    assert page.evaluate("window.currentIdx") == 0
    assert page.evaluate("document.querySelector('#toolbar').classList.contains('hidden')") == (not hidden0)
    page.wait_for_function(f"getComputedStyle(document.querySelector('#img-track')).transform === {base!r}", timeout=3000)
    s.detach()


def test_fast_flick_from_right_zone_advances_forward(ios_page, server):
    """22px flick inside the right zone must follow the FLICK direction,
    not the zone of the starting point — with or without measurable velocity
    (single-sample histories carry none)."""
    page = ios_page
    open_book(page, server, FOLDER, 0, 30)
    # right zone = x > 70% (390*0.7 = 273). right zone tap = previous page,
    # but a fast forward flick is a swipe → next page. Real flicks carry
    # velocity — the synthetic dispatch needs a real gap between moves for
    # moveHistory to measure one (22px / ~35ms ≈ 0.6 px/ms).
    s = touch_session(page)
    touch_start(s, [{"x": 300, "y": 500, "id": 1}])
    touch_move(s, [{"x": 305, "y": 500, "id": 1}])
    page.wait_for_timeout(35)
    touch_move(s, [{"x": 322, "y": 500, "id": 1}])
    touch_end(s, [{"x": 322, "y": 500, "id": 1}])
    # the flick goes through the commit animation (~250ms) before snapTo lands
    page.wait_for_function("window.currentIdx === 1", timeout=3000)
    s.detach()


def test_single_sample_flick_falls_back_to_distance(ios_page, server):
    """A flick compressed into one decided sample carries no measurable
    velocity; direction still follows the gesture (~22px forward), never the
    starting zone (right zone start would otherwise read as 'previous')."""
    page = ios_page
    open_book(page, server, FOLDER, 0, 30)
    s = touch_session(page)
    touch_start(s, [{"x": 300, "y": 500, "id": 1}])
    touch_move(s, [{"x": 305, "y": 500, "id": 1}])
    touch_move(s, [{"x": 322, "y": 500, "id": 1}])
    touch_end(s, [{"x": 322, "y": 500, "id": 1}])
    page.wait_for_function("window.currentIdx === 1", timeout=3000)
    s.detach()
