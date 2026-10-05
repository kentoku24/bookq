"""viewer/zoom.js — absolute zoom state machine, anchor, pan, edge turn, reset."""
import pytest
from playwright.sync_api import expect

from viewer_touch import (
    drag,
    touch_start,
    open_book,
    pinch,
    settle,
    tap,
    touch_end,
    touch_move,
    touch_session,
    track_transform,
    zoom_state,
)

SERIES = "zoom_series/series/ep-1"
CX, CY = 195, 420


@pytest.fixture
def touch(ios_page):
    s = touch_session(ios_page)
    yield s
    s.detach()


def _wait_zoom(page, pred):
    page.wait_for_function(f"(() => {{ const z = window.Zoom.state(); return {pred}; }})()")


def test_zoom_layer_wraps_track_and_zones_are_outside(ios_page, server):
    open_book(ios_page, server, SERIES, 0, 5)
    assert ios_page.evaluate("document.querySelector('#img-track').parentElement.id") == "zoom-layer"
    assert ios_page.evaluate("document.querySelector('#zone-prev').parentElement.id") == "img-container"


def test_pinch_out_zooms_to_2x_and_persists(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 0, 5)
    pinch(touch, CX, CY, [40, 50, 60, 70, 80])  # ratio 2.0
    _wait_zoom(ios_page, "Math.abs(z.zoom - 2) < 0.05 && z.band === 'zoomed'")
    assert ios_page.evaluate("document.body.classList.contains('zoomed')")
    tf = ios_page.evaluate("getComputedStyle(document.querySelector('#zoom-layer')).transform")
    assert tf != "none"


def test_midpoint_anchor_keeps_point_under_fingers(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 0, 5)
    # The image point under (CX, CY) at rest must stay under (CX, CY) after a centered pinch.
    before = ios_page.evaluate(f"(() => {{ const el = document.querySelector('#img-track'); const r = el.getBoundingClientRect(); return {{x: ({CX} - r.left), y: ({CY} - r.top)}}; }})()")
    pinch(touch, CX, CY, [40, 60, 80])
    _wait_zoom(ios_page, "z.zoom > 1.9")
    after = ios_page.evaluate(f"(() => {{ const el = document.querySelector('#img-track'); const r = el.getBoundingClientRect(); const z = window.Zoom.state().zoom; return {{x: ({CX} - r.left) / z, y: ({CY} - r.top) / z}}; }})()")
    assert abs(after["x"] - before["x"]) < 3 and abs(after["y"] - before["y"]) < 3


def test_release_between_1_and_1_15_snaps_to_rest(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 0, 5)
    # 62 arms the pinch (12px per finger, over ARM_PX) at ratio 1.24; 55 walks
    # it back to ratio 1.10, inside the snap band. Holding the fingers down and
    # asserting zoom > 1.05 first proves the gesture really armed — a pinch that
    # never arms would leave zoom at 1 and make the final wait vacuous.
    pinch(touch, CX, CY, [50, 62, 55], hold=True)
    _wait_zoom(ios_page, "z.zoom > 1.05")
    touch_end(touch)
    _wait_zoom(ios_page, "z.zoom === 1 && z.band === 'rest'")


def test_shrink_band_release_bounces_back(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 0, 5)
    pinch(touch, CX, CY, [50, 40, 35])  # ratio 0.70: shrink band, above FIRE
    _wait_zoom(ios_page, "z.zoom === 1 && z.band === 'rest'")
    assert ios_page.evaluate("window.Overview ? window.Overview.isOpen() : false") is False


def test_lift_with_overview_open_does_not_leave_body_zoomed(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 0, 5)
    # onStart adds `zoomed` unconditionally (to promote the compositing layer).
    # Firing the overview on the very first tracked frame means no apply() ever
    # runs to take the class back off, and onEnd then bails out on the open
    # overview — so that branch has to clear the class itself. Without it
    # #zone-prev / #zone-next stay pointer-events:none after the overview closes.
    #
    # The repeated 24 is load-bearing: the frame that arms the pinch returns
    # before onMove, so the fire needs a second dispatch at the same ratio.
    # Any intermediate frame above FIRE would apply() and clear the class,
    # making the assertion below pass for the wrong reason.
    ios_page.evaluate(
        "(() => { window.Zoom.onOverviewFire = () => {"
        " window.Overview = { isOpen: () => true, close(){}, prebuild(){} }; }; })()"
    )
    try:
        pinch(touch, CX, CY, [60, 24, 24], hold=True)   # arms at 24, ratio 0.40 → fire
        ios_page.wait_for_function("(() => window.Overview && window.Overview.isOpen())()")
        touch_end(touch)
        assert ios_page.evaluate("document.body.classList.contains('zoomed')") is False
    finally:
        ios_page.evaluate("(() => { delete window.Overview; window.Zoom.onOverviewFire = null; })()")


def test_zoom_in_from_2x_walks_through_1_without_firing_overview(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 0, 5)
    # IIFE, not a bare arrow assignment: page.evaluate() treats an expression
    # that parses as a function as a function *body* and calls it, which would
    # spend the fire before the gesture even starts.
    ios_page.evaluate("(() => { window.__fired = 0; window.Zoom.onOverviewFire = () => { window.__fired++; }; })()")
    pinch(touch, CX, CY, [40, 60, 80])           # → 2.0
    _wait_zoom(ios_page, "z.zoom > 1.9")
    pinch(touch, CX, CY, [80, 60, 44])           # 2.0 × (44/80) = 1.10 → snaps to 1
    _wait_zoom(ios_page, "z.zoom === 1")
    assert ios_page.evaluate("window.__fired") == 0


def test_page_turn_resets_zoom_via_snapTo(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 0, 5)
    pinch(touch, CX, CY, [40, 60, 80])
    _wait_zoom(ios_page, "z.zoom > 1.9")
    ios_page.keyboard.press("ArrowLeft")  # next page
    expect(ios_page.locator("#counter")).to_have_text("2 / 5")
    _wait_zoom(ios_page, "z.zoom === 1")


def test_back_to_top_resets_zoom(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 0, 5)
    pinch(touch, CX, CY, [40, 60, 80])
    _wait_zoom(ios_page, "z.zoom > 1.9")
    ios_page.go_back()
    _wait_zoom(ios_page, "z.zoom === 1")
    assert ios_page.evaluate("document.body.classList.contains('zoomed')") is False


def test_new_gesture_clears_bounce_transition(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 0, 5)
    pinch(touch, CX, CY, [50, 40, 35])  # bounce begins
    pinch(touch, CX, CY, [40, 60], hold=True)
    assert ios_page.evaluate("document.querySelector('#zoom-layer').style.transition") in ("", "none")
    touch_end(touch)


# ── Task 5: pan / clamp / edge-turn / center zone ──────────────────────────
#
# CURRENT_SPREAD_RECT reads the spread the reader is actually on. A plain
# `querySelector('#img-track .spread-container')` returns the first container in
# DOM order, which is a *neighbour* of the anchor spread whenever the window
# starts before the current page — asserting coverage on that one would be
# asserting about the wrong box.
CURRENT_SPREAD_RECT = """(() => {
  const el = window.spreadEls[window.anchorSpread];
  const r = el.getBoundingClientRect();
  return {left: r.left, top: r.top, right: r.right, bottom: r.bottom};
})()"""


def _zoom_to_2x(page, touch):
    pinch(touch, CX, CY, [40, 60, 80])
    _wait_zoom(page, "z.zoom > 1.9")


def test_pan_moves_layer_and_clamps_to_spread(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 1, 5)
    _zoom_to_2x(ios_page, touch)
    before = zoom_state(ios_page)
    drag(touch, 300, 500, 100, 300)  # pan left/up
    after = zoom_state(ios_page)
    assert after["tx"] < before["tx"] and after["ty"] < before["ty"]
    # clamp: spread rect × zoom keeps covering the viewport
    r = ios_page.evaluate(CURRENT_SPREAD_RECT)
    vw = ios_page.evaluate("window.innerWidth")
    vh = ios_page.evaluate("window.innerHeight")
    assert r["left"] <= 0.5 and r["top"] <= 0.5
    assert r["right"] >= vw - 0.5 and r["bottom"] >= vh - 0.5
    expect(ios_page.locator("#counter")).to_have_text("2 / 5")  # no page turn


def test_left_zone_tap_does_not_turn_page_while_zoomed(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 1, 5)
    _zoom_to_2x(ios_page, touch)
    tap(touch, 60, 700)
    ios_page.wait_for_timeout(150)
    expect(ios_page.locator("#counter")).to_have_text("2 / 5")
    assert zoom_state(ios_page)["zoom"] > 1.9


def test_center_zone_tap_resets_zoom_and_keeps_toolbar(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 1, 5)
    hidden_before = ios_page.evaluate("document.querySelector('#toolbar').classList.contains('hidden')")
    _zoom_to_2x(ios_page, touch)
    tap(touch, 195, 700)
    _wait_zoom(ios_page, "z.zoom === 1")
    assert ios_page.evaluate("document.querySelector('#toolbar').classList.contains('hidden')") == hidden_before


def test_edge_turn_fires_only_when_drag_starts_with_no_room(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 1, 5)
    _zoom_to_2x(ios_page, touch)
    # Push the image fully to the RIGHT so its LEFT edge sits at the viewport
    # edge (no room left to pan further right). ONE drag is enough and is the
    # only shape that works: a second setup drag would already start clamped
    # and would itself be an edge turn.
    drag(touch, 60, 500, 380, 500)
    expect(ios_page.locator("#counter")).to_have_text("2 / 5")  # setup turned nothing
    # IIFE, not a bare assignment: page.evaluate() calls an expression that
    # parses as a function, so the trailing arrow would be invoked once on the
    # spot and the count would start at 1.
    ios_page.evaluate("(() => { window.__vib = 0; navigator.vibrate = () => { window.__vib++; return true; }; })()")
    drag(touch, 100, 500, 220, 500)  # 120px right, starts clamped → goNext
    expect(ios_page.locator("#counter")).to_have_text("3 / 5")
    _wait_zoom(ios_page, "z.zoom === 1")
    assert ios_page.evaluate("window.__vib") == 1


def test_edge_turn_at_the_last_page_settles_instead_of_leaving_the_rubber_band(ios_page, server, touch):
    """goNext returns without a snapTo at the final spread, so the turn's
    Zoom.reset never runs. panEnd must then settle the rubber band itself, or
    the spread stays displaced past the clamp until the next touch."""
    open_book(ios_page, server, "zoom_series/series/ep-3", 6, 7)  # last page of the last chapter
    _zoom_to_2x(ios_page, touch)
    drag(touch, 60, 500, 380, 500)  # push right until the left edge is clamped
    rested = zoom_state(ios_page)
    drag(touch, 100, 500, 220, 500)  # 120px right from the clamp → armed, but nothing to turn to
    expect(ios_page.locator("#counter")).to_have_text("7 / 7")
    after = zoom_state(ios_page)
    assert after["zoom"] > 1.9, after                    # still zoomed: no turn happened
    assert abs(after["tx"] - rested["tx"]) < 0.5, after  # back on the clamp, not 42px past it
    assert after["edgeArmed"] is False
    r = ios_page.evaluate(CURRENT_SPREAD_RECT)
    assert abs(r["left"]) <= 0.5, r                      # left edge flush with the viewport again


def test_edge_turn_does_not_fire_from_inertia_within_one_drag(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 1, 5)
    _zoom_to_2x(ios_page, touch)
    ios_page.evaluate("(() => { window.__vib = 0; navigator.vibrate = () => { window.__vib++; return true; }; })()")
    # one long drag that reaches the clamp mid-way and keeps going: must NOT turn
    drag(touch, 40, 500, 380, 500, steps=12)
    ios_page.wait_for_timeout(150)
    expect(ios_page.locator("#counter")).to_have_text("2 / 5")
    assert ios_page.evaluate("window.__vib") == 0  # no buzz for merely touching the clamp


def test_landscape_single_page_zoomed_can_still_turn(ipad_landscape_page, server):
    page = ipad_landscape_page
    open_book(page, server, SERIES, 1, 5)
    page.locator("#spread-toggle").click()  # 1P
    page.wait_for_function("document.body.className === 'landscape-single'")
    s = touch_session(page)
    try:
        pinch(s, 590, 410, [50, 60, 75])  # 1.5x — width still < viewW, no horizontal room
        page.wait_for_function("window.Zoom.state().zoom > 1.4")
        drag(s, 400, 400, 520, 400)  # 120px right → next page
        expect(page.locator("#counter")).to_have_text("3 / 5")
    finally:
        s.detach()


def test_remaining_finger_pans_without_new_touchstart(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 1, 5)
    pinch(touch, CX, CY, [40, 60, 80], hold=True)
    _wait_zoom(ios_page, "z.zoom > 1.9")
    # CDP's touchEnd touchPoints are the points being RELEASED — an empty list
    # releases everything. Verified with a capture-phase logger: dispatching
    # touchEnd with only id 1 yields `changedTouches=[1], touches=[2]`.
    # So finger 1 is the one named here, and finger 2 is what stays down.
    starts_before = ios_page.evaluate("window.__zoomDebug.panStarts")
    touch_end(touch, [{"x": CX - 80, "y": CY, "id": 1}])
    before = zoom_state(ios_page)
    touch_move(touch, [{"x": CX + 80 - 60, "y": CY - 40, "id": 2}])
    touch_move(touch, [{"x": CX + 80 - 120, "y": CY - 80, "id": 2}])
    after = zoom_state(ios_page)
    touch_end(touch)
    assert after["tx"] != before["tx"] or after["ty"] != before["ty"]
    # ...and the pan came from the pinch's onEnd(remaining), not from a
    # touchstart. panStarts counts only the touchstart-seeded path, so an
    # unchanged count is what makes this a handoff test rather than a
    # re-press test.
    assert ios_page.evaluate("window.__zoomDebug.panStarts") == starts_before


def test_pan_end_suppresses_click_for_400ms(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 1, 5)
    _zoom_to_2x(ios_page, touch)
    drag(touch, 300, 500, 250, 450)
    assert ios_page.evaluate("window.__suppressClickActive()") is True


def test_edge_turn_disarms_when_the_finger_comes_back(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 1, 5)
    _zoom_to_2x(ios_page, touch)
    drag(touch, 60, 500, 380, 500)  # clamp the left edge (no room right)
    expect(ios_page.locator("#counter")).to_have_text("2 / 5")
    # Two cancellations, because they take different code paths.
    #
    # (a) arm at dx=+120, come back to dx=+20: still the same edge direction,
    #     so the disarm inside the edge branch is enough.
    # (b) arm at dx=+120, then a frame that lands at dx=-40 without an
    #     intermediate small-positive frame (a fast reversal skips frames).
    #     dx's sign flip takes edgeDir to 0 and skips the edge branch
    #     ENTIRELY — only a disarm outside the branch catches this one.
    for last, path in ((120, (140, 180, 220, 120)), (60, (140, 180, 220, 60))):
        touch_start(touch, [{"x": 100, "y": 500, "id": 1}])
        for x in path:
            touch_move(touch, [{"x": x, "y": 500, "id": 1}])
        # Poll, don't read once: CDP hands the touch to the input pipeline and
        # returns, so a bare zoom_state() here still reports the state from one
        # move earlier. The poll cannot pass vacuously — dx=+120 armed it, so
        # `false` can only appear once the reversing frame has been handled.
        _wait_zoom(ios_page, "z.edgeArmed === false")
        touch_end(touch, [{"x": last, "y": 500, "id": 1}])
        ios_page.wait_for_timeout(150)
        expect(ios_page.locator("#counter")).to_have_text("2 / 5")
        assert zoom_state(ios_page)["zoom"] > 1.9


def test_edge_turn_left_at_the_right_clamp_goes_to_the_previous_page(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 1, 5)
    _zoom_to_2x(ios_page, touch)
    # Start at 360, not 380: gesture.js treats a touch landing within
    # PALM_EDGE (20px) of a screen edge as a palm and hides it from
    # activeTouches, so a drag starting at 380 in a 390px-wide viewport never
    # pans at all and the setup silently does nothing.
    drag(touch, 360, 500, 40, 500)  # push the image LEFT: right edge clamped
    expect(ios_page.locator("#counter")).to_have_text("2 / 5")
    drag(touch, 220, 500, 100, 500)  # 120px left, starts clamped → goPrev
    expect(ios_page.locator("#counter")).to_have_text("1 / 5")
    _wait_zoom(ios_page, "z.zoom === 1")


# ── drag → pinch handover: the drag must be ABORTED, not merely disarmed ─────
#
# Leaving `dragActive` true with the strip still offset means the touchend that
# ends the PINCH commits a page turn from a dxFinal the reader abandoned.


def test_second_finger_mid_drag_aborts_the_drag_and_rests_the_strip(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 1, 5)
    settle(ios_page)
    rest = track_transform(ios_page)
    drag(touch, 200, 400, 260, 400, hold=True)   # 60px, past the 8px decide threshold
    assert track_transform(ios_page) != rest, "setup: the drag never moved the strip"
    # Second finger: activeTouches() is now 2, so app.js's touchstart takes the
    # abort branch. CDP keeps id 1 down even though only id 2 is listed here
    # (verified with a capture-phase logger: touchstart changedTouches=[2],
    # touches=[1,2]).
    touch_start(touch, [{"x": 120, "y": 400, "id": 2}])
    assert track_transform(ios_page) == rest
    touch_move(touch, [{"x": 275, "y": 400, "id": 1}, {"x": 105, "y": 400, "id": 2}])
    touch_end(touch)
    ios_page.wait_for_timeout(150)
    expect(ios_page.locator("#counter")).to_have_text("2 / 5")
    assert track_transform(ios_page) == rest


def test_palm_promoted_mid_drag_aborts_the_drag(ios_page, server, touch):
    """The touchmove twin: a palm has no touchstart of its own when it is
    promoted — gesture.js simply stops hiding it, and the count goes 1 → 2
    inside a touchmove."""
    open_book(ios_page, server, SERIES, 1, 5)
    settle(ios_page)
    rest = track_transform(ios_page)
    drag(touch, 200, 400, 260, 400, hold=True)
    assert track_transform(ios_page) != rest
    touch_start(touch, [{"x": 120, "y": 838, "id": 2}])   # within PALM_EDGE of the bottom
    assert track_transform(ios_page) != rest, "a resting palm must not disturb the drag"
    touch_move(touch, [{"x": 260, "y": 400, "id": 1}, {"x": 120, "y": 800, "id": 2}])  # palm moves 38px
    assert track_transform(ios_page) == rest
    touch_end(touch)
    ios_page.wait_for_timeout(150)
    expect(ios_page.locator("#counter")).to_have_text("2 / 5")


# ── spec §5.2 gaps ───────────────────────────────────────────────────────────


def test_landscape_2p_clamp_keeps_the_whole_spread_covering_the_viewport(ipad_landscape_page, server):
    """Both pages of the anchor spread stay over the viewport, not just the one
    under the fingers: clampPan works on the `.spread-container` box."""
    page = ipad_landscape_page
    open_book(page, server, SERIES, 1, 5)
    assert page.evaluate("document.body.className") == "landscape"
    # the partner page's cell arrives with the deferred window job, one frame later
    page.wait_for_function(
        "window.spreadEls[window.anchorSpread].querySelectorAll('.page-cell').length === 2"
    )
    s = touch_session(page)
    try:
        pinch(s, 590, 410, [40, 60, 80])   # 2.0
        page.wait_for_function("window.Zoom.state().zoom > 1.9")
        vw = page.evaluate("window.innerWidth")
        # Precondition: at 2x the spread is wider than the viewport, so the x
        # axis really is clamped rather than centred (clampPan centres an axis
        # it cannot cover, and coverage would then be the wrong assertion).
        r0 = page.evaluate(CURRENT_SPREAD_RECT)
        assert r0["right"] - r0["left"] > vw + 1, r0
        assert r0["left"] <= 0.5 and r0["right"] >= vw - 0.5, (r0, vw)
        # A pan that STARTS with room can never turn a page, however far it
        # runs — so this drag only exercises the clamp.
        drag(s, 850, 410, 350, 410, steps=10)
        r1 = page.evaluate(CURRENT_SPREAD_RECT)
        assert r1["left"] <= 0.5 and r1["right"] >= vw - 0.5, (r1, vw)
        expect(page.locator("#counter")).to_have_text("2 / 5")
    finally:
        s.detach()


def test_pinch_during_a_slide_leaves_the_strip_at_a_clean_rest(ios_page, server, touch):
    """zoom.js's onStart reclaims an interrupted snapTo. Whatever page wins, the
    strip must sit exactly where a fresh snapTo would put it — otherwise the
    anchor frame is stale and the next page turn slides from the wrong place."""
    open_book(ios_page, server, SERIES, 1, 5)
    settle(ios_page)
    tap(touch, 60, 700)   # left zone = NEXT page in RTL
    ios_page.wait_for_function("window.sliding === true", timeout=3000)
    pinch(touch, CX, CY, [40, 60, 75], hold=True)   # 1.5x, mid-slide
    _wait_zoom(ios_page, "z.zoom > 1.4")
    touch_end(touch)
    settle(ios_page, 3)
    idx = ios_page.evaluate("window.currentIdx")
    assert idx in (1, 2), idx
    before = track_transform(ios_page)
    ios_page.evaluate("window.viewerSnapTo(window.currentIdx, false)")
    settle(ios_page, 3)
    assert track_transform(ios_page) == before


def test_resize_keeps_the_zoom_only_when_the_viewport_really_changed(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 1, 5)
    _zoom_to_2x(ios_page, touch)
    # same-size resize (iOS keyboard / URL bar): updateOrientation early-returns,
    # so no snapTo runs and the zoom survives
    ios_page.evaluate("window.dispatchEvent(new Event('resize'))")
    ios_page.wait_for_timeout(120)
    assert zoom_state(ios_page)["zoom"] > 1.9
    ios_page.set_viewport_size({"width": 390, "height": 800})
    _wait_zoom(ios_page, "z.zoom === 1")
