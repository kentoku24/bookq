"""Folder-visibility × dual-reader sync guards (plan §8 / §11-D).

A committed state or UI_SYNC browse path whose work root is hidden in
normal mode must never render: the receiver denies it before PairedView/
_pendingMsg, releases the pair, and lands on the top list. Send-side picks
of hidden works never reach the wire. All socket-opening entry points wait
for FolderVisibility.ready().

Two levels, mirroring the existing pair suite:

  - REAL /ws/reader-sync — two live browser contexts on the dedicated
    visibility server (create/join/auto-approve/commit drive the actual
    denial over the wire). Used where the end-to-end invariant is the point:
    follower denial on a real commit, leader pick suppression, UI_SYNC over
    the wire, credential rejoin on reload.
  - MOCK level — the window.__pairBrowseSync seam +
    ViewerNavigation.applyCommittedState are driven directly where a real
    pair adds flakiness without extra signal: pending-stash non-resurrection,
    the unresolvable-manifest pass-through, and the _boot readiness ordering
    (gated ready() + WS spy, no network dependency).

Dedicated synthetic library + state dir via folder_visibility_helpers.py —
the shared conftest server/tree are untouched.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from folder_visibility_helpers import (  # noqa: E402
    VIS_ROOTS,
    build_vis_tree,
    request_browser,
    spawn_vis_server,
    vis_put,
    vis_reset,
)
from test_reader_sync_integration import (  # noqa: E402
    INSTRUMENT,
    _dev_page,
    _intents,
    _open,
    _pair_now,
    _pv,
    _sent,
    _synced,
)
from test_reader_browse_sync import _start_browse  # noqa: E402


@pytest.fixture(scope="module")
def vis_env(tmp_path_factory):
    tree = build_vis_tree(tmp_path_factory.mktemp("vis_pair_dl"))
    state = tmp_path_factory.mktemp("vis_pair_state")
    proc, base = spawn_vis_server(tree, state)
    yield base
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture(autouse=True)
def _vis_clean(vis_env):
    vis_reset(vis_env, list(VIS_ROOTS) + ["keep_a"])
    yield


@pytest.fixture
def vpage(vis_env, request):
    b = request_browser(request)
    context = b.new_context(viewport={"width": 390, "height": 844})
    pg = context.new_page()
    pg.goto(vis_env)
    pg.wait_for_selector(".tile")
    yield pg
    context.close()


def _wait_vis_ready(pg, timeout=10000):
    pg.wait_for_function(
        "() => window.FolderVisibility"
        " && window.FolderVisibility.readyState !== 'loading'",
        timeout=timeout)


def _reload_vis(pg):
    """Reload after a vis_put so the fresh snapshot is the boot judgment."""
    pg.reload()
    pg.wait_for_selector(".tile")
    _wait_vis_ready(pg)


def _arm_mirror(pg, role="follower"):
    """__pairBrowseSync 単体駆動用の偽セッション + leave/sendUiSync スパイ。

    Same shape as test_reader_browse_sync._solo_armed, plus a leave() spy so
    the denial path's PairController.leave() is observable without a socket.
    """
    return pg.evaluate(
        """([role]) => {
          const pbs = window.__pairBrowseSync;
          window.__uiSent = [];
          window.__leaveCalls = 0;
          const origLeave = window.PairController.leave.bind(
            window.PairController);
          window.PairController.leave = () => {
            window.__leaveCalls++;
            return origLeave();
          };
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


# ── committedFolderHidden predicate (mock level) ─────────────────────────

def test_committed_folder_hidden_resolves_folder_and_spread(vpage, vis_env):
    """O(1) resolution: manifest.folder first, else the committed spreadId's
    anchor PageRef; unresolvable manifests never block; 全表示 never blocks."""
    vis_put(vis_env, "hide_me", True)
    _reload_vis(vpage)
    out = vpage.evaluate("""() => {
      const FV = window.FolderVisibility;
      const tbl = [{id: 'c0:s0', right: {folder: 'hide_me', name: '1.png'},
                    left: {folder: 'hide_me', name: '2.png'}}];
      return {
        folder:    FV.committedFolderHidden(
                     {folder: 'hide_me'}, {spreadId: 'x'}),
        descend:   FV.committedFolderHidden({folder: 'hide_me/sub'}, {}),
        visible:   FV.committedFolderHidden({folder: 'keep_a'}, {}),
        spreadTbl: FV.committedFolderHidden(
                     {spreadTable: tbl}, {spreadId: 'c0:s0'}),
        alias:     FV.committedFolderHidden(
                     {spreads: [{id: 's0', left: {folder: 'hide_me',
                                                 name: '1.png'}}]},
                     {spreadId: 's0'}),
        miss:      FV.committedFolderHidden(
                     {spreadTable: tbl}, {spreadId: 'c0:s9'}),
        none:      FV.committedFolderHidden({}, {}),
        nullM:     FV.committedFolderHidden(null, null),
      };
    }""")
    assert out == {
        "folder": True, "descend": True, "visible": False,
        "spreadTbl": True, "alias": True, "miss": False,
        "none": False, "nullM": False,
    }
    vpage.evaluate("() => window.FolderVisibility.setShowAll(true)")
    assert vpage.evaluate(
        "() => window.FolderVisibility.committedFolderHidden"
        "({folder: 'hide_me'}, {})") is False


# ── applyCommittedState → app.js default handler (mock level) ─────────────

_SNAP = ("{v:1, sessionId:'s', revision:1, workEpoch:0, manifestVersion:1,"
         " phase:'reading', spreadId:'c0:s0', offset:0, leaderSide:'right',"
         " presence:{}}")


def test_apply_committed_hidden_manifest_folder_denied(vpage, vis_env):
    """manifest.folder 直判: hidden → redirect、PairedView は不活性のまま。"""
    vis_put(vis_env, "hide_me", True)
    _reload_vis(vpage)
    vpage.evaluate(
        "() => window.ViewerNavigation.applyCommittedState(" + _SNAP + ","
        "{folder:'hide_me', manifestVersion:1,"
        " spreadTable:[{id:'c0:s0', chapterFolder:'hide_me',"
        "  right:{folder:'hide_me',name:'1.png'},"
        "  left:{folder:'hide_me',name:'2.png'}}]},"
        "{role:'follower'})")
    vpage.wait_for_function("location.hash === ''")
    assert vpage.evaluate("() => window.currentFolder || null") is None
    assert vpage.evaluate(
        "() => !!(window.PairedView && window.PairedView.state"
        " && window.PairedView.state().active)") is False
    assert vpage.locator("#sidebar.open").count() == 1


def test_apply_committed_hidden_via_spread_lookup_denied(vpage, vis_env):
    """manifest.folder 不在時は spreadId → PageRef.folder で判定する。"""
    vis_put(vis_env, "hide_me", True)
    _reload_vis(vpage)
    vpage.evaluate(
        "() => window.ViewerNavigation.applyCommittedState(" + _SNAP + ","
        "{manifestVersion:1,"
        " spreads:[{id:'c0:s0', right:{folder:'hide_me',name:'1.png'},"
        "  left:{folder:'hide_me',name:'2.png'}}]},"
        "{role:'follower'})")
    vpage.wait_for_function("location.hash === ''")
    assert vpage.evaluate("() => window.currentFolder || null") is None
    assert vpage.evaluate(
        "() => !!(window.PairedView && window.PairedView.state"
        " && window.PairedView.state().active)") is False


def test_apply_committed_unresolvable_manifest_not_blocked(vpage, vis_env):
    """folder を解決できない commit（legacy/test 形状）は拒否しない —
    現在の表示を維持する。"""
    vis_put(vis_env, "hide_me", True)
    _reload_vis(vpage)
    vpage.click("#tile-grid .tile[data-path='keep_a']")
    vpage.wait_for_function("() => window.currentFolder === 'keep_a'")
    vpage.evaluate(
        "() => window.ViewerNavigation.applyCommittedState("
        "{v:1, sessionId:null, revision:9, workEpoch:0, manifestVersion:1,"
        " phase:'reading', spreadId:'c0:s7', offset:0, leaderSide:'right',"
        " presence:{}},"
        "{manifestVersion:1}, {})")   # no folder, no spread table
    vpage.wait_for_timeout(300)
    assert vpage.evaluate("() => window.currentFolder") == "keep_a"
    assert "keep_a" in vpage.evaluate("() => location.hash")


# ── UI_SYNC remote-path denial (mock level) ──────────────────────────────

def test_ui_sync_hidden_path_denied_no_pending(vpage, vis_env):
    """hidden remote path: leave + top、_pendingMsg に残らない、再 arm でも
    復活しない。"""
    vis_put(vis_env, "hide_series", True)
    _reload_vis(vpage)
    assert _arm_mirror(vpage, "follower") is True
    vpage.evaluate(
        "() => window.__pairBrowseSync.applyRemote("
        "{view:'browse', path:'hide_series', top:0, viewH:500})")
    vpage.wait_for_function("location.hash === ''")
    assert vpage.evaluate("() => window.__leaveCalls") == 1
    assert vpage.evaluate("() => window.__pairBrowseSync.pending") is None
    assert vpage.evaluate("() => window.currentBrowsePath || null") is None
    assert vpage.locator("#sidebar.open").count() == 1
    # 再 arm しても拒否した path は replay されない
    vpage.evaluate("() => window.__disarm()")
    vpage.evaluate("() => window.__arm()")
    vpage.wait_for_timeout(400)
    assert vpage.evaluate("() => window.currentBrowsePath || null") is None


def test_ui_sync_hidden_path_denied_while_disarmed(vpage, vis_env):
    """not-yet-armed race window: a hidden frame is denied, never stashed —
    the arm-edge replay cannot resurrect it either."""
    vis_put(vis_env, "hide_series", True)
    _reload_vis(vpage)
    assert _arm_mirror(vpage, "follower") is True
    vpage.evaluate("() => window.__disarm()")
    vpage.evaluate(
        "() => window.__pairBrowseSync.applyRemote("
        "{view:'browse', path:'hide_series', top:0, viewH:500})")
    vpage.wait_for_function("location.hash === ''")
    assert vpage.evaluate("() => window.__leaveCalls") == 1
    assert vpage.evaluate("() => window.__pairBrowseSync.pending") is None
    vpage.evaluate("() => window.__arm()")
    vpage.wait_for_timeout(400)
    assert vpage.evaluate("() => window.currentBrowsePath || null") is None


def test_ui_sync_visible_path_still_applies(vpage, vis_env):
    """Guard is inert for visible paths — the mirror still navigates and no
    leave fires."""
    _arm_mirror(vpage, "follower")
    vpage.evaluate(
        "() => window.__pairBrowseSync.applyRemote("
        "{view:'browse', path:'hide_series', top:0, viewH:500})")
    vpage.wait_for_function(
        "() => window.currentBrowsePath === 'hide_series'", timeout=5000)
    assert vpage.evaluate("() => window.__leaveCalls") == 0


# ── _boot readiness gate (real promise, no stubbing) ────────────────────

def _wait_held(held, timeout=10.0):
    """Poll until the route handler captured at least one request."""
    import time
    deadline = time.time() + timeout
    while not held and time.time() < deadline:
        time.sleep(0.05)
    return held


def test_boot_resume_waits_for_visibility_ready(request, vis_env):
    """Stored pair creds must not open a socket before the settings judgment:
    _boot defers resume() on the REAL FolderVisibility.ready() — the
    module-scope promise that resolves only with the first GET/PUT judgment
    (a pre-init ready() would previously have resolved immediately)."""
    b = request_browser(request)
    ctx = b.new_context(viewport={"width": 900, "height": 600},
                      has_touch=True)
    ctx.add_init_script(INSTRUMENT)
    pg = ctx.new_page()
    try:
        pg.goto(vis_env)
        pg.wait_for_selector(".tile")
        pg.evaluate(
            "sessionStorage.setItem('bookq.readerSync.v1', JSON.stringify("
            "{sessionId:'S', memberId:'M', resumeToken:'T',"
            " role:'leader'}))")
        held = []
        pg.route("**/api/v1/folder-visibility", lambda r: held.append(r))
        pg.reload()
        pg.wait_for_function("() => document.readyState !== 'loading'")
        assert _wait_held(held), "visibility GET was never issued"
        # The held GET races the client's own 3s fetch abort (GET_TIMEOUT_MS):
        # an abort resolves ready() as 'error' and _boot resumes anyway —
        # the old fixed-sleep+roundtrips assert window could outlast it.
        # Snapshot the judgment and both socket probes in ONE evaluate so the
        # window is ~ms; a buggy immediate resume creates its WebSocket at
        # script-eval time (route capture already proves it would exist).
        snap = pg.evaluate(
            "() => ({rs: window.FolderVisibility &&"
            " window.FolderVisibility.readyState,"
            " conns: window.__wsConns.length,"
            " log: window.__wsLog.length})")
        assert snap["rs"] == "loading", \
            f"visibility abort raced the assert window: {snap}"
        # 判断前: _boot の resume は待機 — ソケットはまだ存在しない
        assert snap["conns"] == 0 and snap["log"] == 0
        # 判断が着いてから resume() → RESUME が wire に出る
        # (wsはconstructor時点で __wsConns に載るが RESUME は onopen 後 —
        #  socket存在ではなく frame 自体を待つ)
        for r in list(held):
            r.fallback()
        pg.wait_for_function(
            "() => window.__wsLog.some(f => f && f.type === 'RESUME')")
        assert pg.evaluate("() => window.__wsConns.length") == 1
    finally:
        ctx.close()


def test_pending_commit_latest_stashed_and_retried(vpage, vis_env):
    """fix2: commits landing while visibility is undecided are held
    (newest only — last write wins) and re-evaluated by retryPendingCommit
    on the ready edge. A hidden newest commit denies (leave + top); an
    older visible one never reaches the display."""
    vis_put(vis_env, "hide_me", True)
    held = []
    vpage.route("**/api/v1/folder-visibility", lambda r: held.append(r))
    # The held GET cannot keep FV 'loading' past its own 3s client-side
    # fetch timeout — the abort then resolves the judgment as 'error'/
    # 'readonly' and the stash window closes mid-test (observed flake).
    # Kill the boot-embedded snapshot AND the cached one so hasSnapshot
    # stays false: `_applyCommitted`'s "判定不能" gate then holds at ANY
    # timing, exactly like a real client whose settings fetch failed with
    # nothing cached.
    vpage.context.add_init_script(
        "Object.defineProperty(window, '__BOOKQ_VISIBILITY_SNAPSHOT__',"
        " {configurable: true, get: function(){ return null; }});"
        "try { localStorage.clear(); } catch(e) {}")
    vpage.reload()
    vpage.wait_for_function(
        "() => window.FolderVisibility"
        " && window.FolderVisibility.readyState === 'loading'"
        " && window.__pairCtlTest")
    # leave/PairedView spies — denial is observable without a socket.
    vpage.evaluate("""() => {
      window.__leaveCalls = 0;
      const origLeave = window.PairController.leave.bind(
        window.PairController);
      window.PairController.leave = () => {
        window.__leaveCalls++;
        return origLeave();
      };
      window.__pvCalls = 0;
      const origApply = window.PairedView.apply.bind(window.PairedView);
      window.PairedView.apply = (...a) => { window.__pvCalls++;
                                          return origApply(...a); };
    }""")
    assert _wait_held(held), "visibility GET was never issued"
    snap = "{v:1, sessionId:'s', revision:1, manifestVersion:1," \
           " phase:'reading', spreadId:'c0:s0', presence:{}}"
    # Older commit (visible work) is REPLACED by the newer hidden one.
    vpage.evaluate(
        "() => window.__pairCtlTest.applyCommitted(" + snap + ","
        "{folder:'keep_a', manifestVersion:1}, {role:'follower'})")
    assert vpage.evaluate(
        "() => window.__pairCtlTest.pendingCommit"
        " && window.__pairCtlTest.pendingCommit.manifest.folder") == "keep_a"
    vpage.evaluate(
        "() => window.__pairCtlTest.applyCommitted(" + snap + ","
        "{folder:'hide_me', manifestVersion:1}, {role:'follower'})")
    assert vpage.evaluate(
        "() => window.__pairCtlTest.pendingCommit"
        " && window.__pairCtlTest.pendingCommit.manifest.folder") == "hide_me"
    # Nothing applied or denied while the judgment is pending.
    assert vpage.evaluate("() => window.__pvCalls") == 0
    assert vpage.evaluate("() => window.__leaveCalls") == 0
    # Judgment lands: onChange → retryPendingCommit → deny the newest.
    for r in list(held):
        r.fallback()
    vpage.wait_for_function(
        "window.FolderVisibility.readyState === 'ready'")
    vpage.wait_for_function("() => window.__leaveCalls >= 1")
    vpage.wait_for_function("location.hash === ''")
    assert vpage.evaluate(
        "() => window.__pairCtlTest.pendingCommit") is None
    assert vpage.evaluate("() => window.__pvCalls") == 0
    assert vpage.evaluate("() => window.currentFolder || null") is None


def test_exit_paired_to_solo_rechecks_hidden_solo_work(vis_env, request):
    """fix6: leaving a pair must re-check the saved solo folder — when it
    was hidden by another device while paired, the solo restore is denied
    and the user lands on the top list instead."""
    b = request_browser(request)
    ctx_l, pl = _dev_page(b, vis_env)
    ctx_f, pf = _dev_page(b, vis_env)
    try:
        _open(pf, "keep_a")              # follower's own solo book
        _open(pl, "hide_series/vol1")    # leader's visible work
        _pair_now(pl, pf)
        for p in (pl, pf):
            assert _pv(p)["active"] is True
        assert pf.evaluate("() => window.currentFolder") \
            == "hide_series/vol1"
        # Another device hides the follower's solo work while paired —
        # the paired view itself stays allowed.
        vis_put(vis_env, "keep_a", True)
        pf.evaluate("() => window.FolderVisibility.refresh()")
        pf.wait_for_function(
            "() => window.FolderVisibility.isHidden('keep_a')")
        assert pf.evaluate("() => window.currentFolder") \
            == "hide_series/vol1"       # paired view untouched
        # Leave → exitPairedToSolo re-checks the stashed solo folder → deny.
        pf.evaluate("() => window.PairController.leave()")
        pf.wait_for_function("() => location.hash === ''")
        assert pf.evaluate("() => window.currentFolder || null") is None
        assert pf.evaluate(
            "() => Array.from(document.querySelectorAll('#img-track img'))"
            ".some(i => (i.src || '').includes('keep_a'))") is False
        assert pf.evaluate(
            "() => window.PairedView.state().active") is False
        pl.wait_for_function(
            "() => window.PairController.state.status === 'ended'",
            timeout=15000)
    finally:
        ctx_l.close()
        ctx_f.close()


# ── solo stays inert ─────────────────────────────────────────────────────

def test_solo_mode_zero_sockets_and_guards_inert(request, vis_env):
    """No pair → no socket; the visibility guards change nothing solo."""
    b = request_browser(request)
    ctx, pg = _dev_page(b, vis_env)
    try:
        _open(pg, "keep_a")
        pg.keyboard.press("ArrowLeft")   # next spread → 2
        pg.wait_for_function("() => window.currentIdx === 2")
        assert pg.evaluate("() => window.__wsConns.length") == 0
        assert pg.evaluate("() => window.__wsLog.length") == 0
        assert pg.evaluate(
            "() => window.PairController.state.status") == "idle"
    finally:
        ctx.close()


# ── real /ws/reader-sync: hidden commit denial ────────────────────────────

def test_pair_commit_for_hidden_work_denied_on_follower(vis_env, request):
    """§11-D: 全表示中の leader が非表示作品を共有しても、通常モードの
    follower は commit を apply せず LEAVE→トップへ。保持経路で復活しない。"""
    vis_put(vis_env, "hide_me", True)
    b = request_browser(request)
    ctx_l, pl = _dev_page(b, vis_env)
    ctx_f, pf = _dev_page(b, vis_env)
    try:
        # leader は 全表示中 — 自身では hidden work を正当に開ける。
        pl.evaluate("() => window.FolderVisibility.setShowAll(true)")
        pl.wait_for_selector("#tile-grid .tile[data-path='hide_me']")
        _open(pl, "hide_me")
        pl.evaluate("() => window.PairController.create()")
        pl.wait_for_function("() => !!window.PairController.state.code")
        code = pl.evaluate("() => window.PairController.state.code")
        pf.evaluate("(c) => window.PairController.join(c)", code)
        # follower: hide_me commit → 拒否 → leave → top
        pf.wait_for_function(
            "() => window.PairController.state.status === 'ended'",
            timeout=15000)
        pf.wait_for_function("() => location.hash === ''")
        assert pf.evaluate("() => window.currentFolder || null") is None
        assert pf.evaluate(
            "() => !!(window.PairedView && window.PairedView.state"
            " && window.PairedView.state().active)") is False
        assert pf.evaluate("() => window.__pairBrowseSync.pending") is None
        # settle — held/_lastApply replay must not resurrect the hidden work
        pf.wait_for_timeout(600)
        assert pf.evaluate("() => window.currentFolder || null") is None
        assert pf.evaluate("() => location.hash") == ""
        assert pf.evaluate(
            "() => Array.from(document.querySelectorAll('#img-track img'))"
            ".some(i => (i.src || '').includes('hide_me'))") is False
        assert "hide_me" not in pf.evaluate(
            "() => document.getElementById('folder-name').textContent")
        # leader side: peer left → session ends too
        pl.wait_for_function(
            "() => window.PairController.state.status === 'ended'",
            timeout=15000)
    finally:
        ctx_l.close()
        ctx_f.close()


def test_leader_open_work_hidden_never_sent(vis_env, request):
    """通常モードの leader が非表示作品を pick: OPEN_WORK は wire に出ず、
    _sentLog にも残らず、利用者はトップへ (solo pick と同じ拒否)。"""
    vis_put(vis_env, "hide_me", True)
    b = request_browser(request)
    ctx_l, pl = _dev_page(b, vis_env)
    ctx_f, pf = _dev_page(b, vis_env)
    try:
        _open(pl, "keep_a")
        _pair_now(pl, pf)
        assert _pv(pl)["active"] is True and _pv(pf)["active"] is True
        # 送信側 guard: hide_me pick は即座に拒否
        pl.evaluate(
            "() => window.ViewerNavigation.request("
            "{type:'openWork', folder:'hide_me', via:'tile'})")
        for p in (pl, pf):
            p.wait_for_function(
                "() => window.PairController.state.status === 'ended'",
                timeout=15000)
        pl.wait_for_function("() => location.hash === ''")
        assert pl.evaluate("() => window.currentFolder || null") is None
        assert [f for f in _sent(pl, {"OPEN_WORK"})
                if (f.get("payload") or {}).get("folder") == "hide_me"] == []
        assert [e for e in _intents(pl, {"openWork"})
                if e.get("folder") == "hide_me"] == []
        # the keep_a bootstrap did send exactly one OPEN_WORK (baseline)
        assert len([f for f in _sent(pl, {"OPEN_WORK"})
                    if (f.get("payload") or {}).get("folder") == "keep_a"]) == 1
        assert pf.evaluate(
            "() => window.PairedView.state().active") is False
        assert "hide_me" not in pf.evaluate("() => location.hash")
    finally:
        ctx_l.close()
        ctx_f.close()


# ── real /ws/reader-sync: UI_SYNC hidden browse path ──────────────────────

def test_ui_sync_hidden_browse_path_redirects_over_wire(vis_env, request):
    """全表示の leader が非表示 root へ潜った UI_SYNC を受けた通常モードの
    follower は mirror せず LEAVE→トップへ。pending にも残らない。"""
    vis_put(vis_env, "hide_series", True)
    b = request_browser(request)
    ctx_l, pl = _dev_page(b, vis_env)
    ctx_f, pf = _dev_page(b, vis_env)
    try:
        _open(pl, "keep_a")
        _pair_now(pl, pf)
        pl.evaluate("() => window.FolderVisibility.setShowAll(true)")
        _start_browse(pl, pf)
        # leader drills into the hidden root — its own nav is allowed (全表示)
        pl.eval_on_selector(
            "#tile-grid .tile[data-path='hide_series']", "el => el.click()")
        pl.wait_for_function(
            "() => window.currentBrowsePath === 'hide_series'",
            timeout=10000)
        # follower: UI_SYNC{path:'hide_series'} → deny → leave + top
        pf.wait_for_function(
            "() => window.PairController.state.status === 'ended'",
            timeout=15000)
        pf.wait_for_function("() => location.hash === ''")
        assert pf.evaluate("() => window.currentBrowsePath || null") is None
        assert pf.evaluate("() => window.__pairBrowseSync.pending") is None
        pf.wait_for_timeout(400)
        assert pf.evaluate(
            "() => window.PairedView.state().active") is False
        # peer's leave ends the leader's session as well
        pl.wait_for_function(
            "() => window.PairController.state.status === 'ended'",
            timeout=15000)
    finally:
        ctx_l.close()
        ctx_f.close()


# ── real /ws/reader-sync: reload rejoin with hidden work ──────────────────

def test_reload_pair_creds_hidden_work_denied(vis_env, request):
    """ペア中リロード: creds 再join は ready() 待ち — 届いた snapshot で
    共有作品が非表示なら commit は deny → トップへ。creds も消える。"""
    b = request_browser(request)
    ctx_l, pl = _dev_page(b, vis_env)
    ctx_f, pf = _dev_page(b, vis_env)
    try:
        _open(pl, "keep_a")
        _pair_now(pl, pf)
        # 「別端末」が共有作品を非表示化 — follower の次回起動判断に入る
        vis_put(vis_env, "keep_a", True)
        pf.reload()
        pf.wait_for_selector(".tile")
        _wait_vis_ready(pf)
        pf.wait_for_function(
            "() => window.PairController.state.status === 'ended'",
            timeout=20000)
        pf.wait_for_function("() => location.hash === ''")
        assert pf.evaluate("() => window.currentFolder || null") is None
        assert pf.evaluate(
            "() => window.PairedView.state().active") is False
        pf.wait_for_timeout(500)
        assert pf.evaluate(
            "() => Array.from(document.querySelectorAll('#img-track img'))"
            ".some(i => (i.src || '').includes('keep_a'))") is False
        # leave cleared the creds — no second rejoin attempt resurrects it
        assert pf.evaluate(
            "() => sessionStorage.getItem('bookq.readerSync.v1')") is None
        pl.wait_for_function(
            "() => window.PairController.state.status === 'ended'",
            timeout=15000)
    finally:
        ctx_l.close()
        ctx_f.close()


# ── real /ws/reader-sync: visible flow regression ─────────────────────────

def test_visible_pair_flow_unchanged(vis_env, request):
    """可視作品のペア流は不変 — pair・turn とも 1操作1request、follower に
    remote echo は出ない。"""
    b = request_browser(request)
    ctx_l, pl = _dev_page(b, vis_env)
    ctx_f, pf = _dev_page(b, vis_env)
    try:
        _open(pl, "keep_a")            # 4 pages → spreads c0:s0 / c0:s1
        _pair_now(pl, pf)
        for p in (pl, pf):
            assert _pv(p)["active"] is True
            assert _pv(p)["spreadId"] == "c0:s0"
        assert len(_sent(pl, {"OPEN_WORK"})) == 1   # bootstrap only
        n0 = len(_intents(pl, {"turn"}))
        t0 = len(_sent(pl, {"TURN"}))
        pl.keyboard.press("ArrowLeft")   # next spread
        pl.wait_for_function(
            "() => window.PairedView.state().spreadId === 'c0:s1'")
        sid, _ = _synced(pl, pf)
        assert len(_intents(pl, {"turn"})) == n0 + 1
        assert len(_sent(pl, {"TURN"})) == t0 + 1
        assert _sent(pf, {"TURN"}) == []       # follower never echoes
        assert _pv(pf)["spreadId"] == sid
    finally:
        ctx_l.close()
        ctx_f.close()
