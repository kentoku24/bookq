"""viewer/overview.js — grid, entry/exit, stacking, windowing."""
import re

import pytest
from playwright.sync_api import expect

from viewer_touch import (
    open_book,
    settle,
    pinch,
    tap,
    touch_end,
    touch_move,
    touch_session,
    two,
    zoom_state,
)

SERIES = "zoom_series/series/ep-1"
CX, CY = 195, 420


@pytest.fixture
def touch(ios_page):
    s = touch_session(ios_page)
    yield s
    s.detach()


def _open_overview(page):
    page.keyboard.press("g")
    page.wait_for_function("window.Overview.isOpen()")


def _settle_scroll(page):
    """show() scrolls the current cell into the upper third, which arms the
    inertia guard. Wait it out so a deliberate tap is not swallowed.

    Let two frames pass first: the scroll event from a just-issued scroll
    (show(), scroll_into_view_if_needed) is dispatched asynchronously in the
    next rendering step. Checking before it arrives passes trivially — right
    after goto() performance.now() is already > 150 while lastScrollTs is
    still 0 — and on a slow runner the late event then swallows the tap."""
    settle(page)
    page.wait_for_function(
        "performance.now() - window.Overview._debug.lastScrollTs() > 150"
    )


def test_g_opens_overview_with_sections_rtl_and_current_at_third(ios_page, server):
    open_book(ios_page, server, "zoom_series/series/ep-2", 1, 4)
    _open_overview(ios_page)
    assert ios_page.locator("#overview .ov-chapter").count() == 3
    assert ios_page.locator("#overview .ov-chapter .ov-heading").all_text_contents() == ["ep-1", "ep-2", "ep-3"]
    assert ios_page.locator("#overview .ov-cell").count() == 16  # spacers dropped
    # RTL: first cell of the first section is at the RIGHT edge
    first = ios_page.locator("#overview .ov-chapter:nth-child(1) .ov-cell").first.bounding_box()
    second = ios_page.locator("#overview .ov-chapter:nth-child(1) .ov-cell").nth(1).bounding_box()
    assert first["x"] > second["x"]
    # all cells 3:4
    ratios = ios_page.evaluate(
        "Array.from(document.querySelectorAll('.ov-cell')).map(c => { const r = c.getBoundingClientRect(); return r.width / r.height; })"
    )
    assert all(abs(r - 0.75) < 0.02 for r in ratios), ratios
    # current cell highlighted and positioned around the upper third
    cur = ios_page.locator("#overview .ov-cell.cur")
    assert cur.count() == 1
    box = cur.bounding_box()
    vh = ios_page.evaluate("window.innerHeight")
    assert vh * 0.15 < box["y"] + box["height"] / 2 < vh * 0.5
    assert "ep-2" in ios_page.locator("#overview .ov-chip").text_content()


def test_prebuilt_grid_does_not_fetch_thumbnails(ios_page, server):
    """The prebuilt grid is laid out (opacity 0) behind the reader. Lazy loading
    keys on scrollport intersection, not visibility, so a live `src` there would
    fetch two screens of thumbnails on every book open — the bandwidth the
    staged page prefetch exists to protect. URLs park in data-src until show()."""
    open_book(ios_page, server, SERIES, 0, 5)
    ios_page.evaluate("window.Overview.prebuild()")
    ios_page.wait_for_function("!!window.Overview.cellCenter()")
    assert ios_page.evaluate("window.Overview.isOpen()") is False
    assert ios_page.locator("#overview .ov-cell img").count() == 16
    assert ios_page.locator("#overview .ov-cell img[src]").count() == 0
    assert ios_page.locator("#overview .ov-cell img[data-src*='/thumb/']").count() == 16
    _open_overview(ios_page)
    ios_page.wait_for_function(
        "document.querySelectorAll('#overview .ov-cell img[data-src]').length === 0"
    )
    assert ios_page.locator("#overview .ov-cell img[src*='/thumb/']").count() == 16


def test_cells_use_thumb_url_and_fall_back_to_image(ios_page, server):
    open_book(ios_page, server, SERIES, 0, 5)
    _open_overview(ios_page)
    img = ios_page.locator("#overview .ov-cell img").first
    src = img.get_attribute("src")
    assert "/thumb/" in src and "w=400" in src
    fallback = ios_page.evaluate(
        """(() => { const img = document.querySelector('#overview .ov-cell img'); img.dispatchEvent(new Event('error')); return img.getAttribute('src'); })()"""
    )
    assert "/image/" in fallback


def test_overview_sits_above_toolbar_and_slider_is_inert(ios_page, server):
    open_book(ios_page, server, SERIES, 2, 5)
    _open_overview(ios_page)
    z_ov = int(ios_page.evaluate("getComputedStyle(document.querySelector('#overview')).zIndex"))
    assert z_ov > 10
    assert ios_page.evaluate("getComputedStyle(document.querySelector('#toolbar')).pointerEvents") == "none"
    # inert as well as pointer-events:none — Tab must not walk into the chrome
    # hidden behind the grid.
    assert ios_page.evaluate("document.querySelector('#toolbar').inert") is True
    assert ios_page.evaluate("document.querySelector('#spread-controls').inert") is True
    idx = ios_page.evaluate("window.currentIdx")
    # click where the slider is: must not change the page
    box = ios_page.locator("#page-slider").bounding_box()
    ios_page.mouse.click(box["x"] + 10, box["y"] + box["height"] / 2)
    assert ios_page.evaluate("window.currentIdx") == idx


def test_close_paths_esc_button_and_g_toggle(ios_page, server):
    open_book(ios_page, server, SERIES, 0, 5)
    # The ✕ carries `hidden` in the markup and is unhidden by show(): a stale
    # style.css served by the SW must not leave it floating over the viewer.
    assert ios_page.evaluate("document.querySelector('#overview-close').hidden") is True
    _open_overview(ios_page)
    assert ios_page.evaluate("document.querySelector('#overview-close').hidden") is False
    ios_page.keyboard.press("Escape")
    ios_page.wait_for_function("!window.Overview.isOpen()")
    assert ios_page.evaluate("document.querySelector('#overview-close').hidden") is True
    assert ios_page.evaluate("document.querySelector('#toolbar').inert") is False
    # Esc did not reach the sidebar handler
    expect(ios_page.locator("#sidebar")).not_to_have_class(re.compile(r"\bopen\b"))
    _open_overview(ios_page)
    ios_page.locator("#overview-close").click()
    ios_page.wait_for_function("!window.Overview.isOpen()")
    _open_overview(ios_page)
    ios_page.keyboard.press("g")
    ios_page.wait_for_function("!window.Overview.isOpen()")
    # Cmd+G ignored
    ios_page.keyboard.press("Meta+g")
    assert ios_page.evaluate("window.Overview.isOpen()") is False


def test_tap_cell_jumps_to_page_and_closes(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 0, 5)
    _open_overview(ios_page)
    cell = ios_page.locator("#overview .ov-cell[data-gidx]").nth(9)  # somewhere in ep-2/ep-3
    gidx = int(cell.get_attribute("data-gidx"))
    cell.scroll_into_view_if_needed()
    _settle_scroll(ios_page)
    b = cell.bounding_box()
    tap(touch, b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)
    ios_page.wait_for_function("!window.Overview.isOpen()")
    assert ios_page.evaluate("window.currentIdx") == gidx
    assert ios_page.evaluate("window.__suppressClickActive()") is True


def test_tap_during_scroll_inertia_does_not_exit(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 0, 5)
    _open_overview(ios_page)
    _settle_scroll(ios_page)
    # A far-future stamp is unconditionally inside `now - lastScrollTs < 100`,
    # so the guard is exercised without racing a real 100ms window.
    ios_page.evaluate(
        "window.Overview._debug.setLastScrollTs(performance.now() + 1e6)"
    )
    cell = ios_page.locator("#overview .ov-cell").nth(3)
    b = cell.bounding_box()
    tap(touch, b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)
    ios_page.wait_for_timeout(50)
    assert ios_page.evaluate("window.Overview.isOpen()") is True


def test_menu_button_closes_overview_first(ios_page, server):
    open_book(ios_page, server, SERIES, 0, 5)
    _open_overview(ios_page)
    ios_page.evaluate("document.querySelector('#menu-btn').click()")
    ios_page.wait_for_function("!window.Overview.isOpen()")


def test_f_closes_overview_before_opening_the_sidebar(ios_page, server):
    """F is #menu-btn's keyboard twin; the sidebar must not slide over the grid."""
    open_book(ios_page, server, SERIES, 0, 5)
    _open_overview(ios_page)
    ios_page.keyboard.press("f")
    ios_page.wait_for_function("!window.Overview.isOpen()")
    assert ios_page.evaluate("document.body.classList.contains('overview-open')") is False


def test_back_closes_overview(ios_page, server):
    open_book(ios_page, server, SERIES, 0, 5)
    _open_overview(ios_page)
    ios_page.go_back()
    ios_page.wait_for_function("!window.Overview.isOpen()")
    assert ios_page.evaluate("document.body.classList.contains('overview-open')") is False
    # close() re-arms prebuild() on idle; on the teardown path images is [] by
    # the time that runs, so the grid must still be empty afterwards.
    # (requestIdleCallback's timeout is 1000ms.)
    ios_page.wait_for_timeout(1400)
    assert ios_page.evaluate("document.querySelector('#overview .ov-body').children.length") == 0


def test_g_is_noop_without_a_book(ios_page, server):
    ios_page.keyboard.press("g")
    assert ios_page.evaluate("window.Overview.isOpen()") is False


def test_ipad_landscape_has_seven_columns(ipad_landscape_page, server):
    page = ipad_landscape_page
    open_book(page, server, SERIES, 0, 5)
    page.keyboard.press("g")
    page.wait_for_function("window.Overview.isOpen()")
    cols = page.evaluate(
        "getComputedStyle(document.querySelector('#overview .ov-grid')).gridTemplateColumns.split(' ').length"
    )
    assert cols == 7


def test_relayout_on_viewer_width_change(ipad_landscape_page, server):
    page = ipad_landscape_page
    open_book(page, server, SERIES, 0, 5)
    page.keyboard.press("g")
    page.wait_for_function("window.Overview.isOpen()")
    h1 = page.locator("#overview .ov-cell").first.bounding_box()["height"]
    page.set_viewport_size({"width": 820, "height": 1180})
    page.wait_for_function(
        f"document.querySelector('#overview .ov-cell').getBoundingClientRect().height !== {h1}"
    )


def test_huge_book_windows_imgs_but_keeps_full_scroll(ios_page, server):
    open_book(ios_page, server, "huge/book/part2", 100, 351)
    _open_overview(ios_page)
    assert ios_page.locator("#overview .ov-cell").count() == 701
    with_img = ios_page.locator("#overview .ov-cell img").count()
    assert 380 <= with_img <= 420, with_img  # ±200 around current, clipped by ends
    win = ios_page.evaluate("window.Overview._debug.imgWindow()")
    cur = ios_page.evaluate("window.currentIdx")
    assert win["lo"] <= cur <= win["hi"]


# ── Task 7: pinch entry (fire / drift / FLIP) and exit (crossfade / pinch-out) ──


def test_pinch_in_to_half_opens_overview_once_with_hysteresis(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 2, 5)
    ios_page.evaluate("(() => { window.__vib = 0; navigator.vibrate = () => { window.__vib++; return true; }; })()")
    pinch(touch, CX, CY, [50, 40, 30, 24], hold=True)  # ratio 0.48 → fire
    ios_page.wait_for_function("window.Overview.isOpen()")
    assert ios_page.evaluate("window.__vib") == 1
    # widen above REARM then squeeze again: the latch may re-arm, but the
    # overview is already open, so show() is a no-op and nothing double-opens.
    touch_move(touch, two(CX, CY, 40))
    touch_move(touch, two(CX, CY, 24))
    touch_end(touch)
    assert ios_page.evaluate("window.Overview.isOpen()") is True
    assert ios_page.evaluate("window.__vib") == 1


def test_pinch_to_0_51_does_not_open(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 2, 5)
    pinch(touch, CX, CY, [50, 40, 26])  # 0.52
    ios_page.wait_for_function("window.Zoom.state().zoom === 1")
    assert ios_page.evaluate("window.Overview.isOpen()") is False


def test_shrink_band_drifts_toward_cur_cell(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 2, 5)
    # prebuild() runs off requestIdleCallback; nudge it so the test does not
    # depend on the idle queue draining.
    ios_page.evaluate("window.Overview.prebuild()")
    ios_page.wait_for_function("!!window.Overview.cellCenter()")
    pinch(touch, CX, CY, [50, 40, 30], hold=True)  # 0.6 — mid drift
    # pinch onStart syncs position (the prebuilt grid is display:none until
    # then), so the drift target is freshly measured here, not at prebuild.
    dest = ios_page.evaluate("window.Overview.cellCenter()")
    assert dest["x"] > 0 and dest["y"] > 0
    st = zoom_state(ios_page)
    # zoom.js writes tx/ty in CONTAINER-LOCAL px (#zoom-layer is inset:0 inside
    # #img-container, transform-origin 0 0), while cellCenter() is viewport.
    # Convert the layer centre to viewport with the container's own rect.
    org = ios_page.evaluate(
        "(() => { const r = document.getElementById('img-container').getBoundingClientRect();"
        " return {left: r.left, top: r.top, w: r.width, h: r.height}; })()"
    )
    W, H = org["w"], org["h"]
    center_x = org["left"] + st["tx"] + st["zoom"] * W / 2
    center_y = org["top"] + st["ty"] + st["zoom"] * H / 2
    rest_x, rest_y = org["left"] + W / 2, org["top"] + H / 2
    # centre has moved from the resting centre toward dest
    assert abs(center_x - rest_x) > 1 or abs(center_y - rest_y) > 1
    d_before = ((rest_x - dest["x"]) ** 2 + (rest_y - dest["y"]) ** 2) ** 0.5
    d_now = ((center_x - dest["x"]) ** 2 + (center_y - dest["y"]) ** 2) ** 0.5
    assert d_now < d_before
    touch_end(touch)


def test_entry_flight_then_reveal_and_close_mid_flight_removes_clone(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 2, 5)
    pinch(touch, CX, CY, [50, 40, 24], hold=True)
    ios_page.wait_for_function("window.Overview.isOpen()")
    # the clone really is created (the flight lasts >= FLY_MS, so rAF polling
    # catches it) and then reclaims itself
    ios_page.wait_for_function("window.Overview._debug.flightActive()", timeout=1500)
    assert ios_page.locator("#ov-fly").count() == 1
    # the overview's own dark background stays up during the flight — only the
    # header bar and the grid body are faded, and both are restored after
    assert ios_page.evaluate("document.querySelector('#overview').style.opacity") == ""
    assert ios_page.evaluate("document.querySelector('#overview .ov-body').style.opacity") == "0"
    ios_page.wait_for_function("!document.querySelector('#ov-fly')", timeout=1500)
    assert ios_page.evaluate("document.querySelector('#overview .ov-body').style.opacity") == ""
    assert ios_page.evaluate("document.querySelector('#overview .ov-header').style.opacity") == ""
    assert ios_page.evaluate("document.querySelector('#overview').style.pointerEvents") == ""
    touch_end(touch)
    # again, but close during the flight
    ios_page.keyboard.press("g")
    ios_page.wait_for_function("!window.Overview.isOpen()")
    pinch(touch, CX, CY, [50, 40, 24], hold=True)
    ios_page.wait_for_function("window.Overview.isOpen()")
    ios_page.wait_for_function("window.Overview._debug.flightActive()", timeout=1500)
    ios_page.evaluate("window.Overview.close()")
    assert ios_page.locator("#ov-fly").count() == 0
    assert ios_page.evaluate("window.Overview._debug.flightActive()") is False
    touch_end(touch)


def test_entry_gesture_widening_does_not_exit(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 2, 5)
    pinch(touch, CX, CY, [50, 40, 24], hold=True)
    ios_page.wait_for_function("window.Overview.isOpen()")
    # ratio 1.8 within the SAME gesture: its touch target is #img-container, so
    # the #overview pinch-out recognizer never sees it.
    touch_move(touch, two(CX, CY, 60))
    touch_move(touch, two(CX, CY, 90))
    assert ios_page.evaluate("window.Overview.isOpen()") is True
    touch_end(touch)
    assert ios_page.evaluate("window.Overview.isOpen()") is True


def test_new_pinch_out_in_overview_exits_to_cell_under_midpoint(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 0, 5)
    _open_overview(ios_page)
    cell = ios_page.locator("#overview .ov-cell").nth(2)
    cell.scroll_into_view_if_needed()
    _settle_scroll(ios_page)
    b = cell.bounding_box()
    gidx = int(cell.get_attribute("data-gidx"))
    mx, my = b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
    pinch(touch, mx, my, [30, 36, 42, 50])  # 1.67 > 1.3
    ios_page.wait_for_function("!window.Overview.isOpen()")
    assert ios_page.evaluate("window.currentIdx") == gidx


def test_tap_exit_shows_crossfade_that_is_removed_on_load_or_error(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 0, 5)
    _open_overview(ios_page)
    cell = ios_page.locator("#overview .ov-cell").nth(12)
    cell.scroll_into_view_if_needed()
    _settle_scroll(ios_page)
    b = cell.bounding_box()
    tap(touch, b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)
    ios_page.wait_for_function("!window.Overview.isOpen()")
    # crossfade clone exists briefly, then disappears (load fires quickly on the fixture)
    ios_page.wait_for_function("!!document.querySelector('#ov-xfade')", timeout=2000)
    # inside #img-container, below #toolbar (5) — the chrome stays visible
    assert ios_page.evaluate(
        "document.querySelector('#ov-xfade').parentElement.id"
    ) == "img-container"
    assert int(ios_page.evaluate(
        "getComputedStyle(document.querySelector('#ov-xfade')).zIndex"
    )) < int(ios_page.evaluate("getComputedStyle(document.querySelector('#toolbar')).zIndex"))
    ios_page.wait_for_function("!document.querySelector('#ov-xfade')", timeout=3500)


def test_prebuilt_cur_cell_follows_page_turns(ios_page, server, touch):
    """`.cur` follows turns without a rebuild (markCurrent runs in snapTo's
    deferred window job), while scroll/cellCenter sync lazily via
    syncPosition() — the pinch-start path — instead of every turn (#23)."""
    open_book(ios_page, server, SERIES, 0, 5)
    ios_page.evaluate("window.Overview.prebuild()")
    ios_page.wait_for_function("!!window.Overview.cellCenter()")
    assert ios_page.evaluate("window.Overview.isOpen()") is False
    ios_page.keyboard.press("ArrowLeft")  # RTL: ← is the NEXT page
    ios_page.keyboard.press("ArrowLeft")
    ios_page.wait_for_function("window.currentIdx === 2")
    # the grid is still closed: `.cur` moved without a rebuild ... markCurrent
    # runs in snapTo's deferred window job (two frames after the turn), so
    # wait for the cell rather than reading it on the same tick as currentIdx.
    ios_page.wait_for_function(
        "+document.querySelector('#overview .ov-cell.cur').dataset.gidx === 2"
    )
    assert ios_page.locator("#overview .ov-cell.cur").count() == 1
    # position sync is lazy now: on a display:none prebuilt grid there is no
    # geometry, so cellCenter() is still the all-zeros capture from prebuild
    # until the pinch-start path refreshes it ...
    stale = ios_page.evaluate("window.Overview.cellCenter()")
    assert stale["x"] == 0 and stale["y"] == 0
    # ... and a sync (exactly what Zoom onStart calls) measures for real,
    # which unhides-then-rehides, so compare against live values here rather
    # than a post-sync rect read (display:none again by then).
    ios_page.evaluate("window.Overview.syncPosition()")
    center = ios_page.evaluate("window.Overview.cellCenter()")
    assert center["x"] > 0 and center["y"] > 0
    # opening still lands on the right cell
    pinch(touch, CX, CY, [50, 40, 24], hold=True)
    ios_page.wait_for_function("window.Overview.isOpen()")
    touch_end(touch)
    assert ios_page.evaluate(
        "+document.querySelector('#overview .ov-cell.cur').dataset.gidx"
    ) == ios_page.evaluate("window.currentIdx")


# ── final-review additions ───────────────────────────────────────────────────


def test_close_rearms_the_prebuild_so_the_next_pinch_has_a_target(ios_page, server, touch):
    """close() empties the grid. Without a re-armed prebuild, the next pinch-in
    would have to BUILD inside a touchmove and the FLIP would have no `.cur`
    cell to fly to (Zoom.driftTarget() would be null for the whole shrink)."""
    open_book(ios_page, server, SERIES, 2, 5)
    _open_overview(ios_page)
    ios_page.keyboard.press("Escape")
    ios_page.wait_for_function("!window.Overview.isOpen()")
    # requestIdleCallback timeout is 1000ms
    ios_page.wait_for_function("window.Overview.cellCenter() !== null", timeout=4000)
    assert ios_page.locator("#overview .ov-cell").count() > 0
    assert ios_page.evaluate("window.Overview.isOpen()") is False
    pinch(touch, CX, CY, [50, 40, 24], hold=True)
    ios_page.wait_for_function("window.Overview.isOpen()")
    ios_page.wait_for_function("window.Overview._debug.flightActive()", timeout=1500)
    touch_end(touch)


def test_scrolling_a_huge_grid_slides_the_img_window(ios_page, server):
    open_book(ios_page, server, "huge/book/part2", 100, 351)
    _open_overview(ios_page)
    win0 = ios_page.evaluate("window.Overview._debug.imgWindow()")
    ios_page.evaluate(
        "(() => { const r = document.querySelector('#overview');"
        " r.scrollTop = r.scrollTop + 4000; r.dispatchEvent(new Event('scroll')); })()"
    )
    ios_page.wait_for_function(
        "(w) => { const n = window.Overview._debug.imgWindow();"
        " return !!n && (n.lo !== w.lo || n.hi !== w.hi); }",
        arg=win0,
        timeout=4000,
    )
    n = ios_page.locator("#overview .ov-cell img").count()
    assert 380 <= n <= 420, n
    assert ios_page.locator("#overview .ov-cell").count() == 701   # cells never change


def test_crossfade_still_covers_the_strip_under_reduced_motion(ios_page, server, touch):
    """Skipping the cover entirely left a black frame where the page should be.
    Reduced motion drops the 200ms fade, not the cover."""
    ios_page.emulate_media(reduced_motion="reduce")
    open_book(ios_page, server, SERIES, 0, 5)
    _open_overview(ios_page)
    cell = ios_page.locator("#overview .ov-cell").nth(12)
    cell.scroll_into_view_if_needed()
    _settle_scroll(ios_page)
    # The cover can be removed the instant the real page decodes, so watch for
    # its insertion instead of polling for it to still be there.
    ios_page.evaluate(
        "(() => { window.__xfade = null;"
        " new MutationObserver(ms => { for (const m of ms) for (const n of m.addedNodes)"
        "   if (n.id === 'ov-xfade') window.__xfade = {transition: n.style.transition, src: n.src};"
        " }).observe(document.getElementById('img-container'), {childList: true}); })()"
    )
    b = cell.bounding_box()
    tap(touch, b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)
    ios_page.wait_for_function("!window.Overview.isOpen()")
    ios_page.wait_for_function("!!window.__xfade", timeout=3000)
    assert ios_page.evaluate("window.__xfade.transition") == ""
    assert "w=800" in ios_page.evaluate("window.__xfade.src")
    ios_page.wait_for_function("!document.querySelector('#ov-xfade')", timeout=3500)


def test_exit_with_a_stale_app_js_closes_instead_of_throwing(ios_page, server):
    """StaleWhileRevalidate can serve this module beside an app.js old enough to
    have no window.viewerSnapTo. The grid must still let the reader out."""
    open_book(ios_page, server, SERIES, 0, 5)
    _open_overview(ios_page)
    errors = []
    ios_page.on("pageerror", lambda e: errors.append(str(e)))
    ios_page.evaluate(
        "(() => { window.__snap = window.viewerSnapTo; window.__supp = window.suppressClickFor;"
        " window.viewerSnapTo = undefined; window.suppressClickFor = undefined; })()"
    )
    try:
        cell = ios_page.locator("#overview .ov-cell").nth(2)
        cell.scroll_into_view_if_needed()
        _settle_scroll(ios_page)
        cell.click()
        ios_page.wait_for_function("!window.Overview.isOpen()")
        assert ios_page.evaluate("document.body.classList.contains('overview-open')") is False
        assert errors == [], errors
    finally:
        ios_page.evaluate(
            "(() => { window.viewerSnapTo = window.__snap;"
            " window.suppressClickFor = window.__supp; })()"
        )


def test_overview_button_opens_and_is_labelled_without_implying_a_toggle(ios_page, server):
    open_book(ios_page, server, SERIES, 0, 5)
    assert ios_page.get_attribute("#overview-btn", "title") == "ページ一覧 (G)"
    ios_page.locator("#overview-btn").click()
    ios_page.wait_for_function("window.Overview.isOpen()")

def test_shrink_drift_aims_at_post_turn_cell_via_pinch_start_sync(ios_page, server, touch):
    """#23: per-turn markCurrent no longer scrolls/measures, so right after
    turns the drift target is stale — but the pinch-start syncPosition()
    refreshes it before any drift read, and the shrink aims at the NEW cell."""
    open_book(ios_page, server, SERIES, 0, 5)
    ios_page.evaluate("window.Overview.prebuild()")
    ios_page.wait_for_function("!!window.Overview.cellCenter()")
    ios_page.keyboard.press("ArrowLeft")  # RTL: ← is the NEXT page
    ios_page.keyboard.press("ArrowLeft")
    ios_page.wait_for_function("window.currentIdx === 2")
    ios_page.wait_for_function(
        "+document.querySelector('#overview .ov-cell.cur').dataset.gidx === 2"
    )
    pinch(touch, CX, CY, [50, 40, 30], hold=True)  # 0.6 — mid drift
    # pinch onStart synced position on the display:none grid (unhide, measure,
    # re-hide): drift target is the NEW cell with real geometry now, even
    # though a rect read this late would see zeros again.
    dest = ios_page.evaluate("window.Overview.cellCenter()")
    assert dest["x"] > 0 and dest["y"] > 0
    st = zoom_state(ios_page)
    org = ios_page.evaluate(
        "(() => { const r = document.getElementById('img-container').getBoundingClientRect();"
        " return {left: r.left, top: r.top, w: r.width, h: r.height}; })()"
    )
    W, H = org["w"], org["h"]
    center_x = org["left"] + st["tx"] + st["zoom"] * W / 2
    center_y = org["top"] + st["ty"] + st["zoom"] * H / 2
    rest_x, rest_y = org["left"] + W / 2, org["top"] + H / 2
    d_before = ((rest_x - dest["x"]) ** 2 + (rest_y - dest["y"]) ** 2) ** 0.5
    d_now = ((center_x - dest["x"]) ** 2 + (center_y - dest["y"]) ** 2) ** 0.5
    assert d_now < d_before
    touch_end(touch)
