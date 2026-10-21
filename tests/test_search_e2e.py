"""E2E tests for the search / filter / view-mode UI.
Covers: search input, filter bottom sheet, view-mode cycling (tile↔list↔card),
pull-to-refresh, and graceful rendering without metadata.

Run:
    uv run --with pytest --with playwright pytest tests/test_search_e2e.py -v
"""

import re
from playwright.sync_api import expect

# ── search input ────────────────────────────────────────────────────────────────


def test_search_input_exists(page):
    """Search input is present and visible in the sidebar."""
    expect(page.locator("#search-input")).to_be_visible()


def test_search_filters_local_folders(page):
    """Search calls the /api/search endpoint.
    With no metadata.toml files in the fixture the API returns 0 results,
    so the grid shows an empty-state message.
    """
    # Type a query that won't match any metadata field
    page.locator("#search-input").fill("nonexistent")
    # wait for 300 ms debounce + fetch
    page.wait_for_timeout(500)
    # Search mode should show the empty-state fallback
    expect(page.locator("#tile-grid")).to_contain_text("一致する結果がありません")

    # Clear the search input
    page.locator("#search-input").fill("")
    page.wait_for_timeout(500)
    # Folders restored
    expect(page.locator(".tile").first).to_be_visible()
    # Parent-level spec: root count includes the ONE images-tile ("mixed").
    expect(page.locator("#folder-count")).to_have_text("18 件")


# ── filter bottom sheet ─────────────────────────────────────────────────────────


def test_filter_button_exists(page):
    """Filter button is present in the sidebar header."""
    expect(page.locator("#filter-btn")).to_be_visible()


def test_bottom_sheet_opens_and_closes(page):
    """Filter bottom sheet slides up on filter-btn click and can be dismissed."""
    sheet = page.locator("#bottom-sheet")
    overlay = page.locator("#bottom-sheet-overlay")
    close_btn = page.locator("#bottom-sheet-close")

    # ---- open ----
    filter_btn = page.locator("#filter-btn")
    # The button is disabled while the shallow first-paint window is open
    # (updateShallowBadge); a .click() during that window is a no-op. Wait
    # for the full tree to re-enable it instead of racing the window.
    expect(filter_btn).to_be_enabled()
    page.evaluate("document.getElementById('filter-btn').click()")
    page.wait_for_timeout(100)  # requestAnimationFrame + layout
    expect(sheet).to_have_class(re.compile(r"\bopen\b"))
    expect(sheet).not_to_have_class(re.compile(r"\bhidden\b"))
    expect(overlay).to_have_class(re.compile(r"\bvisible\b"))

    # ---- close via X button ----
    page.evaluate("document.getElementById('bottom-sheet-close').click()")
    page.wait_for_timeout(400)  # 350 ms transition + buffer
    expect(sheet).to_have_class(re.compile(r"\bhidden\b"))
    expect(sheet).not_to_have_class(re.compile(r"\bopen\b"))
    expect(overlay).not_to_have_class(re.compile(r"\bvisible\b"))

    # ---- open again, close via overlay tap ----
    expect(filter_btn).to_be_enabled()
    page.evaluate("document.getElementById('filter-btn').click()")
    page.wait_for_timeout(100)
    expect(sheet).to_have_class(re.compile(r"\bopen\b"))
    page.evaluate("document.getElementById('bottom-sheet-overlay').click()")
    page.wait_for_timeout(400)
    expect(sheet).to_have_class(re.compile(r"\bhidden\b"))
    expect(sheet).not_to_have_class(re.compile(r"\bopen\b"))


# ── view-mode cycling: tile → list → card → tile ───────────────────────────────


def _current_view_modes(tile_grid):
    """Return set of mode classes currently on #tile-grid."""
    cls = tile_grid.get_attribute("class") or ""
    modes = set()
    if "list-mode" in cls:
        modes.add("list")
    if "card-mode" in cls:
        modes.add("card")
    return modes


def test_view_mode_cycles_through_three_modes(page):
    """Footer button cycles tile → list → card → tile."""
    btn = page.locator("#footer-refresh-btn")
    grid = page.locator("#tile-grid")

    # --- initial: tile ---
    assert _current_view_modes(grid) == set(), f"Expected tile mode, got {_current_view_modes(grid)}"
    expect(page.locator(".tile").first).to_be_visible()

    # --- 1st click → list ---
    btn.click()
    page.wait_for_timeout(100)
    expect(grid).to_have_class(re.compile(r"\blist-mode\b"))
    expect(page.locator(".item.folder").first).to_be_visible()

    # --- 2nd click → card ---
    btn.click()
    page.wait_for_timeout(100)
    expect(grid).to_have_class(re.compile(r"\bcard-mode\b"))
    expect(grid).not_to_have_class(re.compile(r"\blist-mode\b"))
    expect(page.locator(".card").first).to_be_visible()

    # --- 3rd click → back to tile ---
    btn.click()
    page.wait_for_timeout(100)
    assert _current_view_modes(grid) == set(), f"Expected tile mode, got {_current_view_modes(grid)}"
    expect(page.locator(".tile").first).to_be_visible()
    expect(page.locator("#folder-count")).to_have_text("18 件")


# ── pull-to-refresh delegated to Safari native (custom top reload removed) ──


def _ptr_fire(page, events):
    """Dispatch touch events via JS helper (sidebarScroll target).

    Kept for bottom/horizontal probes. Top PTR now native.
    """
    return page.evaluate(
        """(evs) => {
            const scroll = document.getElementById('sidebar-scroll');
            const grid = document.getElementById('tile-grid');
            const target = scroll;
            const fire = (type, y, hasTouch) => {
                let ev;
                try {
                    const t = hasTouch ? [new Touch({identifier:0, target:grid, clientX:10, clientY:y})] : [];
                    const ct = hasTouch ? [] : [new Touch({identifier:0, target:grid, clientX:10, clientY:y})];
                    ev = new TouchEvent(type, {touches: t, changedTouches: ct, bubbles:true, cancelable:true});
                } catch {
                    ev = new Event(type, {bubbles:true, cancelable:true});
                    ev.touches = hasTouch ? [{clientX:10, clientY:y, identifier:0, target:grid}] : [];
                    ev.changedTouches = hasTouch ? [] : [{clientX:10, clientY:y, identifier:0, target:grid}];
                }
                target.dispatchEvent(ev);
            };
            for (const [type, y, hasTouch] of evs) fire(type, y, hasTouch);
            return window.__ptrDebug ? {hState: window.__ptrDebug.hState, bVisual: window.__ptrDebug.bVisual} : null;
        }""",
        events,
    )


def test_ptr_native_overscroll_enabled(page):
    """Native PTR: html overscroll auto, sidebar auto, no custom indicator."""
    styles = page.evaluate("""() => {
        const html = getComputedStyle(document.documentElement).overscrollBehavior;
        const scroll = getComputedStyle(document.getElementById('sidebar-scroll')).overscrollBehavior;
        const indEl = document.getElementById('pull-indicator');
        return {html, scroll, hasIndicator: indEl !== null};
    }""")
    assert styles["html"] in ("auto", ""), f"html overscroll {styles['html']} expected auto"
    assert styles["scroll"] in ("auto", ""), f"sidebar overscroll {styles['scroll']} expected auto"
    assert styles["hasIndicator"] is False, "pull-indicator should not exist — delegated to Safari native"


def test_ptr_top_drag_does_not_trigger_custom_reload(page):
    """Top drag must NOT trigger custom loadFolders; native Safari handles reload."""
    page.evaluate("document.getElementById('sidebar-scroll').scrollTop = 0")
    page.wait_for_timeout(100)
    fetch_count = {"n": 0}
    def _handler(route):
        if "/api/folders" in route.request.url:
            fetch_count["n"] += 1
        route.continue_()
    page.route("**/api/folders*", _handler)
    try:
        _ptr_fire(page, [
            ("touchstart", 20, True),
            ("touchmove", 100, True),
            ("touchend", 100, False),
        ])
        page.wait_for_timeout(500)
        assert fetch_count["n"] == 0, f"top pull should not fetch, got {fetch_count['n']}"
        has_indicator = page.evaluate("document.getElementById('pull-indicator') !== null")
        assert not has_indicator
        # bottom/h state still idle after top drag
        b = page.evaluate("window.__ptrDebug ? window.__ptrDebug.bVisual : 0")
        assert b == 0
    finally:
        page.unroute("**/api/folders*")


def test_ptr_touchcancel_resets(page):
    """touchcancel with empty changedTouches should reset without crash (bottom/h idle)."""
    page.evaluate("document.getElementById('sidebar-scroll').scrollTop = 0")
    page.wait_for_timeout(100)
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    try:
        page.evaluate("""() => {
            const scroll = document.getElementById('sidebar-scroll');
            const grid = document.getElementById('tile-grid');
            const target = scroll;
            const fire = (type, y, hasTouch) => {
                let ev;
                try {
                    const t = hasTouch ? [new Touch({identifier:0, target:grid, clientX:10, clientY:y})] : [];
                    const ct = hasTouch ? [] : [new Touch({identifier:0, target:grid, clientX:10, clientY:y})];
                    ev = new TouchEvent(type, {touches: t, changedTouches: ct, bubbles:true, cancelable:true});
                } catch {
                    ev = new Event(type, {bubbles:true, cancelable:true});
                    ev.touches = hasTouch ? [{clientX:10, clientY:y, identifier:0, target:grid}] : [];
                    ev.changedTouches = hasTouch ? [] : [{clientX:10, clientY:y, identifier:0, target:grid}];
                }
                target.dispatchEvent(ev);
            };
            fire('touchstart', 20, true);
            fire('touchmove', 60, true);
            const ev = new Event('touchcancel', {bubbles:true, cancelable:true});
            ev.touches = [];
            ev.changedTouches = [];
            target.dispatchEvent(ev);
            window._cancelBVisual = window.__ptrDebug ? window.__ptrDebug.bVisual : 0;
        }""")
        page.wait_for_timeout(300)
        assert not errors, f"touchcancel threw {errors}"
        bval = page.evaluate("window._cancelBVisual")
        assert bval == 0, f"after touchcancel bVisual expected 0 got {bval}"
        cur = page.evaluate("window.__ptrDebug ? window.__ptrDebug.bVisual : 0")
        assert cur == 0
        has_indicator = page.evaluate("document.getElementById('pull-indicator') !== null")
        assert not has_indicator
        expect(page.locator("#folder-count")).to_have_text("18 件")
    finally:
        try:
            page.remove_listener("pageerror", lambda exc: None)
        except Exception:
            pass


def test_pull_bottom_resistance_no_refresh(page):
    """At bottom, dragging up 80 shows ~15px resistance and triggers no fetch."""
    # Scroll to bottom
    page.evaluate("""() => {
        const s = document.getElementById('sidebar-scroll');
        s.scrollTop = s.scrollHeight;
    }""")
    page.wait_for_timeout(150)
    at_bottom = page.evaluate("document.getElementById('sidebar-scroll').scrollTop + document.getElementById('sidebar-scroll').clientHeight >= document.getElementById('sidebar-scroll').scrollHeight - 1")
    assert at_bottom, "not at bottom"
    fetch_count = {"n": 0}
    def _handler(route):
        if "/api/folders" in route.request.url:
            fetch_count["n"] += 1
        route.continue_()
    page.route("**/api/folders*", _handler)
    try:
        page.evaluate("""() => {
            const scroll = document.getElementById('sidebar-scroll');
            const grid = document.getElementById('tile-grid');
            const target = scroll;
            const fire = (type, y, hasTouch) => {
                let ev;
                try {
                    const t = hasTouch ? [new Touch({identifier:0, target:grid, clientX:10, clientY:y})] : [];
                    const ct = hasTouch ? [] : [new Touch({identifier:0, target:grid, clientX:10, clientY:y})];
                    ev = new TouchEvent(type, {touches: t, changedTouches: ct, bubbles:true, cancelable:true});
                } catch {
                    ev = new Event(type, {bubbles:true, cancelable:true});
                    ev.touches = hasTouch ? [{clientX:10, clientY:y, identifier:0, target:grid}] : [];
                    ev.changedTouches = hasTouch ? [] : [{clientX:10, clientY:y, identifier:0, target:grid}];
                }
                target.dispatchEvent(ev);
            };
            // vertical lock needed: first small vertical move to lock v, then large up drag
            fire('touchstart', 100, true);
            fire('touchmove', 90, true); // dy -10, locks v (still at bottom)
            fire('touchmove', 80, true);
            fire('touchmove', 60, true);
            fire('touchmove', 40, true);
            fire('touchmove', 20, true); // dy -80 total
        }""")
        page.wait_for_timeout(80)
        b_visual = page.evaluate("window.__ptrDebug ? window.__ptrDebug.bVisual : 0")
        transform = page.evaluate("document.getElementById('tile-grid').style.transform")
        # 80px drag => 18*(1-exp(-80/40)) ~15.56
        assert b_visual is not None
        assert 12 < b_visual < 18, f"bVisual {b_visual} not in (12,18)"
        assert "-15" in transform or "-16" in transform or "-14" in transform or "-13" in transform or "15" in transform, f"transform {transform} not ~-15px bVisual {b_visual}"
        # Now release
        page.evaluate("""() => {
            const scroll = document.getElementById('sidebar-scroll');
            const grid = document.getElementById('tile-grid');
            const target = scroll;
            const ev = new Event('touchend', {bubbles:true, cancelable:true});
            ev.touches = [];
            ev.changedTouches = [{clientX:10, clientY:20, identifier:0, target:grid}];
            try {
                const te = new TouchEvent('touchend', {touches: [], changedTouches: [new Touch({identifier:0, target:grid, clientX:10, clientY:20})], bubbles:true, cancelable:true});
                target.dispatchEvent(te);
            } catch {
                target.dispatchEvent(ev);
            }
        }""")
        page.wait_for_timeout(350)  # snap 180 + buffer
        assert fetch_count["n"] == 0, f"bottom drag triggered fetch {fetch_count['n']}"
        after_transform = page.evaluate("document.getElementById('tile-grid').style.transform")
        assert after_transform == "" or after_transform == "none", f"after snap transform not cleared: {after_transform}"
        b_after = page.evaluate("window.__ptrDebug ? window.__ptrDebug.bVisual : 0")
        assert b_after == 0
    finally:
        page.unroute("**/api/folders*")
        # reset scroll to top for next tests
        page.evaluate("document.getElementById('sidebar-scroll').scrollTop = 0")


# ── header ──────────────────────────────────────────────────────────────────────


def test_header_shows_correct_title(page):
    """Sidebar header shows the root-level title."""
    header = page.locator("#browse-header")
    expect(header).to_be_visible()
    expect(header).to_have_text("📁 フォルダ")


# ── card mode without metadata ──────────────────────────────────────────────────


def test_card_mode_renders_folders_without_metadata(page):
    """Card mode gracefully shows folders even when no metadata.toml exists."""
    btn = page.locator("#footer-refresh-btn")

    # cycle to list → card
    btn.click()  # list
    btn.click()  # card
    page.wait_for_timeout(100)

    # Cards are rendered
    expect(page.locator(".card").first).to_be_visible()
    # Each card shows the folder name
    first_card = page.locator(".card").first
    expect(first_card.locator(".card-title")).to_be_visible()
    # Count matches the number of root folders plus the ONE images-tile ("mixed").
    expected_count = 18
    expect(page.locator(".card")).to_have_count(expected_count)



