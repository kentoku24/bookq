"""viewer/gesture.js — palm exclusion, pinch arming, native-zoom guards."""
import re

import pytest
from playwright.sync_api import expect

from viewer_touch import (open_book, touch_session, pinch, drag, settle, touch_start, touch_move,
                          touch_end, track_tx, two)

SERIES = "zoom_series/series/ep-1"


@pytest.fixture
def touch(ios_page):
    s = touch_session(ios_page)
    yield s
    s.detach()


def test_fixture_series_opens_with_three_chapters(ios_page, server):
    open_book(ios_page, server, SERIES, 0, 5)
    assert ios_page.evaluate("window.chapterList.length") == 3
    assert ios_page.evaluate("window.images.filter(i => !i.isSpacer).length") == 16


def _dispatch_touch_prevented(page, selector, n_touches, type_="touchmove"):
    return page.evaluate(
        """([sel, n, type]) => {
          const el = document.querySelector(sel);
          const r = el.getBoundingClientRect();
          const mk = (i) => new Touch({identifier: i, target: el, clientX: r.left + 50 + i*40, clientY: r.top + 300});
          const touches = Array.from({length: n}, (_, i) => mk(i+1));
          const ev = new TouchEvent(type, {touches, changedTouches: touches, bubbles: true, cancelable: true});
          el.dispatchEvent(ev);
          return ev.defaultPrevented;
        }""",
        [selector, n_touches, type_],
    )


def test_two_finger_touchmove_in_viewer_is_prevented_one_finger_is_not(ios_page, server):
    open_book(ios_page, server, SERIES, 0, 5)
    assert _dispatch_touch_prevented(ios_page, "#img-container", 2) is True
    assert _dispatch_touch_prevented(ios_page, "#img-container", 1) is False
    assert _dispatch_touch_prevented(ios_page, "#img-container", 2, "touchstart") is True


def test_two_finger_in_sidebar_is_not_prevented(ios_page, server):
    open_book(ios_page, server, SERIES, 0, 5)
    ios_page.keyboard.press("f")  # open sidebar
    sidebar = ios_page.locator("#sidebar")
    if "open" not in (sidebar.get_attribute("class") or ""):
        ios_page.keyboard.press("f")
    expect(sidebar).to_have_class(re.compile(r"\bopen\b"))
    assert _dispatch_touch_prevented(ios_page, "#sidebar-scroll", 2) is False
    assert _dispatch_touch_prevented(ios_page, "#sidebar-scroll", 2, "touchstart") is False


def test_two_finger_touchmove_in_open_overview_is_prevented(ios_page, server):
    """overview.js stops touch propagation at #overview (so app.js's swipe never
    sees grid touches). The 2-finger touchmove guard must therefore live on
    `document` in the capture phase — on #viewer's bubble phase it never runs
    here, and a pinch inside the grid becomes Safari's native page zoom / tab
    overview. Measured in the iOS Simulator: pinch-in on the grid opened the
    tab switcher; a natively zoomed page then pans horizontally on swipe."""
    open_book(ios_page, server, SERIES, 0, 5)
    ios_page.evaluate("window.Overview.open()")
    expect(ios_page.locator("body")).to_have_class(re.compile(r"\boverview-open\b"))
    assert _dispatch_touch_prevented(ios_page, ".ov-body", 2) is True
    assert _dispatch_touch_prevented(ios_page, ".ov-body", 2, "touchstart") is True
    assert _dispatch_touch_prevented(ios_page, ".ov-body", 1) is False  # 1-finger scroll stays native


def test_gesturestart_is_prevented_only_inside_viewer(ios_page, server):
    open_book(ios_page, server, SERIES, 0, 5)
    prevented = ios_page.evaluate(
        """() => {
          const ev = new Event('gesturestart', {bubbles: true, cancelable: true});
          document.querySelector('#img-container').dispatchEvent(ev);
          return ev.defaultPrevented;
        }"""
    )
    assert prevented is True


def test_guards_inactive_when_no_book_is_open(ios_page, server):
    # browse root: images.length === 0 → guards must not fire
    assert _dispatch_touch_prevented(ios_page, "#img-container", 2) is False


def test_edge_palm_is_excluded_until_it_moves(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 0, 5)
    # finger 1 in the middle, finger 2 resting 8px from the bottom edge (palm)
    touch_start(touch, [{"x": 200, "y": 400, "id": 1}, {"x": 200, "y": 836, "id": 2}])
    assert ios_page.evaluate("window.ViewerGesture._debug.palmIds()") == [2]
    touch_move(touch, [{"x": 200, "y": 400, "id": 1}, {"x": 200, "y": 820, "id": 2}])  # palm moved 16px → promoted
    assert ios_page.evaluate("window.ViewerGesture._debug.palmIds()") == []
    touch_end(touch)


def test_pinch_arms_only_after_both_fingers_move_10px(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 0, 5)
    ios_page.evaluate("window.__pinchStarts = 0; window.ViewerGesture.attach(document.querySelector('#img-container'), {onStart(){ window.__pinchStarts++; }, onMove(){}, onEnd(){}})")
    pinch(touch, 195, 420, [50, 55], hold=True)      # 5px each: not armed
    assert ios_page.evaluate("window.__pinchStarts") == 0
    touch_move(touch, two(195, 420, 62))              # 12px each: armed
    assert ios_page.evaluate("window.__pinchStarts") == 1
    touch_end(touch)


def test_swipe_survives_resting_palm_and_commits_on_swipe_finger_release(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 1, 5)  # start on page 2 so both directions exist
    # palm at bottom edge (id 2) stays put the whole time
    touch_start(touch, [{"x": 300, "y": 400, "id": 1}, {"x": 200, "y": 838, "id": 2}])
    for i in range(1, 7):
        touch_move(touch, [{"x": 300 - 20 * i, "y": 400, "id": 1}, {"x": 200, "y": 838, "id": 2}])
    # Release the PALM first: must not commit anything. CDP's touchEnd
    # touchPoints are the points being RELEASED, so the palm (id 2) is what is
    # named here — naming id 1 would release the swipe finger instead and
    # commit the turn right now.
    touch_end(touch, [{"x": 200, "y": 838, "id": 2}])
    # Assert on state, not the counter: a commit animates the track for 250ms
    # and only updates #counter (and currentIdx) in the finalize that follows,
    # so BOTH a counter check and a currentIdx check pass on their first poll
    # even when the swipe has in fact committed. The inline transition on
    # #img-track is the immediate, deterministic signal — "none" while a drag
    # is live, "transform 0.25s ..." the moment a commit starts.
    assert ios_page.evaluate("document.querySelector('#img-track').style.transition") == "none"
    assert ios_page.evaluate("window.currentIdx") == 1
    expect(ios_page.locator("#counter")).to_have_text("2 / 5")
    touch_move(touch, [{"x": 300 - 140, "y": 400, "id": 1}])
    touch_end(touch, [{"x": 300 - 140, "y": 400, "id": 1}])  # swipe finger released → commit (left = previous page in RTL)
    expect(ios_page.locator("#counter")).to_have_text("1 / 5")


def test_lone_swipe_starting_on_the_bezel_still_turns_the_page(ios_page, server, touch):
    """A single finger landing within PALM_EDGE (20px) of the screen is a palm
    at touchstart, so nothing arms there. Once it moves ARM_PX it is the swipe
    finger — app.js arms the drag late, measured from where it landed, and the
    RTL 'thumb from the right bezel' page-turn keeps working."""
    open_book(ios_page, server, SERIES, 1, 5)
    settle(ios_page)
    rest = track_tx(ios_page)
    touch_start(touch, [{"x": 382, "y": 400, "id": 1}])   # 8px from the right edge
    assert ios_page.evaluate("window.ViewerGesture._debug.palmIds()") == [1]
    for x in (370, 340, 300, 260):
        touch_move(touch, [{"x": x, "y": 400, "id": 1}])
    assert ios_page.evaluate("window.ViewerGesture._debug.palmIds()") == []
    # 382 - 260 = 122px of drag, measured from the LANDING point, not from the
    # frame that crossed ARM_PX (which would show ~110px).
    assert abs(track_tx(ios_page) - rest + 122) < 2, track_tx(ios_page) - rest
    touch_end(touch, [{"x": 260, "y": 400, "id": 1}])   # left = PREVIOUS page in RTL
    expect(ios_page.locator("#counter")).to_have_text("1 / 5")


def test_lone_bezel_touch_that_never_moves_does_not_arm_anything(ios_page, server, touch):
    """A resting thumb on the bezel, lifted without moving: no drag, and the
    pending id is forgotten so the NEXT bezel touch can still arm late."""
    open_book(ios_page, server, SERIES, 1, 5)
    settle(ios_page)
    rest = track_tx(ios_page)
    touch_start(touch, [{"x": 200, "y": 838, "id": 1}])
    touch_end(touch, [{"x": 200, "y": 838, "id": 1}])
    assert track_tx(ios_page) == rest
    # second bezel touch, this one a real swipe → arms late and turns
    touch_start(touch, [{"x": 382, "y": 400, "id": 2}])
    for x in (370, 340, 300, 260):
        touch_move(touch, [{"x": x, "y": 400, "id": 2}])
    touch_end(touch, [{"x": 260, "y": 400, "id": 2}])
    expect(ios_page.locator("#counter")).to_have_text("1 / 5")


def test_initial_body_class_is_set_despite_orientation_guard(ios_page, ipad_landscape_page, server):
    assert ios_page.evaluate("document.body.className") == "portrait"
    assert ipad_landscape_page.evaluate("document.body.className") == "landscape"


def test_resize_without_dimension_change_skips_update_orientation(ios_page, server):
    open_book(ios_page, server, SERIES, 2, 5)
    runs_before = ios_page.evaluate("window.__orientDebug.runs")
    ios_page.evaluate("window.dispatchEvent(new Event('resize'))")
    assert ios_page.evaluate("window.__orientDebug.runs") == runs_before
    ios_page.set_viewport_size({"width": 390, "height": 800})
    ios_page.wait_for_function("(before) => window.__orientDebug.runs === before + 1", arg=runs_before)


def test_palm_landing_mid_swipe_does_not_rearm_the_drag(ios_page, server, touch):
    """A palm dropping onto the bezel mid-swipe is hidden from activeTouches, so
    its touchstart still looks like "one finger" — but it is the SAME finger that
    is already dragging. Re-arming would move dragStartX to wherever that finger
    is now, and the strip would snap back by exactly that much."""
    open_book(ios_page, server, SERIES, 1, 5)  # page 2 of 5: both directions exist
    settle(ios_page)
    rest = track_tx(ios_page)
    touch_start(touch, [{"x": 150, "y": 400, "id": 1}])
    for x in (170, 200, 230):
        touch_move(touch, [{"x": x, "y": 400, "id": 1}])
    # palm at the bottom edge, 80px into the swipe
    touch_start(touch, [{"x": 120, "y": 838, "id": 2}])
    touch_move(touch, [{"x": 260, "y": 400, "id": 1}, {"x": 120, "y": 838, "id": 2}])
    # 260 - 150 = 110px of drag. A re-armed dragStartX (230) would show 30px.
    assert abs(track_tx(ios_page) - rest - 110) < 2, track_tx(ios_page) - rest
    touch_end(touch, [{"x": 120, "y": 838, "id": 2}])   # palm first: commits nothing
    assert ios_page.evaluate("window.currentIdx") == 1
    touch_end(touch, [{"x": 260, "y": 400, "id": 1}])   # right = NEXT page in RTL
    expect(ios_page.locator("#counter")).to_have_text("3 / 5")
    ios_page.wait_for_function("window.currentIdx === 2")
