"""S09 (#84) viewer composition: PairController wiring PairUI ⇄ PairClient ⇄
ViewerNavigation ⇄ PairedView is exercised end-to-end over the real
``/ws/reader-sync`` transport — two real browser contexts against the live
test server, no protocol fakes.

Covers the #84 machine-checkable criteria: shared-spread establishment from
the leader's current PageRef, exactly one wire request per user input on
every input path, leader/follower convergence, leader browse→selecting→
cancel→work-change phases, follower work change = LEAVE + solo, shared-anchor
URL/progress writes (blank side never saves null), one hashchange listener,
swipe/remote races converging on the latest commit, transport fallback, and
zero sockets in solo mode.
"""

import json
import sys
from pathlib import Path

import pytest
from playwright.sync_api import expect

sys.path.insert(0, str(Path(__file__).parent))
from viewer_touch import drag, settle, touch_end, touch_session


# Injected before any page script: counts addEventListener registrations by
# type (the hash-listener-count criterion) and spies on every WebSocket
# constructed + frame sent (socket-count / request-count criteria).
INSTRUMENT = """(() => {
  window.__listenerCount = {};
  const origAdd = EventTarget.prototype.addEventListener;
  EventTarget.prototype.addEventListener = function (t, f, o) {
    window.__listenerCount[t] = (window.__listenerCount[t] || 0) + 1;
    return origAdd.call(this, t, f, o);
  };
  window.__wsLog = [];
  window.__wsConns = [];
  const RealWS = window.WebSocket;
  function SpyWS(url, p) {
    const ws = (p !== undefined) ? new RealWS(url, p) : new RealWS(url);
    window.__wsConns.push(ws);
    const send = ws.send.bind(ws);
    ws.send = (d) => {
      try { window.__wsLog.push(JSON.parse(d)); } catch (e) { /* non-JSON */ }
      return send(d);
    };
    return ws;
  }
  SpyWS.prototype = RealWS.prototype;
  SpyWS.CONNECTING = 0; SpyWS.OPEN = 1; SpyWS.CLOSING = 2; SpyWS.CLOSED = 3;
  window.WebSocket = SpyWS;
})()"""


def _dev_page(browser, server, viewport=None):
    """A second-device window: fresh context + page landed on the app."""
    ctx = browser.new_context(
        viewport=viewport or {"width": 900, "height": 600},
        has_touch=True)
    ctx.add_init_script(INSTRUMENT)
    pg = ctx.new_page()
    pg.goto(server)
    pg.wait_for_selector(".tile")
    return ctx, pg


@pytest.fixture
def devs(browser, server):
    ctx_l, pl = _dev_page(browser, server)
    ctx_f, pf = _dev_page(browser, server)
    yield pl, pf
    ctx_l.close()
    ctx_f.close()


def _open(pg, folder, name=None):
    """Open a work (and optional page) through the navigation port."""
    pg.evaluate(
        "(f) => window.ViewerNavigation.request("
        "{type:'openWork', folder:f, via:'tile'})",
        folder,
    )
    pg.wait_for_function("() => (window.images || []).length > 0")
    if name:
        pg.evaluate(
            "([f, n]) => window.ViewerNavigation.request("
            "{type:'goto', page:{folder:f, name:n}, via:'test'})",
            [folder, name],
        )
        pg.wait_for_function(
            "(n) => (window.images[window.currentIdx] || {}).name === n",
            arg=name,
        )
    settle(pg)


def _pair_now(pl, pf, timeout=15000):
    """Drive the real pairing handshake through the controller surface."""
    pl.evaluate("() => window.PairController.create()")
    pl.wait_for_function("() => !!window.PairController.state.code",
                         timeout=timeout)
    code = pl.evaluate("() => window.PairController.state.code")
    assert code and len(str(code)) == 6
    pf.evaluate("(c) => window.PairController.join(c)", code)
    pl.wait_for_function("() => !!window.PairController.state.joinRequest",
                         timeout=timeout)
    pl.evaluate("() => window.PairController.approve()")
    for p in (pl, pf):
        p.wait_for_function(
            "() => window.PairController.state.status === 'connected'"
            " || window.PairController.state.status === 'loading'",
            timeout=timeout)
        p.wait_for_function(
            "() => !!(window.PairedView && window.PairedView.state"
            " && window.PairedView.state().active)",
            timeout=timeout)
        # 'loading' drops reading-gated intents — wait for the READY
        # round-trip to settle the session into 'reading' before ops.
        p.wait_for_function(
            "() => window.PairController.state.phase === 'reading'",
            timeout=timeout)


def _cstate(pg):
    return pg.evaluate("() => window.PairController.state")


def _pv(pg):
    return pg.evaluate("() => window.PairedView.state()")


def _synced(pg1, pg2, timeout=15000):
    """Wait until device 2 displays device 1's committed spread+revision."""
    pg1.wait_for_function(
        "() => window.PairController.state.spreadId != null", timeout=timeout)
    sid = pg1.evaluate("() => window.PairController.state.spreadId")
    rev = pg1.evaluate("() => window.PairController.state.revision")
    pg2.wait_for_function(
        "([s, r]) => window.PairController.state.spreadId === s"
        " && window.PairController.state.revision === r",
        arg=[sid, rev], timeout=timeout)
    return sid, rev


def _sent(pg, types=None):
    log = pg.evaluate("() => window.__wsLog")
    if types is None:
        return log
    return [f for f in log if f.get("type") in types]


def _intents(pg, types=None):
    log = pg.evaluate("() => (window.PairController._sentLog || [])")
    if types is None:
        return log
    return [e for e in log if e.get("type") in types]


# ── solo mode is untouched ────────────────────────────────────────────────

def test_solo_opens_no_socket_and_pair_idle(browser, server):
    """Browsing + reading solo must never open a pairing socket."""
    ctx, pg = _dev_page(browser, server)
    try:
        _open(pg, "long")
        pg.keyboard.press("ArrowLeft")          # next spread
        pg.wait_for_function("() => window.currentIdx === 2")
        assert pg.evaluate("() => window.__wsConns.length") == 0
        assert pg.evaluate("() => window.__wsLog.length") == 0
        assert pg.evaluate("() => window.PairController.state.status") == "idle"
    finally:
        ctx.close()


# ── pairing establishment ─────────────────────────────────────────────────

def test_pair_establish_shares_leader_spread(devs):
    """The shared spread contains the leader's current PageRef; the
    follower's saved ReadingProgress has no say in the placement."""
    pl, pf = devs
    # Seed a stale resume point on the follower — it must be ignored.
    pf.evaluate(
        "() => localStorage.setItem('bookq_progress', JSON.stringify("
        "{'long': {page: 20, total: 30, ts: Date.now()}}))")
    _open(pl, "long", "p005.png")
    _pair_now(pl, pf)

    ls, fs = _pv(pl), _pv(pf)
    assert ls["active"] is True and fs["active"] is True
    assert ls["side"] == "left" and fs["side"] == "right"   # leader-left default
    assert ls["spreadId"] == fs["spreadId"]
    # offset-0 pairing: p005 is right, p006 is left of spread c0:s2
    assert ls["ref"]["name"] == "p006.png"
    assert fs["ref"]["name"] == "p005.png"
    assert ls["anchorRef"]["name"] == "p005.png"
    assert fs["anchorRef"]["name"] == "p005.png"
    # Both devices render the leader's manifest, not the follower's progress.
    for p in (pl, pf):
        assert p.evaluate("() => window.images.length") == 30
        assert p.evaluate("() => window.images[4].name") == "p005.png"


# ── one request per operation, every input path ───────────────────────────

def test_all_input_paths_one_request_and_converge(devs):
    """key / tap-zone / slider / overview / offset each emit exactly one
    wire request and both devices land on the same shared spread."""
    pl, pf = devs
    _open(pl, "long", "p005.png")
    _pair_now(pl, pf)

    # — keyboard: ArrowLeft = next spread (RT-L), crossChapter=False —
    s0 = _pv(pl)["spreadId"]
    n0 = len(_intents(pl, {"turn"}))
    pl.keyboard.press("ArrowLeft")
    pl.wait_for_function("(s) => window.PairedView.state().spreadId !== s", arg=s0)
    _synced(pl, pf)
    assert len(_intents(pl, {"turn"})) == n0 + 1

    # — tap zone: #zone-prev is "next" under the RT-L default —
    s0 = _pv(pl)["spreadId"]
    n0 = len(_intents(pl, {"turn"}))
    pl.eval_on_selector("#zone-prev", "el => el.click()")
    pl.wait_for_function("(s) => window.PairedView.state().spreadId !== s", arg=s0)
    _synced(pl, pf)
    assert len(_intents(pl, {"turn"})) == n0 + 1

    # — slider: one input event = one goto request —
    s0 = _pv(pl)["spreadId"]
    n0 = len(_intents(pl, {"goto"}))
    pl.eval_on_selector(
        "#page-slider",
        "el => { el.value = '7'; el.dispatchEvent(new Event('input')); }")
    pl.wait_for_function("(s) => window.PairedView.state().spreadId !== s", arg=s0)
    _synced(pl, pf)
    assert len(_intents(pl, {"goto"})) == n0 + 1

    # — overview: one cell click = one goto request —
    s0 = _pv(pl)["spreadId"]
    n0 = len(_intents(pl, {"goto"}))
    pl.eval_on_selector("#overview-btn", "el => el.click()")
    pl.wait_for_selector(".ov-cell")
    # prebuild scrolls the grid on open; a cell tap inside INERTIA_MS (100ms)
    # is treated as momentum-stop, not a pick.
    pl.wait_for_timeout(200)
    pl.eval_on_selector(".ov-cell[data-gidx='0']", "el => el.click()")
    pl.wait_for_function("(s) => window.PairedView.state().spreadId !== s", arg=s0)
    _synced(pl, pf)
    assert len(_intents(pl, {"goto"})) == n0 + 1

    # — offset toggle: one click = one setOffset request; the shared table
    #   is rebuilt server-side (manifestVersion bumps on both) —
    mv0 = _cstate(pl)["manifestVersion"]
    n0 = len(_intents(pl, {"setOffset"}))
    pl.eval_on_selector("#spread-shift", "el => el.click()")
    pl.wait_for_function(
        "(v) => window.PairController.state.manifestVersion !== v", arg=mv0)
    pf.wait_for_function(
        "(v) => window.PairController.state.manifestVersion === v", arg=mv0 + 1)
    # The rebuilt manifest re-enters 'loading' until READY settles.
    for p in (pl, pf):
        p.wait_for_function(
            "() => window.PairController.state.phase === 'reading'",
            timeout=15000)
    _synced(pl, pf)
    assert len(_intents(pl, {"setOffset"})) == n0 + 1

    # — swipe: release past the threshold = one turn request —
    s0 = _pv(pl)["spreadId"]
    n0 = len(_intents(pl, {"turn"}))
    sess = touch_session(pl)
    drag(sess, 100, 300, 750, 300, steps=8)    # right drag = next (RT-L)
    pl.wait_for_function("(s) => window.PairedView.state().spreadId !== s", arg=s0)
    _synced(pl, pf)
    assert len(_intents(pl, {"turn"})) == n0 + 1


def test_follower_initiated_turn_converges(devs):
    pl, pf = devs
    _open(pl, "long", "p005.png")
    _pair_now(pl, pf)
    s0 = _pv(pf)["spreadId"]
    n0 = len(_intents(pf, {"turn"}))
    pf.keyboard.press("ArrowLeft")
    pf.wait_for_function("(s) => window.PairedView.state().spreadId !== s", arg=s0)
    sid, _ = _synced(pl, pf)
    assert len(_intents(pf, {"turn"})) == n0 + 1
    assert _pv(pl)["spreadId"] == sid


# ── leader browse / work change ───────────────────────────────────────────

def test_leader_browse_selecting_cancel_then_change_work(devs):
    pl, pf = devs
    _open(pl, "long", "p005.png")
    _pair_now(pl, pf)
    before = _pv(pl)["spreadId"]

    # ☰ on the leader pauses the session at 'selecting' on both devices.
    pl.eval_on_selector("#menu-btn", "el => el.click()")
    pl.wait_for_function(
        "() => document.getElementById('sidebar').classList.contains('open')")
    for p in (pl, pf):
        p.wait_for_function(
            "() => window.PairController.state.phase === 'selecting'",
            timeout=15000)

    # Cancel: closing the sidebar returns the leader to the shared spread.
    pl.evaluate("() => window.closeSidebar()")
    for p in (pl, pf):
        p.wait_for_function(
            "() => window.PairController.state.phase === 'reading'",
            timeout=15000)
    assert _pv(pl)["active"] is True
    assert _pv(pl)["spreadId"] == before

    # Pick a different work — both devices follow into the new manifest.
    pl.eval_on_selector("#menu-btn", "el => el.click()")
    pl.wait_for_function(
        "() => window.PairController.state.phase === 'selecting'",
        timeout=15000)
    pl.eval_on_selector(".tile[data-path='simple_manga']", "el => el.click()")
    for p in (pl, pf):
        p.wait_for_function(
            "() => window.PairController.state.phase === 'reading'",
            timeout=15000)
        p.wait_for_function(
            "() => (window.images[0] || {}).folder === 'simple_manga'")
        assert p.evaluate("() => window.images.length") == 5
    _synced(pl, pf)
    assert _pv(pl)["spreadId"] == _pv(pf)["spreadId"]


def test_follower_work_change_leaves_pair(devs):
    """followerの作品変更はLEAVE後の単独操作: the session ends for both and
    the leader's solo book/position are untouched by the work pick itself."""
    pl, pf = devs
    _open(pl, "long", "p005.png")
    _open(pf, "series/vol1")
    _pair_now(pl, pf)
    assert _pv(pl)["active"] is True

    pf.evaluate(
        "() => window.ViewerNavigation.request("
        "{type:'openWork', folder:'simple_manga', via:'tile'})")
    for p in (pl, pf):
        p.wait_for_function(
            "() => window.PairController.state.status === 'ended'",
            timeout=15000)
        p.wait_for_function(
            "() => !(window.PairedView && window.PairedView.state"
            " && window.PairedView.state().active)")
    # follower's pick became a solo open; leader fell back to its own book.
    pf.wait_for_function("() => window.currentFolder === 'simple_manga'")
    assert pf.evaluate("() => window.images.length") == 5
    pl.wait_for_function("() => window.currentFolder === 'long'")
    assert pl.evaluate("() => window.images.length") == 30
    assert pl.evaluate("() => window.images[window.currentIdx].name") == "p005.png"


# ── shared anchor: URL hash + ReadingProgress on both devices ─────────────

def test_anchor_hash_and_progress_on_both_and_blank_side(devs):
    pl, pf = devs
    _open(pl, "simple_manga")
    _pair_now(pl, pf)
    # Drive to the trailing spread (11.png right, blank left for leader).
    pl.evaluate(
        "() => window.ViewerNavigation.request({type:'goto',"
        " page:{folder:'simple_manga', name:'11.png'}, via:'test'})")
    pl.wait_for_function("() => window.PairedView.state().ref === null")
    pf.wait_for_function("() => window.PairedView.state().ref.name === '11.png'")

    ls = _pv(pl)
    assert ls["blank"] is True or ls["ref"] is None
    # Both URLs mirror the shared anchor, not each device's own side.
    expect(pf.locator("#counter")).to_have_text("5 / 5")
    expect(pl.locator("#counter")).to_have_text("空白")
    pl.wait_for_function("() => location.hash.includes('simple_manga')")
    assert pl.evaluate("() => location.hash") == \
        pf.evaluate("() => location.hash")
    assert "p=4" in pl.evaluate("() => location.hash")
    # ReadingProgress (debounced 500ms): the anchor's chapter-local idx —
    # never null, never the peer's side.
    for p in (pl, pf):
        p.wait_for_function(
            "() => JSON.parse(localStorage.getItem('bookq_progress') || '{}')"
            "['simple_manga'] && JSON.parse(localStorage.getItem"
            "('bookq_progress'))['simple_manga'].page === 4",
            timeout=5000)


def test_spread_shift_usable_in_paired_portrait(browser, server):
    """#112: the ◀ spread-shift must be reachable in paired mode even on a
    portrait device (spread-controls are otherwise landscape-only), while
    the solo-only 2P toggle stays hidden."""
    ctx_l, pl = _dev_page(browser, server)
    ctx_f, pf = _dev_page(browser, server,
                          viewport={"width": 390, "height": 844})
    try:
        _open(pl, "simple_manga")
        _pair_now(pl, pf)
        # portrait follower: shift visible via pair-mode, 2P toggle hidden
        expect(pf.locator("#spread-shift")).to_be_visible()
        expect(pf.locator("#spread-toggle")).to_be_hidden()
        # a real click → setOffset intent → shared manifest bump on both
        mv0 = _cstate(pf)["manifestVersion"]
        n0 = len(_intents(pf, {"setOffset"}))
        pf.locator("#spread-shift").click()
        pf.wait_for_function(
            f"() => (window.PairController._sentLog || [])"
            f".filter(e => e.type === 'setOffset').length > {n0}")
        assert _intents(pf, {"setOffset"})[-1]["ok"]
        pf.wait_for_function(
            "(v) => window.PairController.state.manifestVersion !== v",
            arg=mv0)
        for p in (pl, pf):
            p.wait_for_function(
                "() => window.PairController.state.phase === 'reading'",
                timeout=15000)
        # the lit state mirrors the committed shared offset on both ends
        for p in (pl, pf):
            assert "active" in (
                p.locator("#spread-shift").get_attribute("class") or "")
    finally:
        ctx_l.close()
        ctx_f.close()


def test_side_swap_keeps_anchor_and_projection(devs):
    pl, pf = devs
    _open(pl, "long", "p005.png")
    _pair_now(pl, pf)
    assert _pv(pl)["ref"]["name"] == "p006.png"

    pl.evaluate("() => window.PairController.swapSides()")
    pl.wait_for_function("() => window.PairedView.state().side === 'right'")
    pf.wait_for_function("() => window.PairedView.state().side === 'left'")
    # Same shared spread/anchor — only the projection flips.
    assert _pv(pl)["spreadId"] == _pv(pf)["spreadId"]
    assert _pv(pl)["anchorRef"]["name"] == "p005.png"
    assert _pv(pl)["ref"]["name"] == "p005.png"      # leader now shows right
    assert _pv(pf)["ref"]["name"] == "p006.png"


# ── session lifecycle ─────────────────────────────────────────────────────

def test_leave_restores_both_solo_books(devs):
    pl, pf = devs
    _open(pl, "long", "p005.png")
    _open(pf, "series/vol1", "p2.png")
    _pair_now(pl, pf)

    pf.evaluate("() => window.PairController.leave()")
    for p in (pl, pf):
        p.wait_for_function(
            "() => window.PairController.state.status === 'ended'",
            timeout=15000)
        p.wait_for_function(
            "() => !(window.PairedView && window.PairedView.state"
            " && window.PairedView.state().active)")
    # Solo snapshots are restored page-for-page.
    pl.wait_for_function("() => window.currentFolder === 'long'")
    assert pl.evaluate("() => window.images.length") == 30
    assert pl.evaluate("() => window.images[window.currentIdx].name") == "p005.png"
    pf.wait_for_function("() => window.currentFolder === 'series/vol1'")
    assert pf.evaluate("() => window.images.length") == 2
    assert pf.evaluate("() => window.images[window.currentIdx].name") == "p2.png"


def test_reload_resumes_into_active_pair(devs):
    pl, pf = devs
    _open(pl, "long", "p005.png")
    _pair_now(pl, pf)
    committed = _cstate(pf)["spreadId"]

    pf.reload()
    pf.wait_for_selector(".tile")
    pf.wait_for_function(
        "() => window.PairController.state.status === 'connected'"
        " || window.PairController.state.status === 'loading'",
        timeout=20000)
    pf.wait_for_function(
        "() => !!(window.PairedView && window.PairedView.state"
        " && window.PairedView.state().active)", timeout=20000)
    assert _pv(pf)["spreadId"] == committed
    assert _pv(pf)["side"] == "right"


# ── hash listener count + URL races ───────────────────────────────────────

def test_single_hashchange_listener_through_pairing(devs):
    pl, pf = devs
    for p in (pl, pf):
        assert p.evaluate("() => window.__listenerCount['hashchange'] || 0") == 1
    _open(pl, "long", "p005.png")
    _pair_now(pl, pf)
    pl.keyboard.press("ArrowLeft")
    pl.wait_for_function("() => window.currentIdx === 6")
    for p in (pl, pf):
        assert p.evaluate("() => window.__listenerCount['hashchange'] || 0") == 1


# ── races converge on the latest commit ───────────────────────────────────

def test_swipe_held_while_remote_commit_lands(devs):
    """A remote commit must win over a half-finished local drag; after the
    release the final display equals the last server commit (rollback 0)."""
    pl, pf = devs
    _open(pl, "long", "p005.png")
    _pair_now(pl, pf)

    sess = touch_session(pf)
    drag(sess, 700, 300, 300, 300, steps=4, hold=True)   # finger still down
    # A committed turn arrives mid-drag.
    pl.keyboard.press("ArrowLeft")
    pf.wait_for_function("() => window.PairedView.state().spreadId === 'c0:s3'")
    touch_end(sess)
    settle(pf, 4)
    sid, rev = _synced(pl, pf)
    # Whatever the release decided, the final display is the latest commit.
    assert _pv(pl)["spreadId"] == _pv(pf)["spreadId"] == sid
    assert _cstate(pl)["revision"] == _cstate(pf)["revision"]


def test_rapid_turns_converge_on_latest(devs):
    pl, pf = devs
    _open(pl, "long", "p005.png")
    _pair_now(pl, pf)
    s0 = _pv(pl)["spreadId"]
    for _ in range(5):
        pl.keyboard.press("ArrowLeft")
    pl.wait_for_function("(s) => window.PairedView.state().spreadId !== s", arg=s0)
    sid, rev = _synced(pl, pf)
    assert _pv(pl)["spreadId"] == _pv(pf)["spreadId"] == sid
    assert _pv(pl)["anchorRef"] == _pv(pf)["anchorRef"]


# ── transport switching / fallback ────────────────────────────────────────

FAKE_DC_JS = """() => {
  window.__dc = {
    readyState: 'open',
    bufferedAmount: 0,
    send(d) { this.sentCount = (this.sentCount || 0) + 1; this.lastFrame = d; },
    close() {
      if (this.readyState === 'closed') return;
      this.readyState = 'closed';
      if (this.onclose) this.onclose();
    },
    onopen: null, onclose: null, onmessage: null,
  };
  window.PairController.attachDataChannel(window.__dc);
}"""


def test_dc_close_falls_back_to_server_ws(devs):
    """Dead webrtc-dc must fall back to server-ws; committed state keeps
    converging through the control channel."""
    pl, pf = devs
    _open(pl, "long", "p005.png")
    _pair_now(pl, pf)

    pl.evaluate(FAKE_DC_JS)
    pf.evaluate(FAKE_DC_JS)
    for p in (pl, pf):
        p.wait_for_function(
            "() => window.PairController.state.transport === 'webrtc-dc'",
            timeout=15000)

    # Leader sends still ride the control channel — convergence holds.
    s0 = _pv(pl)["spreadId"]
    pl.keyboard.press("ArrowLeft")
    pl.wait_for_function("(s) => window.PairedView.state().spreadId !== s", arg=s0)
    _synced(pl, pf)

    # Kill both data channels — the session degrades back to server-ws.
    pl.evaluate("() => window.__dc.close()")
    pf.evaluate("() => window.__dc.close()")
    for p in (pl, pf):
        p.wait_for_function(
            "() => window.PairController.state.transport === 'server-ws'",
            timeout=15000)

    s0 = _pv(pf)["spreadId"]
    pf.keyboard.press("ArrowRight")          # follower: prev spread
    pf.wait_for_function("(s) => window.PairedView.state().spreadId !== s", arg=s0)
    _synced(pl, pf)


# ── version incompatibility → ended + update notice ───────────────────────

def test_foreign_protocol_version_ends_with_update_notice(devs):
    pl, pf = devs
    _open(pl, "long", "p005.png")
    _pair_now(pl, pf)

    # Inject a foreign-version inbound frame on the follower's socket.
    pf.evaluate(
        "() => window.__wsConns[0].onmessage("
        "{data: JSON.stringify({v: 2, type: 'STATE'})})")
    pf.wait_for_function(
        "() => window.PairController.state.endReason === 'incompatible'",
        timeout=15000)
    # Sync stopped, paired rendering torn down, the update notice surfaced.
    pf.wait_for_function(
        "() => !(window.PairedView && window.PairedView.state"
        " && window.PairedView.state().active)")
    expect(pf.locator("#toast")).to_contain_text("再読み込み")
