"""Browse-list scroll mirror in paired mode — UI_SYNC end-to-end.

Two real browser contexts over the real ``/ws/reader-sync`` transport.
While the leader browses the work/folder list (session phase 'selecting'),
``browse/pairSync.js`` on each device relays its own ``{path, scrollTop,
viewH, viewMode}`` as ephemeral UI_SYNC frames; the peer applies them under
the invariant ``followerTop == leaderTop + leaderClientHeight`` — the
follower always shows exactly one leader-screen below the leader.

Covered: follower sidebar auto-open + path/scroll adoption, the +viewH
offset with unequal device viewport heights, bidirectional scroll AND
bidirectional folder nav, leader-close disarm (follower returns to the
shared reader), leader-search suspend, follower work pick = LEAVE + solo,
and the absence of a send/apply echo storm.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_reader_sync_integration import (  # noqa: E402
    _dev_page, _open, _pair_now, _cstate, _pv, _sent,
)

_SC = "document.getElementById('sidebar-scroll')"
_SB = "document.getElementById('sidebar')"


def _devs_diff_heights(browser, server):
    """Two 'devices' with DIFFERENT viewport heights — proves the offset is
    the leader's viewH, not a shared constant or the follower's own."""
    ctx_l, pl = _dev_page(browser, server,
                          viewport={"width": 390, "height": 620})
    ctx_f, pf = _dev_page(browser, server,
                          viewport={"width": 390, "height": 460})
    return ctx_l, pl, ctx_f, pf


def _scroll(pg):
    return pg.evaluate(f"() => ({_SC}).scrollTop")


def _view_h(pg):
    return pg.evaluate(f"() => ({_SC}).clientHeight")


def _scroll_h(pg):
    return pg.evaluate(f"() => ({_SC}).scrollHeight")


def _set_scroll(pg, top):
    pg.evaluate(f"([t]) => ({_SC}).scrollTop = t", [top])


def _sidebar_open(pg):
    return pg.evaluate(f"() => ({_SB}).classList.contains('open')")


def _browse_path(pg):
    return pg.evaluate("() => window.currentBrowsePath")


def _ui_syncs(pg):
    return _sent(pg, {"UI_SYNC"})


def _start_browse(pl, pf, timeout=20000):
    """Leader ☰ -> selecting on both -> follower's mirrored sidebar open.

    Retry: a stale 'reading'-phase commit landing in the window between the
    sidebar opening and the 'selecting' STATE arriving can re-close it via
    applyPairedCommit -> enterPairStrip -> closeSidebar (pre-existing race,
    unrelated to the mirror). Re-tapping ☰ — as a user would — re-issues the
    browse intent and converges once the settle traffic drains.
    """
    deadline = time.time() + timeout / 1000.0
    while True:
        if not _sidebar_open(pl):
            pl.eval_on_selector("#menu-btn", "el => el.click()")
        try:
            pl.wait_for_function(
                "() => document.getElementById('sidebar')"
                ".classList.contains('open')", timeout=4000)
            pl.wait_for_function(
                "() => window.PairController.state.phase === 'selecting'",
                timeout=4000)
            break
        except Exception:
            if time.time() > deadline:
                raise
    pf.wait_for_function(
        "() => window.PairController.state.phase === 'selecting'",
        timeout=timeout)
    pf.wait_for_function(
        "() => document.getElementById('sidebar').classList.contains('open')",
        timeout=timeout)


def _wait_follower_offset(pl, pf, timeout=10000):
    """follower.scrollTop converges to leader.scrollTop + leader.clientHeight."""
    pl.wait_for_function(
        "() => !!window.__pairBrowseSync", timeout=timeout)
    pf.wait_for_function(
        "() => !!window.__pairBrowseSync", timeout=timeout)
    # converge: sample leader's current values, then wait for follower to hit
    # exactly top+viewH (the invariant), allowing a small float tolerance.
    deadline = time.time() + timeout / 1000.0
    lt = lh = ft = 0
    while time.time() < deadline:
        lt = _scroll(pl)
        lh = _view_h(pl)
        ft = _scroll(pf)
        if abs(ft - (lt + lh)) <= 2:
            return lt, lh, ft
        time.sleep(0.05)
    raise AssertionError(
        f"follower scrollTop {ft} never reached leader top {lt} + viewH {lh}")


def test_mirror_open_adopts_leader_view_at_plus_viewH(browser, server):
    ctx_l, pl, ctx_f, pf = _devs_diff_heights(browser, server)
    try:
        _open(pl, "long", "p005.png")
        _pair_now(pl, pf)
        assert not _sidebar_open(pf)

        _start_browse(pl, pf)

        # The leader sits on the root list; the follower mirrors path and the
        # +viewH offset from the leader's own scroller geometry.
        assert _browse_path(pf) == _browse_path(pl)
        lt, lh, ft = _wait_follower_offset(pl, pf)
        assert ft >= 0 and lt + lh >= 0
        assert _ui_syncs(pl), "leader must emit UI_SYNC"
        assert pf.evaluate("() => window.__pairBrowseSync.unlocked") is True
        # the mirrored sidebar is fully interactive (tiles exist to tap)
        assert pf.locator("#tile-grid .tile").count() == \
            pl.locator("#tile-grid .tile").count()
    finally:
        ctx_l.close(); ctx_f.close()


def test_leader_scroll_tracks_with_leader_viewH(browser, server):
    ctx_l, pl, ctx_f, pf = _devs_diff_heights(browser, server)
    try:
        _open(pl, "long", "p005.png")
        _pair_now(pl, pf)
        _start_browse(pl, pf)
        _wait_follower_offset(pl, pf)

        for top in (150, 420, 60):
            _set_scroll(pl, top)
            lt, lh, ft = _wait_follower_offset(pl, pf)
            assert lt == top
            assert ft == pytest.approx(top + lh, abs=2), \
                f"follower must sit exactly one leader-screen below: {ft} vs {top}+{lh}"
    finally:
        ctx_l.close(); ctx_f.close()


def test_follower_scroll_drives_leader_minus_viewH(browser, server):
    """Bidirectional: a follower-originated scroll moves the leader too —
    the leader lands one screen ABOVE the follower."""
    ctx_l, pl, ctx_f, pf = _devs_diff_heights(browser, server)
    try:
        _open(pl, "long", "p005.png")
        _pair_now(pl, pf)
        _start_browse(pl, pf)
        _wait_follower_offset(pl, pf)

        lh = _view_h(pl)
        target_f = lh + 300            # inside the follower's scroll range
        _set_scroll(pf, target_f)
        pf.wait_for_function(
            f"() => Math.abs(({_SC}).scrollTop - {target_f}) <= 2",
            timeout=5000)
        # leader converges to follower.top - leader.clientHeight
        pl.wait_for_function(
            f"() => Math.abs(({_SC}).scrollTop - ({target_f} - {_view_h(pl)})) <= 2",
            timeout=10000)
        assert _ui_syncs(pf), "follower must emit UI_SYNC back to the leader"
    finally:
        ctx_l.close(); ctx_f.close()


def test_folder_nav_and_viewmode_propagate_bidirectionally(browser, server):
    ctx_l, pl, ctx_f, pf = _devs_diff_heights(browser, server)
    try:
        _open(pl, "long", "p005.png")
        _pair_now(pl, pf)
        _start_browse(pl, pf)
        _wait_follower_offset(pl, pf)

        # leader drills into 'group' — follower navigates to the same level.
        pl.eval_on_selector(
            "#tile-grid .tile[data-path='group']", "el => el.click()")
        for p in (pl, pf):
            p.wait_for_function(
                "() => window.currentBrowsePath === 'group'", timeout=10000)

        # follower drills into 'series' — the leader follows to 'group' first
        # (its nav is relative), so drive the follower back to root then into
        # 'series' to keep the shared level deterministic.
        pf.eval_on_selector("#browse-header", "el => el.click()")   # back: group -> root
        for p in (pl, pf):
            p.wait_for_function(
                "() => window.currentBrowsePath === null", timeout=10000)
        pf.eval_on_selector(
            "#tile-grid .tile[data-path='series']", "el => el.click()")
        for p in (pl, pf):
            p.wait_for_function(
                "() => window.currentBrowsePath === 'series'", timeout=10000)

        # display mode: leader tile -> list cycles on both devices.
        pl.eval_on_selector("#footer-refresh-btn", "el => el.click()")
        for p in (pl, pf):
            p.wait_for_function(
                "() => window.viewMode === 'list'", timeout=10000)
        assert pf.evaluate(
            "() => document.getElementById('tile-grid').classList"
            ".contains('list-mode')")
    finally:
        ctx_l.close(); ctx_f.close()


def test_leader_close_disarms_and_follower_returns_to_reader(browser, server):
    ctx_l, pl, ctx_f, pf = _devs_diff_heights(browser, server)
    try:
        _open(pl, "long", "p005.png")
        _pair_now(pl, pf)
        _start_browse(pl, pf)
        _wait_follower_offset(pl, pf)

        pl.evaluate("() => window.closeSidebar()")
        for p in (pl, pf):
            p.wait_for_function(
                "() => window.PairController.state.phase === 'reading'",
                timeout=15000)
        pf.wait_for_function(
            "() => !document.getElementById('sidebar')"
            ".classList.contains('open')",
            timeout=10000)
        # the shared spread is back in front on both devices
        assert _pv(pl)["active"] is True and _pv(pf)["active"] is True
        assert _pv(pl)["spreadId"] == _pv(pf)["spreadId"]
    finally:
        ctx_l.close(); ctx_f.close()


def test_search_mode_suspends_mirror(browser, server):
    """Leader searching stops its scroll relay — the follower keeps the last
    mirrored level (search-result sync is out of scope for v1)."""
    ctx_l, pl, ctx_f, pf = _devs_diff_heights(browser, server)
    try:
        _open(pl, "long", "p005.png")
        _pair_now(pl, pf)
        _start_browse(pl, pf)
        _wait_follower_offset(pl, pf)

        pl.eval_on_selector(
            "#search-input",
            "el => { el.value='manga'; el.dispatchEvent(new Event('input')); }")
        pl.wait_for_function("() => window.searchMode === true",
                             timeout=10000)
        ft0 = _scroll(pf)
        fp0 = _browse_path(pf)
        # the leader's result list scrolls — nothing may reach the follower
        _set_scroll(pl, 500)
        time.sleep(1.2)
        assert _scroll(pf) == ft0
        assert _browse_path(pf) == fp0
        # clearing search re-enables the relay on the leader's next render
        pl.eval_on_selector(
            "#search-input",
            "el => { el.value=''; el.dispatchEvent(new Event('input')); }")
        pl.wait_for_function("() => window.searchMode === false",
                             timeout=10000)
        _set_scroll(pl, 90)
        lt, lh, ft = _wait_follower_offset(pl, pf)
        assert ft == pytest.approx(lt + lh, abs=2)
    finally:
        ctx_l.close(); ctx_f.close()


def test_apply_echo_does_not_reemit(browser, server):
    """A remote apply must not echo back: after convergence the follower's
    UI_SYNC send count stays flat."""
    ctx_l, pl, ctx_f, pf = _devs_diff_heights(browser, server)
    try:
        _open(pl, "long", "p005.png")
        _pair_now(pl, pf)
        _start_browse(pl, pf)
        _wait_follower_offset(pl, pf)

        n0 = len(_ui_syncs(pf))
        _set_scroll(pl, 260)
        _wait_follower_offset(pl, pf)
        # settle past the 400ms suppression window + 120ms trail — the applied
        # position must not re-emit at all (one-shot tolerance would mask a
        # broken echo guard).
        time.sleep(0.7)
        assert len(_ui_syncs(pf)) == n0, \
            f"echo storm: follower sent {len(_ui_syncs(pf))} frames " \
            f"after applying a remote scroll"
    finally:
        ctx_l.close(); ctx_f.close()


def test_follower_work_pick_leaves_pair_as_solo(browser, server):
    """Follower tile taps stay fully enabled: picking a work keeps the
    existing semantics — LEAVE the pair, open solo."""
    ctx_l, pl, ctx_f, pf = _devs_diff_heights(browser, server)
    try:
        _open(pl, "long", "p005.png")
        _pair_now(pl, pf)
        _start_browse(pl, pf)
        _wait_follower_offset(pl, pf)

        pf.eval_on_selector(
            "#tile-grid .tile[data-path='simple_manga']", "el => el.click()")
        for p in (pl, pf):
            p.wait_for_function(
                "() => window.PairController.state.status === 'ended'",
                timeout=15000)
        pf.wait_for_function(
            "() => window.currentFolder === 'simple_manga'", timeout=10000)
        assert pf.evaluate("() => window.images.length") == 5
        # The leader had already torn its reader down for 'selecting', so an
        # end-from-browse leaves it on the list — no solo restore (existing
        # semantics; the mirror adds nothing here).
        assert pl.evaluate("() => window.currentFolder") is None
        assert _sidebar_open(pl)
        assert pl.evaluate(
            "() => !(window.PairedView && window.PairedView.state"
            " && window.PairedView.state().active)")
    finally:
        ctx_l.close(); ctx_f.close()


# ── Deterministic single-page drives (no real pair; fake controller state) ──


def _solo_armed(pg, role):
    """Arm the mirror module on a plain page: patch PairController.state with
    a mutable fake + spy on sendUiSync, then drive a 'selecting' state."""
    return pg.evaluate(
        """([role]) => {
          const pbs = window.__pairBrowseSync;
          window.__uiSent = [];
          window.PairController.sendUiSync = (p) => {
            window.__uiSent.push(JSON.parse(JSON.stringify(p)));
            return true;
          };
          window.__fakeState = {status: 'idle'};
          Object.defineProperty(window.PairController, 'state',
            {configurable: true, get: () => window.__fakeState});
          window.__arm = (over) => {
            window.__fakeState = Object.assign({
              status: 'active', sessionId: 'S', phase: 'selecting', role,
              presence: {A: {status: 'connected', role: 'leader'},
                         B: {status: 'connected', role: 'follower'}},
            }, over);
            pbs.onState(window.__fakeState);
          };
          window.__disarm = () => {
            window.__fakeState = {status: 'idle'};
            pbs.onState(window.__fakeState);
          };
          window.__arm();
          return pbs.armed;
        }""", [role])


def test_presence_change_forces_resend(browser, server):
    """A presence-sig change re-emits the current position even when nothing
    scrolled — a reconnecting peer's missed frames get resent."""
    ctx, pg = _dev_page(browser, server,
                        viewport={"width": 390, "height": 460})
    try:
        pg.wait_for_selector(".tile")
        pg.evaluate("() => window.openSidebar()")
        assert _solo_armed(pg, "leader") is True
        n0 = pg.evaluate("() => window.__uiSent.length")
        assert n0 >= 1, "leader arm-edge must emit its position"
        # follower link flaps -> presence sig changes -> forced resend of an
        # otherwise-identical (deduped) payload
        pg.evaluate(
            """() => window.__arm({presence: {
                 A: {status: 'connected', role: 'leader'},
                 B: {status: 'connecting', role: 'follower'}}})""")
        pg.evaluate("() => window.__pairBrowseSync.flush()")
        assert pg.evaluate("() => window.__uiSent.length") == n0 + 1
    finally:
        ctx.close()


def test_stale_pending_frame_dropped_on_rearm(browser, server):
    """A frame stashed while disarmed expires: re-arming after the TTL must
    not yank the user to a stale folder/position."""
    ctx, pg = _dev_page(browser, server,
                        viewport={"width": 390, "height": 460})
    try:
        pg.wait_for_selector(".tile")
        assert _solo_armed(pg, "follower") is True
        pg.evaluate("() => window.__disarm()")
        # the shell opens the sidebar during initial browse restore — put it
        # in the closed state a real follower (which opened a work) has
        pg.evaluate("() => window.closeSidebar()")
        # in-flight frame stashes on disarm — backdate past the 2s TTL
        pg.evaluate(
            """() => window.__pairBrowseSync.applyRemote(
                 {view: 'browse', path: 'group', top: 100, viewH: 500},
                 Date.now() - 5000)""")
        pg.evaluate("() => window.__arm()")
        time.sleep(0.3)
        assert _browse_path(pg) is None
        assert not _sidebar_open(pg)
        # a fresh stash still applies on arm — TTL isn't overly aggressive
        pg.evaluate("() => window.__disarm()")
        pg.evaluate(
            """() => window.__pairBrowseSync.applyRemote(
                 {view: 'browse', path: 'series', top: 50, viewH: 500})""")
        pg.evaluate("() => window.__arm()")
        pg.wait_for_function(
            "() => window.currentBrowsePath === 'series'", timeout=5000)
        assert _sidebar_open(pg)
    finally:
        ctx.close()


def test_remote_nav_updates_url_and_scroll_positions(browser, server):
    """Remote folder nav keeps local URL state truthful (replaceState —
    no extra history entry) and preserves the departing level's scroll
    position exactly like local navigateToFolder does."""
    ctx, pg = _dev_page(browser, server,
                        viewport={"width": 390, "height": 460})
    try:
        pg.wait_for_selector(".tile")
        assert _solo_armed(pg, "follower") is True
        pg.evaluate(
            """() => window.__pairBrowseSync.applyRemote(
                 {view: 'browse', path: null, top: 0, viewH: 500})""")
        pg.wait_for_function(
            "() => document.getElementById('sidebar')"
            ".classList.contains('open')", timeout=5000)
        # user sits at a scrolled root position, then the peer navigates
        sat = pg.evaluate(
            f"() => {{ ({_SC}).scrollTop = 123; return ({_SC}).scrollTop; }}")
        assert sat > 0
        pg.evaluate(
            """() => window.__pairBrowseSync.applyRemote(
                 {view: 'browse', path: 'group', top: 0, viewH: 500})""")
        pg.wait_for_function(
            "() => window.currentBrowsePath === 'group'", timeout=5000)
        assert pg.evaluate("() => location.hash") == "#/browse/group"
        # departing root position was stored under the same key local nav uses
        assert pg.evaluate(
            "() => window.scrollPositions[window.getScrollKey(null)]") == sat
    finally:
        ctx.close()


def test_user_closed_sidebar_stays_closed_until_next_arm(browser, server):
    """If the follower user closes the mirrored sidebar, incoming frames
    still apply state but never re-open it during the same arm."""
    ctx, pg = _dev_page(browser, server,
                        viewport={"width": 390, "height": 460})
    try:
        pg.wait_for_selector(".tile")
        assert _solo_armed(pg, "follower") is True
        pg.evaluate(
            """() => window.__pairBrowseSync.applyRemote(
                 {view: 'browse', path: null, top: 0, viewH: 500})""")
        pg.wait_for_function(
            "() => document.getElementById('sidebar')"
            ".classList.contains('open')", timeout=5000)
        # user closes it (Esc / menu) — the class observer marks the arm
        pg.evaluate(
            "() => document.getElementById('sidebar')"
            ".classList.remove('open')")
        pg.wait_for_function(
            "() => window.__pairBrowseSync.userClosed === true",
            timeout=5000)
        pg.evaluate(
            """() => window.__pairBrowseSync.applyRemote(
                 {view: 'browse', path: null, top: 80, viewH: 500})""")
        time.sleep(0.3)
        assert not _sidebar_open(pg)
        # state still applies — follower transform is top + leader viewH
        assert _scroll(pg) == 580
        # a new arm clears the flag — mirroring resumes normally
        pg.evaluate("() => window.__disarm()")
        pg.evaluate("() => window.__arm()")
        pg.evaluate(
            """() => window.__pairBrowseSync.applyRemote(
                 {view: 'browse', path: null, top: 20, viewH: 500})""")
        pg.wait_for_function(
            "() => document.getElementById('sidebar')"
            ".classList.contains('open')", timeout=5000)
    finally:
        ctx.close()


def test_clamped_remote_position_is_not_echoed(browser, server):
    """A target beyond maxTop clamps scrollTop; the clamped value must never
    echo back — the peer's reverse transform would yank it ~1 screen up."""
    ctx, pg = _dev_page(browser, server,
                        viewport={"width": 390, "height": 460})
    try:
        pg.wait_for_selector(".tile")
        assert _solo_armed(pg, "follower") is True
        pg.evaluate(
            """() => window.__pairBrowseSync.applyRemote(
                 {view: 'browse', path: null, top: 1e7, viewH: 500})""")
        pg.wait_for_function(
            "() => document.getElementById('sidebar')"
            ".classList.contains('open')", timeout=5000)
        max_top = pg.evaluate(
            f"() => ({_SC}).scrollHeight - ({_SC}).clientHeight")
        pg.wait_for_function(
            f"() => Math.abs(({_SC}).scrollTop - {max_top}) <= 2",
            timeout=5000)
        pg.evaluate("() => window.__pairBrowseSync.flush()")
        assert pg.evaluate("() => window.__uiSent.length") == 0, \
            "clamped remote position must not echo back"
        # a genuine user scroll away from the held position DOES send
        pg.evaluate(f"() => ({_SC}).scrollTop = {max_top - 50}")
        pg.evaluate("() => window.__pairBrowseSync.flush()")
        sent = pg.evaluate("() => window.__uiSent")
        assert len(sent) == 1
        assert sent[0]["top"] == pytest.approx(max_top - 50, abs=2)
    finally:
        ctx.close()


def test_shell_watchdog_marks_healthy_and_stale(browser, server):
    """Deploy-consistency watchdog: a page running this deploy's full shell
    reports healthy; deleting any marker the new html requires reports stale
    (the new-html + old-js mix that killed pair browse-sync after #122)."""
    ctx, pg = _dev_page(browser, server,
                        viewport={"width": 390, "height": 460})
    try:
        pg.wait_for_selector(".tile")
        assert pg.evaluate("() => window.__shellStale()") is False
        # an old bundle lacks these markers — simulate each one missing
        assert pg.evaluate(
            "() => { delete window.openSidebar; return window.__shellStale(); }"
        ) is True
        assert pg.evaluate(
            "() => { window.BOOKQ_BROWSE_SYNC = false;"
            "        return window.__shellStale(); }"
        ) is True
    finally:
        ctx.close()


def test_mirror_needs_tree_level_scrollable(browser, server):
    """Precondition guard: the fixture's root level must actually scroll at
    the small viewports used here — otherwise the offset tests above can
    silently prove nothing."""
    ctx, pg = _dev_page(browser, server,
                        viewport={"width": 390, "height": 460})
    try:
        pg.wait_for_selector(".tile")
        assert _scroll_h(pg) > _view_h(pg), \
            "root tile list is not scrollable at 390x460 — fixture changed?"
    finally:
        ctx.close()
