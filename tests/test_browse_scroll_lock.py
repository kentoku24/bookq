"""Regression tests for the intermittent tile-view scroll lock.

The sidebar scroller (#sidebar-scroll, touch-action:pan-y) arms a non-passive
preventDefault touchmove listener (`tmBlocker`) only while an h-drag or a
bottom overscroll pull actually needs it. On iPhone/iPad Safari the tile view
intermittently became unscrollable "until it recovered". Three reproduced
mechanisms, pinned here:

- a flick that starts slightly horizontal locks ptrLocked='h' and arms the
  blocker; the gesture can never return to vertical scrolling (one-way lock)
- a touchstart arriving while a previous gesture's touchend was lost (target
  detached by a mid-gesture re-render, or an OS-interrupted touch) used to
  early-return on `e.touches.length !== 1` *before* disarming, so the blocker
  stayed armed indefinitely
- releasing one finger of a two-finger touch left ptrStart stale, so the
  surviving finger's next move could lock 'h' off a bogus delta

Run:
    uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_browse_scroll_lock.py -q
"""

import re
import time

from playwright.sync_api import expect


# Shared in-page touch dispatcher. `pts` become e.touches (active touches);
# `changed` become e.changedTouches (defaults to pts). Returns whether the
# dispatched event ended up defaultPrevented — a direct probe for whether the
# armed tmBlocker listener fired on it.
_FIRE_JS = """() => {
    const scroll = document.getElementById('sidebar-scroll');
    const grid = document.getElementById('tile-grid');
    const mkT = (p) => {
        try { return new Touch({identifier: p.id || 0, target: p.t || grid, clientX: p.x, clientY: p.y}); }
        catch { return {identifier: p.id || 0, target: p.t || grid, clientX: p.x, clientY: p.y}; }
    };
    window.__lockTestFire = (type, pts, changed) => {
        const touches = (pts || []).map(mkT);
        const cts = (changed === undefined ? (pts || []) : changed).map(mkT);
        let ev;
        try {
            ev = new TouchEvent(type, {touches, changedTouches: cts, bubbles: true, cancelable: true});
        } catch {
            ev = new Event(type, {bubbles: true, cancelable: true});
            ev.touches = touches;
            ev.changedTouches = cts;
        }
        scroll.dispatchEvent(ev);
        return ev.defaultPrevented;
    };
    window.__lockTestState = () => ({
        armed: window.__ptrDebug ? window.__ptrDebug.tmArmed : null,
        h: window.__ptrDebug ? window.__ptrDebug.hState : null,
        lock: window.__ptrDebug ? window.__ptrDebug.ptrLock : null,
    });
}
"""


def _install_fire(page):
    page.evaluate(_FIRE_JS)


def _fire(page, js):
    return page.evaluate(js)


def _goto_episodes(page):
    page.locator('.tile[data-path="group"]').click()
    page.locator('.tile[data-path="group/episodes"]').click()
    expect(page).to_have_url(re.compile(r"#/browse/group/episodes$"))


def test_vertical_rescue_after_horizontal_lock(page, server):
    """Diagonal flick: early horizontal twitch then a clear vertical turn must
    keep scrolling, not arm the scroll blocker. Uses the root list (scrollable)
    with an edge-zone start so the h-gate is open."""
    _install_fire(page)
    r = _fire(page, """() => {
        const F = window.__lockTestFire;
        const out = {};
        F('touchstart', [{x: 15, y: 400, id: 1}]);
        out.m1 = F('touchmove', [{x: 30, y: 400, id: 1}]);    // dx 15: provisional 'h' lock (below arm)
        out.m2 = F('touchmove', [{x: 33, y: 350, id: 1}]);    // |dy|=50 > |dx|=18*1.5 -> rescue 'v'
        out.m3 = F('touchmove', [{x: 35, y: 290, id: 1}]);
        out.m4 = F('touchmove', [{x: 35, y: 220, id: 1}]);
        out.state = window.__lockTestState();
        F('touchend', [], [{x: 35, y: 220, id: 1}]);
        return out;
    }""")
    st = r["state"]
    assert st["h"] == "idle", f"vertical-turn flick captured by h-drag: {st}"
    assert st["armed"] is False, f"scroll blocker armed on a vertical-intent flick: {st}"
    assert st["lock"] in (None, "v"), f"lock state {st}"
    assert r["m3"] is False and r["m4"] is False, "touchmove was preventDefault-ed mid vertical flick"


def test_multitouch_start_disarms_stuck_blocker(page, server):
    """A stuck blocker from a gesture whose touchend never arrived must be
    dropped by ANY new touchstart — including multi-touch ones that skip the
    single-finger tracking path."""
    _goto_episodes(page)
    _install_fire(page)
    r = _fire(page, """() => {
        const F = window.__lockTestFire;
        F('touchstart', [{x: 200, y: 400, id: 1}]);
        F('touchmove', [{x: 245, y: 400, id: 1}]);   // dx 45 -> draggingH, blocker armed
        const armed = window.__lockTestState().armed;
        // Simulate a second finger landing while the first gesture's end was
        // never delivered: touchstart with two active touches.
        F('touchstart', [{x: 245, y: 400, id: 1}, {x: 300, y: 200, id: 2}]);
        return {armed, after: window.__lockTestState()};
    }""")
    assert r["armed"] is True, "precondition: h-drag did not arm the blocker"
    assert r["after"]["armed"] is False, f"blocker survived a new touchstart: {r['after']}"
    assert r["after"]["h"] == "idle", f"leaked hDragState {r['after']}"


def test_rerender_mid_gesture_releases_blocker(page, server):
    """Rebuilding tile-grid innerHTML mid-gesture detaches the touch target so
    the gesture's touchend is lost; the re-render must sweep the leaked armed
    state and any leftover preview transform."""
    _goto_episodes(page)
    _install_fire(page)
    armed = _fire(page, """() => {
        const F = window.__lockTestFire;
        F('touchstart', [{x: 200, y: 400, id: 1}]);
        F('touchmove', [{x: 245, y: 400, id: 1}]);
        const st = window.__lockTestState();
        window.renderView();   // innerHTML replace — touch target detached, no touchend will arrive
        return st.armed;
    }""")
    assert armed is True, "precondition: h-drag did not arm the blocker"
    page.wait_for_timeout(100)   # MutationObserver callback is a microtask
    st = _fire(page, "window.__lockTestState()")
    assert st["armed"] is False, f"blocker leaked past re-render: {st}"
    assert st["h"] == "idle", f"leaked hDragState after re-render: {st}"
    transform = _fire(page, "document.getElementById('tile-grid').style.transform")
    assert transform in ("", "none"), f"leaked preview transform: {transform}"
    # The h-preview lives on #sidebar now — it must clear too, underlay included.
    sb_transform = _fire(page, "document.getElementById('sidebar').style.transform")
    assert sb_transform in ("", "none"), f"leaked sidebar preview transform: {sb_transform}"
    underlays = _fire(page, "[...document.querySelectorAll('#sidebar')].length")
    assert underlays == 1, f"leaked nav underlay: {underlays} #sidebar nodes"
    leaked = _fire(page, "!!document.getElementById('nav-underlay')")
    assert leaked is False, "leaked #nav-underlay host"


def test_second_finger_release_rebaselines(page, server):
    """Lifting one finger of a two-finger touch must not let the survivor lock
    'h' off a stale start point (which would re-arm the blocker and freeze
    scrolling for the rest of that gesture)."""
    _goto_episodes(page)
    _install_fire(page)
    r = _fire(page, """() => {
        const F = window.__lockTestFire;
        // Leave ptrStartX/Y at a stale position: a completed edge-zone gesture.
        F('touchstart', [{x: 10, y: 300, id: 1}]);
        F('touchmove', [{x: 15, y: 300, id: 1}]);
        F('touchend', [], [{x: 15, y: 300, id: 1}]);
        // Two-finger touch: tracking early-returns on the multi-touch start.
        F('touchstart', [{x: 200, y: 300, id: 1}, {x: 300, y: 300, id: 2}]);
        // Finger 2 lifts; finger 1 keeps moving — touchend with one remaining.
        F('touchend', [{x: 200, y: 300, id: 1}], [{x: 300, y: 300, id: 2}]);
        // Surviving finger moves straight down. Without re-baselining the delta
        // is dx=190 (bogus 'h' lock + arm); with it, dy=+40 -> 'v'.
        const mv = F('touchmove', [{x: 200, y: 340, id: 1}]);
        const mv2 = F('touchmove', [{x: 200, y: 380, id: 1}]);
        return {mv, mv2, state: window.__lockTestState()};
    }""")
    st = r["state"]
    assert st["lock"] == "v", f"surviving finger locked '{st['lock']}' off a stale baseline"
    assert st["armed"] is False, f"blocker re-armed after partial release: {st}"
    assert r["mv"] is False and r["mv2"] is False, "surviving finger's scroll was preventDefault-ed"


def test_real_touch_diagonal_flick_scrolls(ios_page):
    """End-to-end smoke with real CDP touch input on the iPhone-like context:
    a flick that twitches right then goes up must still scroll #sidebar-scroll.

    Note: Chromium re-classifies the gesture once it turns vertical-dominant,
    so this passes even pre-fix — it guards that the fix does not break native
    scroll for real touches. The strict regression pins are the synthetic
    tests above (they fail against the pre-fix code)."""
    page = ios_page
    scrollable = page.evaluate(
        "(() => { const s = document.getElementById('sidebar-scroll'); return s.scrollHeight - s.clientHeight; })()"
    )
    assert scrollable > 100, f"root list not scrollable: {scrollable}"
    sess = page.context.new_cdp_session(page)

    def t(type_, pts):
        sess.send("Input.dispatchTouchEvent", {
            "type": type_,
            "touchPoints": [{"x": float(p[0]), "y": float(p[1]), "id": (p[2] if len(p) > 2 else 1)} for p in pts],
        })

    # edge-zone start (x<24) so the h-gate is open even at root; a slight
    # rightward twitch then a clearly vertical flick must still scroll.
    # Small sleeps keep Chromium from coalescing the moves into one gesture
    # summary — with no gap the events collapse and the pre-fix bug is masked.
    t("touchStart", [(15, 700, 1)])
    for pt in [(30, 700, 1), (33, 640, 1), (35, 560, 1), (35, 440, 1), (35, 330, 1)]:
        t("touchMove", [pt])
        time.sleep(0.03)
    t("touchEnd", [])
    page.wait_for_timeout(200)

    st = page.evaluate("document.getElementById('sidebar-scroll').scrollTop")
    assert st > 40, f"diagonal flick did not scroll (scrollTop={st}) — h-drag captured the gesture"
