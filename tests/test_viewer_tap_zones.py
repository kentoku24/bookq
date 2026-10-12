"""Tap-zone drift tests: the drift-tap (F2) path and its boundary cases.

Everything here drives Chromium's real input pipeline via CDP
(`Input.dispatchTouchEvent`, see `viewer_touch.py`), because the classification
lives in the touchend handler and depends on gesture-shaped input. Cases:

- 18px drift tap  → exactly one turn (F2)
- pull-back cancel (long drag, release near the start) → no turn (bounce branch)
- center-zone drift tap → toolbar toggles AND the dragged strip offset is restored
- boundary-crossing drift → zone resolves at the LANDING spot
- 22px fast flick from the right zone → advances FORWARD by velocity (not
  the tap-of-the-starting-zone), regression for the direction inversion
- classifier unit pins: every branch of `classifyTouchEnd` directly, including
  the dt<=1 same-ms fallback and the exactly-20px boundary

CDP caveat (measured, not assumed): in this Chromium build a touchMove that
stays within ~12px of the touchStart never reaches the page — only moves past
~16px are delivered. Drift integration therefore uses 18px (inside the <=20px
tap band, above the delivery floor); the 8–16px sub-range behaves identically
by code inspection (`dragMaxTravel <= TAP_MAX_TRAVEL`) and is pinned per-value
by `test_classifier_pins_all_branches`. Moves sent after a real gap
(`wait_for_timeout(35)`) are delivered; back-to-back dispatches after the
start degrade to the stationary-tap path and prove nothing about the branch
under test. For the same reason the distance fallback (`!velocityKnown`,
reachable only on same-ms dispatch collapse) cannot be forced through CDP at
all — it is pinned deterministically by the classifier test instead.
"""
import json

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
    touch_move(s, [{"x": 96, "y": 500, "id": 1}])
    touch_end(s, [{"x": 96, "y": 500, "id": 1}])
    assert page.evaluate("window.currentIdx") == 1
    assert page.evaluate("window.__lastTouchEndVia") == "drift-tap"
    touch_start(s, [{"x": 78, "y": 500, "id": 2}])
    touch_move(s, [{"x": 96, "y": 500, "id": 2}])
    touch_end(s, [{"x": 96, "y": 500, "id": 2}])
    assert page.evaluate("window.currentIdx") == 2
    assert page.evaluate("window.__lastTouchEndVia") == "drift-tap"
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
    assert page.evaluate("window.__lastTouchEndVia") == "bounce"
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
    touch_move(s, [{"x": 148, "y": 500, "id": 1}])   # 18px drift inside center zone
    touch_end(s, [{"x": 148, "y": 500, "id": 1}])
    assert page.evaluate("window.currentIdx") == 0
    assert page.evaluate("window.__lastTouchEndVia") == "drift-tap"
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
    # velocity — the synthetic dispatch needs a real gap after the start for
    # the moves to reach the page at all (22px / ~70ms ≈ 0.3 px/ms).
    s = touch_session(page)
    touch_start(s, [{"x": 300, "y": 500, "id": 1}])
    page.wait_for_timeout(35)
    touch_move(s, [{"x": 305, "y": 500, "id": 1}])
    page.wait_for_timeout(35)
    touch_move(s, [{"x": 322, "y": 500, "id": 1}])
    touch_end(s, [{"x": 322, "y": 500, "id": 1}])
    # the flick goes through the commit animation (~250ms) before snapTo lands
    page.wait_for_function("window.currentIdx === 1", timeout=3000)
    assert page.evaluate("window.__lastTouchEndVia") == "velocity"
    s.detach()


def test_drift_tap_resolves_zone_at_landing_spot(ios_page, server):
    """An 18px drift crossing the 30% boundary resolves the zone at the LANDING
    spot (consistent with the stationary-tap path): start in the left zone
    (x=102) but land in the center (x=120) → toolbar toggles, no page turn.
    Resolving at the start would have turned the page instead."""
    page = ios_page
    open_book(page, server, FOLDER, 0, 30)
    hidden0 = page.evaluate("document.querySelector('#toolbar').classList.contains('hidden')")
    s = touch_session(page)
    touch_start(s, [{"x": 102, "y": 500, "id": 1}])
    touch_move(s, [{"x": 120, "y": 500, "id": 1}])
    touch_end(s, [{"x": 120, "y": 500, "id": 1}])
    assert page.evaluate("window.currentIdx") == 0
    assert page.evaluate("window.__lastTouchEndVia") == "drift-tap"
    assert page.evaluate("document.querySelector('#toolbar').classList.contains('hidden')") == (not hidden0)
    s.detach()


def test_classifier_pins_all_branches(ios_page, server):
    """Direct pins for every classifyTouchEnd branch, including inputs CDP
    timing cannot force: the dt<=1 same-ms fallback and the exactly-20px
    tap-band boundary."""
    page = ios_page
    open_book(page, server, FOLDER, 0, 30)
    cases = [
        # swipe-ready path wins over everything (dragPx decides)
        ({"swipeReady": True, "dragPx": 50, "velocity": 0, "known": True,
          "dxFinal": 50, "travel": 50}, 1, "swipe-ready"),
        # measured fast flick commits by velocity
        ({"swipeReady": False, "dragPx": 0, "velocity": 0.6, "known": True,
          "dxFinal": 22, "travel": 22}, 1, "velocity"),
        # same-ms dispatch: samples held but nothing measured → distance
        # fallback still follows the gesture, never the starting zone
        ({"swipeReady": False, "dragPx": 0, "velocity": 0, "known": False,
          "dxFinal": 22, "travel": 22}, 1, "distance"),
        ({"swipeReady": False, "dragPx": 0, "velocity": 0, "known": False,
          "dxFinal": -22, "travel": 22}, -1, "distance"),
        # measured pull-back cancel: known ≈0 after a long travel → bounce,
        # never re-read as a forward flick (hysteresis)
        ({"swipeReady": False, "dragPx": 0, "velocity": 0.01, "known": True,
          "dxFinal": 10, "travel": 100}, 0, "bounce"),
        # the hysteresis-test shape: multi-sample SAME-MS history (velocity 0
        # but known) with a far landing must bounce, never fall back —
        # gating `known` on dt instead would turn this into a flick
        ({"swipeReady": False, "dragPx": 0, "velocity": 0, "known": True,
          "dxFinal": 45, "travel": 75}, 0, "bounce"),
        # velocity sign disagreeing with landing direction → no commit
        ({"swipeReady": False, "dragPx": 0, "velocity": -0.6, "known": True,
          "dxFinal": 22, "travel": 22}, 0, "bounce"),
        # slow exactly-20px drift → tap (the 1px bounce hole, now closed)
        ({"swipeReady": False, "dragPx": 0, "velocity": 0, "known": True,
          "dxFinal": 20, "travel": 20}, 0, "drift-tap"),
        # ordinary drift taps
        ({"swipeReady": False, "dragPx": 0, "velocity": 0, "known": True,
          "dxFinal": 12, "travel": 12}, 0, "drift-tap"),
        ({"swipeReady": False, "dragPx": 0, "velocity": 0, "known": True,
          "dxFinal": 0, "travel": 0}, 0, "drift-tap"),
    ]
    for g, exp_adv, exp_via in cases:
        r = page.evaluate(f"window.__classifyTouchEnd({json.dumps(g)})")
        assert (r["advanced"], r["via"]) == (exp_adv, exp_via), g
