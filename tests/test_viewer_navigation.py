"""viewer/navigation.js — the user-movement request port (#77 / S02).

Separates "the user asked to move" (a typed request intent) from "the
display applied a move" (committed state). A handler spy installed via
``ViewerNavigation.use()`` must see exactly one intent per user operation
with the S01-conformant payload, while the zero-intent paths (rotation,
raw snapTo, bounce, initial load, zoom slide recovery,
``applyCommittedState``) must emit none. With the default handler the
existing solo behaviour must be preserved byte-for-byte.

Intent contract (local port — the comm layer maps these to wire frames
and must never receive DOM nodes or globalIdx):

    turn      {type, delta:+1|-1, crossChapter:bool, fromChapter:str|null, via}
    goto      {type, page:PageRef{folder,name}, via, animate?:bool}
    setOffset {type, offset:0|1, via}
    openWork  {type, folder:str, via}
    browse    {type, via}
"""
import re

import pytest
from playwright.sync_api import expect

from viewer_touch import (
    open_book,
    touch_session,
    touch_start,
    touch_move,
    touch_end,
    tap,
    drag,
    pinch,
)

FOLDER = "long"                     # 30 pages, single chapter
SERIES = "zoom_series/series/ep-1"  # 5 pages, chapter 0 of 3
CX, CY = 195, 420


def _spy(page):
    """Install a request handler that records every intent (replacing the
    default handler, so intents it captures do NOT navigate)."""
    page.evaluate(
        """() => {
          window.__navIntents = [];
          window.__navUnuse = window.ViewerNavigation.use((intent) => {
            window.__navIntents.push(JSON.parse(JSON.stringify(intent)));
          });
        }"""
    )


def _intents(page):
    return page.evaluate("window.__navIntents")


def _unuse(page):
    page.evaluate("() => { if (window.__navUnuse) window.__navUnuse(); window.__navUnuse = null; }")


# ── module surface ────────────────────────────────────────────────────────


def test_navigation_module_surface(page, server):
    api = page.evaluate(
        """() => ({
          nav: typeof window.ViewerNavigation,
          request: typeof window.ViewerNavigation.request,
          use: typeof window.ViewerNavigation.use,
          applyCommittedState: typeof window.ViewerNavigation.applyCommittedState,
          generation: typeof window.ViewerNavigation.generation,
        })"""
    )
    assert api == {
        "nav": "object",
        "request": "function",
        "use": "function",
        "applyCommittedState": "function",
        "generation": "function",
    }
    # use() must return the unregister function (issue completion criterion)
    assert page.evaluate("typeof window.ViewerNavigation.use(() => {})") == "function"
    page.evaluate("() => { const u = window.ViewerNavigation.use(() => {}); u(); }")


def test_request_rejects_malformed_intents(page, server):
    codes = page.evaluate(
        """() => {
          const bad = [null, 42, "turn", {}, {type: 'nope'},
            {type: 'turn'}, {type: 'turn', delta: 2, crossChapter: false},
            {type: 'turn', delta: 1, crossChapter: 'x'},
            {type: 'goto'}, {type: 'goto', page: {folder: 1}},
            {type: 'setOffset'}, {type: 'setOffset', offset: 2},
            {type: 'openWork'}, {type: 'openWork', folder: 3}];
          return bad.map(b => {
            try { window.ViewerNavigation.request(b); return null; }
            catch (e) { return e.code || e.message; }
          });
        }"""
    )
    assert all(c and str(c).startswith("ERR_") for c in codes), codes


# ── exactly one intent per operation ─────────────────────────────────────


def test_tap_zones_emit_one_turn_intent(page, server):
    open_book(page, server, FOLDER, 1, 30)
    _spy(page)
    page.locator("#zone-prev").click()   # left zone = next (tapForward default 'left')
    page.locator("#zone-next").click()   # right zone = prev
    assert _intents(page) == [
        {"type": "turn", "delta": 1, "crossChapter": True, "fromChapter": FOLDER, "via": "tap"},
        {"type": "turn", "delta": -1, "crossChapter": True, "fromChapter": FOLDER, "via": "tap"},
    ]
    # The spy replaced the handler: intents were captured, not applied.
    assert page.evaluate("window.currentIdx") == 1


def test_from_chapter_is_current_chapter_folder(page, server):
    open_book(page, server, SERIES, 1, 5)
    _spy(page)
    page.locator("#zone-prev").click()
    assert _intents(page)[0]["fromChapter"] == "zoom_series/series/ep-1"


def test_touch_tap_emits_exactly_one_turn(ios_page, server):
    """A real touch tap goes through the touchend path; the synthesized
    click must not emit a second intent."""
    page = ios_page
    open_book(page, server, FOLDER, 1, 30)
    _spy(page)
    s = touch_session(page)
    try:
        tap(s, 60, 500)   # left zone = next
        page.wait_for_timeout(150)
        assert _intents(page) == [
            {"type": "turn", "delta": 1, "crossChapter": True, "fromChapter": FOLDER, "via": "tap"}
        ]
        tap(s, 330, 500)  # right zone = prev
        page.wait_for_timeout(150)
        assert len(_intents(page)) == 2
        assert _intents(page)[1]["delta"] == -1
    finally:
        s.detach()


def test_drift_tap_in_zone_emits_one_turn(ios_page, server):
    """An 18px drift that lands in a zone resolves there — one tap intent."""
    page = ios_page
    open_book(page, server, FOLDER, 1, 30)
    _spy(page)
    s = touch_session(page)
    try:
        touch_start(s, [{"x": 300, "y": 500, "id": 1}])
        touch_move(s, [{"x": 318, "y": 500, "id": 1}])
        touch_end(s, [{"x": 318, "y": 500, "id": 1}])
        assert page.evaluate("window.__lastTouchEndVia") == "drift-tap"
        assert _intents(page) == [
            {"type": "turn", "delta": -1, "crossChapter": True, "fromChapter": FOLDER, "via": "tap"}
        ]
    finally:
        s.detach()


def test_center_tap_emits_zero_intents(page, server):
    """Center zone toggles the toolbar — no navigation request."""
    open_book(page, server, FOLDER, 1, 30)
    _spy(page)
    page.locator("#zone-toggle").click()
    assert _intents(page) == []


def test_arrow_keys_emit_turn(page, server):
    open_book(page, server, FOLDER, 1, 30)
    _spy(page)
    page.keyboard.press("ArrowLeft")    # next (RTL)
    page.keyboard.press("ArrowRight")   # prev
    assert _intents(page) == [
        {"type": "turn", "delta": 1, "crossChapter": False, "fromChapter": FOLDER, "via": "key"},
        {"type": "turn", "delta": -1, "crossChapter": False, "fromChapter": FOLDER, "via": "key"},
    ]


def test_home_end_emit_goto(page, server):
    open_book(page, server, FOLDER, 5, 30)
    _spy(page)
    page.keyboard.press("Home")
    page.keyboard.press("End")
    assert _intents(page) == [
        {"type": "goto", "page": {"folder": FOLDER, "name": "p001.png"}, "via": "key", "animate": False},
        {"type": "goto", "page": {"folder": FOLDER, "name": "p030.png"}, "via": "key"},
    ]


def test_slider_emits_goto(page, server):
    open_book(page, server, FOLDER, 1, 30)
    _spy(page)
    page.locator("#page-slider").evaluate(
        "s => { s.value = '5'; s.dispatchEvent(new Event('input')); }"
    )
    assert _intents(page) == [
        {"type": "goto", "page": {"folder": FOLDER, "name": "p006.png"}, "via": "slider"}
    ]


def test_overview_select_emits_goto(page, server):
    open_book(page, server, FOLDER, 1, 30)
    page.evaluate("window.Overview.open()")
    _spy(page)
    page.locator(".ov-cell[data-gidx='3']").click()
    assert _intents(page) == [
        {"type": "goto", "page": {"folder": FOLDER, "name": "p004.png"}, "via": "overview"}
    ]


def test_swipe_commit_emits_turn(ios_page, server):
    """A decided swipe that commits emits one chapter-local turn."""
    page = ios_page
    open_book(page, server, FOLDER, 1, 30)
    _spy(page)
    s = touch_session(page)
    try:
        drag(s, 100, 500, 240, 500)   # 140px right, past the swipe threshold
        assert _intents(page) == [
            {"type": "turn", "delta": 1, "crossChapter": False, "fromChapter": FOLDER, "via": "swipe"}
        ]
    finally:
        s.detach()


def test_zoom_edge_turn_emits_turn(ios_page, server):
    """The zoomed pan that arms at the clamp edge turns the page — one
    cross-chapter turn intent, same as the tap zones."""
    page = ios_page
    open_book(page, server, FOLDER, 1, 30)
    s = touch_session(page)
    try:
        pinch(s, CX, CY, [40, 60, 80])            # 2.0x
        page.wait_for_function("window.Zoom.state().zoom > 1.9")
        drag(s, 60, 500, 380, 500)                # clamp the left edge (setup)
        _spy(page)
        drag(s, 100, 500, 220, 500)               # starts clamped → edge turn
        assert _intents(page) == [
            {"type": "turn", "delta": 1, "crossChapter": True, "fromChapter": FOLDER, "via": "edge"}
        ]
    finally:
        s.detach()


def test_spread_shift_emits_setoffset(landscape_page, server):
    page = landscape_page
    open_book(page, server, FOLDER, 0, 30)
    _spy(page)
    page.locator("#spread-shift").click()
    assert _intents(page) == [
        {"type": "setOffset", "offset": 1, "via": "spread-shift"}
    ]


def test_tile_open_emits_openwork(page, server):
    _spy(page)
    page.locator('.tile[data-path="simple_manga"]').click()
    assert _intents(page) == [
        {"type": "openWork", "folder": "simple_manga", "via": "tile"}
    ]


def test_browse_drilldown_emits_zero(page, server):
    """Drilling into a folder is browse-level navigation, not a reader
    movement request — no intent."""
    _spy(page)
    page.locator('.tile[data-path="series"]').click()
    page.wait_for_url(re.compile(r"#/browse/series"))
    assert _intents(page) == []


def test_menu_back_to_browse_emits_browse(page, server):
    open_book(page, server, FOLDER, 0, 30)
    _spy(page)
    page.locator("#menu-btn").click()
    assert _intents(page) == [{"type": "browse", "via": "sidebar"}]


# ── zero intents on non-request paths ────────────────────────────────────


def test_zero_intent_paths(page, server):
    open_book(page, server, FOLDER, 1, 30)
    _spy(page)
    # raw snapTo — display application, not a user request
    page.evaluate("window.viewerSnapTo(2, false)")
    # zoom slide-recovery goes through the same raw entry point
    page.evaluate("window.viewerSnapTo(window.currentIdx, false)")
    # remote committed application — receive-only, never echoes a request
    page.evaluate(
        """() => window.ViewerNavigation.applyCommittedState(
          {v:1, sessionId:'s', revision:1, workEpoch:0, manifestVersion:1,
           phase:'reading', spreadId:'c0:s0', offset:0, leaderSide:'right', presence:{}},
          {spreads:[{id:'c0:s0', chapterFolder:'long',
                     right:{folder:'long', name:'p001.png'}, left:null}]},
          {})"""
    )
    assert _intents(page) == []


def test_rotation_emits_zero_intents(page, server):
    open_book(page, server, FOLDER, 1, 30)
    _spy(page)
    page.set_viewport_size({"width": 844, "height": 390})
    page.wait_for_function("document.body.className === 'landscape'")
    assert _intents(page) == []


def test_initial_load_emits_zero_intents(page, server):
    """Deep-link / URL-restore load is display application, not a request."""
    _spy(page)
    page.evaluate("() => { location.hash = '#/view/long?p=2'; }")
    expect(page.locator("#counter")).to_have_text("3 / 30")
    assert _intents(page) == []


def test_bounce_emits_zero_intents(ios_page, server):
    """A pull-back cancel must emit nothing — it is a display bounce."""
    page = ios_page
    open_book(page, server, FOLDER, 1, 30)
    _spy(page)
    s = touch_session(page)
    vthresh = 39  # min(390*0.10, VIEWER_SWIPE_THRESHOLD)
    try:
        touch_start(s, [{"x": 78, "y": 500, "id": 1}])
        touch_move(s, [{"x": 78 + vthresh + 40, "y": 500, "id": 1}])   # commit-ready
        touch_move(s, [{"x": 78 + vthresh - 18, "y": 500, "id": 1}])   # pulled back
        touch_end(s, [{"x": 88, "y": 500, "id": 1}])                   # release near start
        page.wait_for_timeout(150)
        assert page.evaluate("window.__lastTouchEndVia") == "bounce"
        assert _intents(page) == []
    finally:
        s.detach()


# ── default handler preserves solo behaviour ─────────────────────────────


def test_default_handler_solo_turns_page(page, server):
    """No handler installed: the default reproduces existing navigation."""
    open_book(page, server, FOLDER, 0, 30)
    page.keyboard.press("ArrowLeft")   # next
    expect(page.locator("#counter")).to_have_text("2 / 30")
    page.keyboard.press("ArrowRight")  # prev
    expect(page.locator("#counter")).to_have_text("1 / 30")


def test_use_unregister_restores_default(page, server):
    open_book(page, server, FOLDER, 0, 30)
    _spy(page)
    page.locator("#zone-prev").click()
    assert len(_intents(page)) == 1
    assert page.evaluate("window.currentIdx") == 0   # swallowed by the spy
    _unuse(page)
    page.locator("#zone-prev").click()
    expect(page.locator("#counter")).to_have_text("2 / 30")
    assert len(_intents(page)) == 1   # no further intents reached the old spy


def test_use_stacks_with_unregister(page, server):
    page.evaluate(
        """() => {
          window.__a = []; window.__b = [];
          window.__ua = window.ViewerNavigation.use(i => window.__a.push(i));
          window.__ub = window.ViewerNavigation.use(i => window.__b.push(i));
          window.ViewerNavigation.request({type: 'browse'});
          window.__ub();
          window.ViewerNavigation.request({type: 'browse'});
          window.__ua();
        }"""
    )
    assert page.evaluate("window.__b.length") == 1   # top handler saw the first
    assert page.evaluate("window.__a.length") == 1   # restored handler saw the second


# ── applyCommittedState: the receive-only channel ─────────────────────────


def test_apply_committed_state_moves_anchor(page, server):
    open_book(page, server, FOLDER, 0, 30)
    _spy(page)
    page.evaluate(
        """() => window.ViewerNavigation.applyCommittedState(
          {v:1, sessionId:null, revision:3, workEpoch:0, manifestVersion:1,
           phase:'reading', spreadId:'c0:s4', offset:0, leaderSide:'right', presence:{}},
          {spreads:[{id:'c0:s4', chapterFolder:'long',
                     right:{folder:'long', name:'p005.png'},
                     left:{folder:'long', name:'p006.png'}}]},
          {})"""
    )
    # The committed anchor is the spread's first real page (p005 → idx 4).
    assert page.evaluate("window.currentIdx") == 4
    assert _intents(page) == []   # remote apply must not echo as a request


def test_apply_committed_invalidates_stale_swipe_finalizer(page, server):
    """Deterministic generation scenario: finalizer reserved → committed
    state applied → the old-generation finalizer must NOT overwrite the
    committed anchor when it fires."""
    open_book(page, server, FOLDER, 0, 30)
    # 1) A committed swipe reserves a deferred display finalizer (~260ms).
    page.evaluate(
        """() => window.ViewerNavigation.request(
          {type:'turn', delta:1, crossChapter:false, fromChapter:'long', via:'swipe'})"""
    )
    gen0 = page.evaluate("window.ViewerNavigation.generation()")
    # 2) A committed remote state lands before that finalizer fires.
    page.evaluate(
        """() => window.ViewerNavigation.applyCommittedState(
          {v:1, sessionId:'s', revision:7, workEpoch:0, manifestVersion:1,
           phase:'reading', spreadId:'c0:s2', offset:0, leaderSide:'right', presence:{}},
          {spreads:[{id:'c0:s2', chapterFolder:'long',
                     right:{folder:'long', name:'p003.png'}, left:null}]},
          {})"""
    )
    assert page.evaluate("window.currentIdx") == 2   # committed anchor applied
    assert page.evaluate("window.ViewerNavigation.generation()") > gen0
    # 3) The stale finalizer fires — the committed anchor must survive.
    page.wait_for_timeout(500)
    assert page.evaluate("window.currentIdx") == 2
    expect(page.locator("#counter")).to_have_text("3 / 30")
