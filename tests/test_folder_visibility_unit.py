"""Unit-ish tests of browse/visibility.js in isolation (Playwright blank page).

Loads the real offline-core.js + visibility.js, stubs fetch/localStorage, and
asserts the module contract: root normalization, snapshot apply/monotonicity,
owner-scoped cache, single-flight refresh, write path, offline/no-persist.
No server needed — the shared `server` fixture is never touched.
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))
from folder_visibility_helpers import request_browser  # noqa: E402

CORE_SRC = (REPO_ROOT / "app" / "static" / "offline-core.js").read_text(encoding="utf-8")
ENDPOINTS_SRC = (REPO_ROOT / "app" / "static" / "api" / "endpoints.js").read_text(encoding="utf-8")
API_SRC = (REPO_ROOT / "app" / "static" / "api" / "v1.js").read_text(encoding="utf-8")
VIS_SRC = (REPO_ROOT / "app" / "static" / "browse" / "visibility.js").read_text(encoding="utf-8")

_STUB_HTML = """<!DOCTYPE html><html><body>
<div id="tile-grid"></div>
<span id="visibility-status"></span>
</body></html>"""

# Fake http origin — localStorage is denied on about:blank/data: URLs, so the
# stub document is served by route interception; no app server is needed.
_STUB_ORIGIN = "http://vis-stub.test/"


@pytest.fixture
def vpage(request):
    b = request_browser(request)
    context = b.new_context()
    yield context.new_page()
    context.close()


def _json_response(expr: str, status: int = 200) -> str:
    return (
        "(u,o)=>{window.__calls.push({u:String(u),o:o||{}});"
        f"return Promise.resolve(new Response(JSON.stringify({expr}),"
        f"{{status:{status},headers:{{'Content-Type':'application/json'}}}}))}}"
    )


def _boot(page, fetch_impl: str, storage_seed: dict | None = None):
    page.route(
        "**/*",
        lambda route: route.fulfill(
            status=200, content_type="text/html", body=_STUB_HTML
        ),
    )
    page.goto(_STUB_ORIGIN)
    if storage_seed:
        for k, v in storage_seed.items():
            page.evaluate(f"localStorage.setItem({json.dumps(k)}, {json.dumps(v)})")
    # Wrapped in an IIFE: a bare assignment's completion value is the arrow
    # function itself, which Playwright evaluate would invoke as a callback.
    page.evaluate("(()=>{window.__calls=[]; window.fetch=" + fetch_impl + ";})()")
    page.evaluate(CORE_SRC)
    # #236: visibility.js goes through the typed window.BookqApi client —
    # load the real endpoints + v1.js so the stubbed fetch below is the
    # only seam (calls still record {u,o} on window.__calls).
    page.evaluate(ENDPOINTS_SRC)
    page.evaluate(API_SRC)
    page.evaluate(VIS_SRC)


def _init(page, owner="ownA"):
    page.evaluate(
        "window.FolderVisibility.init({owner: "
        + json.dumps(owner)
        + ", onChange: ()=>{window.__changes=(window.__changes||0)+1;}})"
    )
    page.wait_for_function("window.FolderVisibility.readyState !== 'loading'")


def test_root_for_path_normalization(vpage):
    _boot(vpage, _json_response("{}"))
    out = vpage.evaluate(
        """(() => {
          const FV = window.FolderVisibility;
          return [
            FV.rootForPath('a/b/c'), FV.rootForPath('a/../b'), FV.rootForPath('./a'),
            FV.rootForPath(''), FV.rootForPath('..'), FV.rootForPath('../a'),
            FV.rootForPath('a/./b'), FV.rootForPath('a//b'),
            FV.rootForPath('visible/../hidden'), FV.rootForPath(null),
            FV.rootForPath('a/../../x'), FV.rootForPath('wrap/inner/vol1')
          ];
        })()"""
    )
    assert out == [
        "a", "b", "a",
        None, None, None,
        "a", "a",
        "hidden", None,
        None, "wrap",
    ]


def test_top_level_set_tracks_flattened_node_names(vpage):
    """_topLevel holds root-level node _fullPaths — flattened names keep
    their '/' and only EXACT membership counts (never first segments)."""
    _boot(vpage, _json_response("{owner:'ownA',revision:1,hiddenRoots:[]}"))
    _init(vpage)
    vpage.evaluate(
        "window.FolderVisibility.setTopLevelPaths(['wrap/wA','hide_me','arc.zip'])"
    )
    out = vpage.evaluate(
        "(()=>{const FV=window.FolderVisibility;return ["
        "FV.isTopLevel('wrap/wA'),FV.isTopLevel('wrap'),FV.isTopLevel('wA'),"
        "FV.isTopLevel('hide_me'),FV.isTopLevel('hide_me/sub'),"
        "FV.isTopLevel('arc.zip'),FV.isTopLevel(''),FV.isTopLevel(null)]})()"
    )
    assert out == [True, False, False, True, False, True, False, False]
    # Re-index replaces the set wholesale (stale top-levels drop out).
    vpage.evaluate("window.FolderVisibility.setTopLevelPaths(['only'])")
    assert vpage.evaluate("window.FolderVisibility.isTopLevel('wrap/wA')") is False
    assert vpage.evaluate("window.FolderVisibility.isTopLevel('only')") is True


def test_hidden_set_apply_and_downrev_converges_once(vpage):
    """srvRev < _revision (server reset OR a stale pre-PUT read): the server
    snapshot is adopted verbatim — the server is truth — and exactly ONE
    convergence GET is scheduled to self-correct a stale read."""
    _boot(vpage, _json_response(
        "{owner:'ownA',revision:3,hiddenRoots:['hide_me','hide_series']}"
    ))
    _init(vpage)
    assert vpage.evaluate("window.FolderVisibility.isHidden('hide_me/x')") is True
    assert vpage.evaluate("window.FolderVisibility.isVisible('hide_series/vol1')") is False
    assert vpage.evaluate("window.FolderVisibility.isVisible('keep_a')") is True
    # Drain one-shot ambient refresh triggers (first-paint visibilitychange /
    # focus → a coalesced re-GET) before measuring call counts.
    vpage.wait_for_timeout(700)
    n0 = vpage.evaluate("window.__calls.length")
    vpage.evaluate(
        "(()=>{window.fetch=(u,o)=>{window.__calls.push({u:String(u),o:o||{}});"
        "return Promise.resolve(new Response(JSON.stringify("
        "{owner:'ownA',revision:1,hiddenRoots:[]}),{status:200}))};})()"
    )
    vpage.evaluate("window.FolderVisibility.refresh()")
    # The refresh plus its one scheduled convergence both land — wait for
    # the convergence RESPONSE (flight end), not just its issue: a refresh()
    # overlapping the in-flight convergence would queue a follow-up and
    # skew the count.
    vpage.wait_for_function(
        "window.__calls.length >= %d"
        " && !window.FolderVisibility._debug().refreshInFlight" % (n0 + 2))
    snap = vpage.evaluate("window.FolderVisibility.snapshot()")
    assert snap["revision"] == 1
    assert snap["hiddenRoots"] == []
    # Bounded: an identical refresh is adopted but does NOT re-arm the
    # convergence (the downrev self-correction is one GET per adoption).
    n2 = vpage.evaluate("window.__calls.length")
    vpage.evaluate("window.FolderVisibility.refresh()")
    vpage.wait_for_timeout(600)
    assert vpage.evaluate("window.__calls.length") == n2 + 1


def test_hidden_root_for_longest_match(vpage):
    """hiddenRootFor: full normalized-path prefix match on hidden keys;
    longest key wins; escape/empty → null."""
    _boot(vpage, _json_response(
        "{owner:'ownA',revision:1,hiddenRoots:['wrap/wA','a']}"
    ))
    _init(vpage)
    out = vpage.evaluate(
        """(() => {const FV = window.FolderVisibility; return [
          FV.hiddenRootFor('wrap/wA'), FV.hiddenRootFor('wrap/wA/p1.png'),
          FV.hiddenRootFor('wrap/wB'), FV.hiddenRootFor('a/b'),
          FV.hiddenRootFor('x/../a/b'), FV.hiddenRootFor('ab'),
          FV.hiddenRootFor(''), FV.hiddenRootFor('..'),
          FV.isHidden('wrap/wA/sub'), FV.isHidden('wrap'),
          FV.isVisible('wrap/wA'), FV.isVisible('wrap/wB')
        ];})()"""
    )
    assert out == [
        "wrap/wA", "wrap/wA",
        None, "a",
        "a", None,
        None, None,
        True, False,
        False, True,
    ]


def test_guard_path_states(vpage):
    _boot(vpage, _json_response(
        "{owner:'ownA',revision:1,hiddenRoots:['hide_me']}"
    ))
    _init(vpage)
    out = vpage.evaluate(
        """(() => {const FV = window.FolderVisibility; return [
          FV.guardPath('hide_me'), FV.guardPath('hide_me/x'),
          FV.guardPath('keep_a'), FV.guardPath('')];})()"""
    )
    assert out == ["hidden", "hidden", "allow", "allow"]
    vpage.evaluate("window.FolderVisibility.setShowAll(true)")
    assert vpage.evaluate("window.FolderVisibility.guardPath('hide_me')") == "allow"


def test_guard_path_pending_without_snapshot(vpage):
    """error with no snapshot at all: never 'hidden' — deny paths queue."""
    _boot(vpage, "(u,o)=>{window.__calls.push({u:String(u),o:o||{}});"
                 "return Promise.reject(new TypeError('down'))}")
    _init(vpage)
    vpage.wait_for_function("window.FolderVisibility.readyState === 'error'")
    assert vpage.evaluate("window.FolderVisibility.guardPath('hide_me')") == "pending"
    assert vpage.evaluate("window.FolderVisibility.isHidden('hide_me')") is False
    assert vpage.evaluate("window.FolderVisibility.isVisible('keep_a')") is False


def test_blocked_grid_html_states(vpage):
    _boot(vpage, "(u,o)=>{window.__calls.push({u:String(u),o:o||{}});"
                 "return Promise.reject(new TypeError('down'))}")
    # Loading, no snapshot → 読込中 placeholder (never an unfiltered grid).
    _init(vpage)
    vpage.wait_for_function("window.FolderVisibility.readyState === 'error'")
    html = vpage.evaluate("window.FolderVisibility.blockedGridHtml()")
    assert "取得失敗" in html and "fv-grid-retry" in html


def test_sig_delimiter_collision_still_fires_onchange(vpage):
    """['a,b','c'] vs ['a','b,c'] — a delimiter join must not collide them
    into the same signature (a silent no-change would skip onChange)."""
    _boot(vpage, _json_response("{owner:'ownA',revision:1,hiddenRoots:['a,b','c']}"))
    _init(vpage)
    n0 = vpage.evaluate("window.__changes || 0")
    vpage.evaluate(
        "(()=>{window.fetch=(u,o)=>{window.__calls.push({u:String(u),o:o||{}});"
        "return Promise.resolve(new Response(JSON.stringify("
        "{owner:'ownA',revision:2,hiddenRoots:['a','b,c']}),{status:200}))};})()"
    )
    vpage.evaluate("window.FolderVisibility.refresh()")
    vpage.wait_for_function("(window.__changes||0) > %d" % n0)
    snap = vpage.evaluate("window.FolderVisibility.snapshot()")
    assert sorted(snap["hiddenRoots"]) == ["a", "b,c"]


def test_put_response_never_rolled_back_by_get(vpage):
    """A GET issued before an in-flight PUT resolves after it: the older
    request's response is skipped, so the PUT snapshot stays verbatim."""
    _boot(
        vpage,
        "(u,o)=>{window.__calls.push({u:String(u),o:o||{}});"
        "if(o&&o.method==='PUT'){"
        "return Promise.resolve(new Response(JSON.stringify("
        "{owner:'ownA',revision:2,hiddenRoots:['hide_me']}),"
        "{status:200,headers:{'Content-Type':'application/json'}}));}"
        "const gets=window.__calls.filter(c=>!(c.o&&c.o.method==='PUT')).length;"
        "if(gets<=1){return Promise.resolve(new Response(JSON.stringify("
        "{owner:'ownA',revision:1,hiddenRoots:[]}),{status:200,"
        "headers:{'Content-Type':'application/json'}}));}"
        "return new Promise(res=>{(window.__held=window.__held||[]).push(res)});}",
    )
    _init(vpage)
    n0 = vpage.evaluate("window.__calls.length")
    # Both promises are held — evaluate must not await them (fire-and-forget).
    vpage.evaluate("(()=>{window.FolderVisibility.refresh();})()")          # GET#2 held
    res = vpage.evaluate("window.FolderVisibility.setHidden('hide_me', true)")
    assert res["ok"] is True
    # The in-flight GET resolves with pre-PUT data → skipped entirely.
    vpage.evaluate(
        "(window.__held||[]).forEach(r=>r(new Response(JSON.stringify("
        "{owner:'ownA',revision:1,hiddenRoots:[]}),{status:200,"
        "headers:{'Content-Type':'application/json'}})))"
    )
    vpage.wait_for_timeout(100)
    snap = vpage.evaluate("window.FolderVisibility.snapshot()")
    assert snap["revision"] == 2
    assert snap["hiddenRoots"] == ["hide_me"]
    # No extra GETs beyond the held one (skip ≠ convergence).
    assert vpage.evaluate("window.__calls.length") == n0 + 2


def test_stale_get_landing_after_put_converges_once(vpage):
    """A GET issued DURING a PUT flight that resolves stale afterwards is
    adopted (server truth), then self-corrected by exactly one
    convergence refresh — final state is the PUT's revision."""
    _boot(
        vpage,
        "(u,o)=>{window.__calls.push({u:String(u),o:o||{}});"
        "if(o&&o.method==='PUT'){"
        "return new Promise(res=>{(window.__heldPut=window.__heldPut||[]).push(res)});}"
        "const gets=window.__calls.filter(c=>!(c.o&&c.o.method==='PUT')).length;"
        "if(gets<=1){return Promise.resolve(new Response(JSON.stringify("
        "{owner:'ownA',revision:1,hiddenRoots:[]}),{status:200,"
        "headers:{'Content-Type':'application/json'}}));}"
        "return new Promise(res=>{(window.__heldGet=window.__heldGet||[]).push(res)});}",
    )
    _init(vpage)
    vpage.evaluate("(()=>{window.FolderVisibility.setHidden('hide_me', true);})()")  # PUT held
    vpage.evaluate("(()=>{window.FolderVisibility.refresh();})()")                  # GET held
    # PUT lands first with the committed snapshot.
    vpage.evaluate(
        "(window.__heldPut||[]).splice(0).forEach(r=>r(new Response(JSON.stringify("
        "{owner:'ownA',revision:2,hiddenRoots:['hide_me']}),{status:200,"
        "headers:{'Content-Type':'application/json'}})))"
    )
    # The newer-issued GET then resolves STALE (read before the commit):
    # adopted downrev → exactly one convergence GET is issued (splice drains
    # so the wait below only sees the NEW held request).
    vpage.evaluate(
        "(window.__heldGet||[]).splice(0).forEach(r=>r(new Response(JSON.stringify("
        "{owner:'ownA',revision:1,hiddenRoots:[]}),{status:200,"
        "headers:{'Content-Type':'application/json'}})))"
    )
    vpage.wait_for_function(
        "(window.__heldGet||[]).length >= 1", timeout=5000
    )
    vpage.evaluate(
        "(window.__heldGet||[]).splice(0).forEach(r=>r(new Response(JSON.stringify("
        "{owner:'ownA',revision:2,hiddenRoots:['hide_me']}),{status:200,"
        "headers:{'Content-Type':'application/json'}})))"
    )
    vpage.wait_for_function(
        "window.FolderVisibility.snapshot().revision === 2"
    )
    snap = vpage.evaluate("window.FolderVisibility.snapshot()")
    assert snap["revision"] == 2
    assert snap["hiddenRoots"] == ["hide_me"]


def test_ready_resolves_on_put_before_first_get(vpage):
    """ready() resolves on the first completed judgment from ANY source —
    a PUT landing before the initial GET counts."""
    _boot(
        vpage,
        "(u,o)=>{window.__calls.push({u:String(u),o:o||{}});"
        "if(o&&o.method==='PUT'){return Promise.resolve(new Response("
        "JSON.stringify({owner:'ownA',revision:5,hiddenRoots:['hide_me']}),"
        "{status:200,headers:{'Content-Type':'application/json'}}));}"
        "return new Promise(()=>{})}",   # GET never resolves
    )
    vpage.evaluate(
        "window.FolderVisibility.init({owner:'ownA',"
        "onChange:()=>{window.__changes=(window.__changes||0)+1;}})"
    )
    vpage.evaluate(
        "(()=>{window.__readyFlag=false;"
        "window.FolderVisibility.ready().then(()=>{window.__readyFlag=true;});})()"
    )
    res = vpage.evaluate("window.FolderVisibility.setHidden('hide_me', true)")
    assert res["ok"] is True
    vpage.wait_for_function("window.__readyFlag === true")
    assert vpage.evaluate("window.FolderVisibility.readyState") == "ready"


def test_set_hidden_accepts_multi_segment_root(vpage):
    """Flattened roots like wrap/wA pass client validation — membership in
    the top-level set is the server's check, not the shape's."""
    _boot(
        vpage,
        "(u,o)=>{window.__calls.push({u:String(u),o:o||{}});"
        "if(o&&o.method==='PUT')return Promise.resolve(new Response("
        "JSON.stringify({owner:'ownA',revision:2,hiddenRoots:['wrap/wA']}),"
        "{status:200,headers:{'Content-Type':'application/json'}}));"
        "return Promise.resolve(new Response(JSON.stringify("
        "{owner:'ownA',revision:1,hiddenRoots:[]}),{status:200}))}",
    )
    _init(vpage)
    res = vpage.evaluate("window.FolderVisibility.setHidden('wrap/wA', true)")
    assert res["ok"] is True
    puts = [c for c in vpage.evaluate("window.__calls") if c["o"].get("method") == "PUT"]
    assert len(puts) == 1
    assert json.loads(puts[0]["o"]["body"]) == {"root": "wrap/wA", "hidden": True}
    # Malformed multi-segment shapes still fail client-side without a PUT.
    for bad in ("a//b", "a/./b", "a/../b", "/a", "a/", ".h/x", "a/.b"):
        res = vpage.evaluate("window.FolderVisibility.setHidden(%s, true)" % json.dumps(bad))
        assert res["ok"] is False and res.get("invalid") is True, bad


def test_refresh_is_single_flight(vpage):
    _boot(vpage, _json_response("{owner:'ownA',revision:1,hiddenRoots:[]}"))
    _init(vpage)
    n0 = vpage.evaluate("window.__calls.length")
    same = vpage.evaluate(
        "(()=>{const a=window.FolderVisibility.refresh();"
        "const b=window.FolderVisibility.refresh();return a===b;})()"
    )
    assert same is True
    # two refresh() calls share the single in-flight fetch (call n0 + 1)
    assert vpage.evaluate("window.__calls.length") == n0 + 1


def test_set_hidden_commits_server_snapshot_verbatim(vpage):
    _boot(
        vpage,
        "(u,o)=>{window.__calls.push({u:String(u),o:o||{}});"
        "if(o&&o.method==='PUT')return Promise.resolve(new Response("
        "JSON.stringify({owner:'ownA',revision:4,hiddenRoots:['hide_me','extra']}),"
        "{status:200,headers:{'Content-Type':'application/json'}}));"
        "return Promise.resolve(new Response(JSON.stringify("
        "{owner:'ownA',revision:3,hiddenRoots:[]}),{status:200}))}",
    )
    _init(vpage)
    res = vpage.evaluate("window.FolderVisibility.setHidden('hide_me', true)")
    assert res["ok"] is True
    calls = vpage.evaluate("window.__calls")
    puts = [c for c in calls if c["o"].get("method") == "PUT"]
    assert len(puts) == 1
    assert puts[0]["o"]["headers"]["X-Bookq-Visibility-Owner"] == "ownA"
    assert json.loads(puts[0]["o"]["body"]) == {"root": "hide_me", "hidden": True}
    snap = vpage.evaluate("window.FolderVisibility.snapshot()")
    assert sorted(snap["hiddenRoots"]) == ["extra", "hide_me"]
    assert snap["revision"] == 4
    cache = json.loads(
        vpage.evaluate("localStorage.getItem('bookq.folderVisibility.v1.ownA')")
    )
    assert cache["revision"] == 4
    assert sorted(cache["hiddenRoots"]) == ["extra", "hide_me"]


def test_owner_mismatch_cache_never_applied(vpage):
    seed = {
        "bookq.folderVisibility.v1.ownB": json.dumps(
            {"v": 1, "owner": "ownB", "revision": 9, "hiddenRoots": ["x"]}
        )
    }
    _boot(
        vpage,
        _json_response("{owner:'ownA',revision:2,hiddenRoots:[]}"),
        storage_seed=seed,
    )
    _init(vpage, owner="ownA")
    assert vpage.evaluate("window.FolderVisibility.isHidden('x')") is False
    snap = vpage.evaluate("window.FolderVisibility.snapshot()")
    assert snap["revision"] == 2
    assert snap["owner"] == "ownA"
    assert json.loads(
        vpage.evaluate("localStorage.getItem('bookq.folderVisibility.v1.ownB')")
    )["revision"] == 9


def test_offline_set_hidden_rejected_no_queue(vpage):
    _boot(vpage, _json_response(
        "{owner:'ownA',revision:1,hiddenRoots:['hide_me']}"
    ))
    _init(vpage)
    vpage.evaluate("window.dispatchEvent(new Event('offline'))")
    res = vpage.evaluate("window.FolderVisibility.setHidden('keep_a', true)")
    assert res["ok"] is False
    assert res.get("offline") is True
    assert vpage.evaluate(
        "window.__calls.filter(c=>c.o&&c.o.method==='PUT').length"
    ) == 0


def test_offline_boot_uses_cached_snapshot_readonly(vpage):
    seed = {
        "bookq.folderVisibility.v1.ownA": json.dumps(
            {"v": 1, "owner": "ownA", "revision": 5, "hiddenRoots": ["hide_me"]}
        )
    }
    _boot(vpage, "(u,o)=>{window.__calls.push({u:String(u),o:o||{}});"
                 "return Promise.reject(new TypeError('offline'))}",
          storage_seed=seed)
    _init(vpage)
    assert vpage.evaluate("window.FolderVisibility.readyState") == "readonly"
    assert vpage.evaluate("window.FolderVisibility.isHidden('hide_me')") is True
    assert vpage.evaluate("window.FolderVisibility.isVisible('hide_me/x')") is False
    res = vpage.evaluate("window.FolderVisibility.setHidden('keep_a', true)")
    assert res["ok"] is False and res.get("offline") is True


def test_no_snapshot_error_blocks_grid_queues_nav_and_retries(vpage):
    """Error with no snapshot: the render gate stays closed (an unfiltered
    grid is never painted), but nothing is positively hidden — navigation
    guards return 'pending' (queue), never 'hidden' (deny). Retry recovers."""
    _boot(vpage, "(u,o)=>{window.__calls.push({u:String(u),o:o||{}});"
                 "return Promise.reject(new TypeError('down'))}")
    _init(vpage)
    vpage.wait_for_function("window.FolderVisibility.readyState === 'error'")
    # Render gate: no snapshot → not visible — but NOT positively hidden.
    assert vpage.evaluate("window.FolderVisibility.isVisible('keep_a')") is False
    assert vpage.evaluate("window.FolderVisibility.isHidden('keep_a')") is False
    assert vpage.evaluate("window.FolderVisibility.isHidden('hide_me/x')") is False
    assert vpage.evaluate("window.FolderVisibility.guardPath('x')") == "pending"
    assert vpage.evaluate("!!document.getElementById('visibility-retry')") is True
    # Retry converges to ready once the server answers.
    vpage.evaluate(
        "(()=>{window.fetch=(u,o)=>{window.__calls.push({u:String(u),o:o||{}});"
        "return Promise.resolve(new Response(JSON.stringify("
        "{owner:'ownA',revision:1,hiddenRoots:[]}),{status:200,"
        "headers:{'Content-Type':'application/json'}}))};})()"
    )
    vpage.click("#visibility-retry")
    vpage.wait_for_function("window.FolderVisibility.readyState === 'ready'")
    assert vpage.evaluate("window.FolderVisibility.isVisible('keep_a')") is True
    assert vpage.evaluate("window.FolderVisibility.guardPath('x')") == "allow"


def test_show_all_never_persisted(vpage):
    _boot(vpage, _json_response(
        "{owner:'ownA',revision:1,hiddenRoots:['hide_me']}"
    ))
    _init(vpage)
    keys0 = vpage.evaluate(
        "Object.keys(localStorage).concat(Object.keys(sessionStorage))"
    )
    vpage.evaluate("window.FolderVisibility.setShowAll(true)")
    vpage.wait_for_timeout(80)
    keys1 = vpage.evaluate(
        "Object.keys(localStorage).concat(Object.keys(sessionStorage))"
    )
    assert set(keys1) == set(keys0)  # mode never written to any storage
    assert vpage.evaluate("window.FolderVisibility.showAll") is True
    assert vpage.evaluate("window.FolderVisibility.isVisible('hide_me/x')") is True
    assert vpage.evaluate("location.hash") == ""


# ── review round 2 ─────────────────────────────────────────────────────


def test_ready_pends_until_first_judgment(vpage):
    """ready() taken BEFORE init() must still wait for the first GET/PUT
    judgment — pair _boot runs while visibility.js's module is evaluated
    but before app.js calls init()."""
    _boot(
        vpage,
        "(u,o)=>{window.__calls.push({u:String(u),o:o||{}});"
        "return new Promise(res=>{(window.__held=window.__held||[]).push(res)});}",
    )
    # A pre-init caller gets the module-scope promise — still pending.
    vpage.evaluate(
        "(()=>{window.__readyFlag=false;"
        "window.FolderVisibility.ready().then(()=>{window.__readyFlag=true;});})()"
    )
    vpage.evaluate(
        "window.FolderVisibility.init({owner:'ownA',"
        "onChange:()=>{window.__changes=(window.__changes||0)+1;}})"
    )
    vpage.wait_for_timeout(200)
    assert vpage.evaluate("window.__readyFlag") is False
    # init() must not have recreated the promise — the SAME wait resolves
    # on the first GET judgment.
    vpage.evaluate(
        "(window.__held||[]).forEach(r=>r(new Response(JSON.stringify("
        "{owner:'ownA',revision:1,hiddenRoots:[]}),{status:200,"
        "headers:{'Content-Type':'application/json'}})))"
    )
    vpage.wait_for_function("window.__readyFlag === true")


def test_embedded_snapshot_applies_and_owner_mismatch_ignored(vpage):
    """init(opts.snapshot): owner-matching embed filters before the GET
    resolves (and is written to the owner cache); a mismatched owner is
    ignored entirely."""
    _boot(
        vpage,
        "(u,o)=>{window.__calls.push({u:String(u),o:o||{}});"
        "return new Promise(()=>{})}",   # GET never resolves — embed only
    )
    vpage.evaluate(
        "window.FolderVisibility.init({owner:'ownA',"
        "snapshot:{owner:'ownA',revision:7,hiddenRoots:['hide_me']},"
        "onChange:()=>{window.__changes=(window.__changes||0)+1;}})"
    )
    vpage.wait_for_timeout(150)
    # Snapshot applied while still 'loading' — filtering is already live.
    assert vpage.evaluate("window.FolderVisibility.isHidden('hide_me')") is True
    assert vpage.evaluate("window.FolderVisibility.isVisible('hide_me/x')") is False
    assert vpage.evaluate("window.FolderVisibility.isVisible('keep_a')") is True
    assert vpage.evaluate("window.FolderVisibility.snapshot()")["revision"] == 7
    assert json.loads(
        vpage.evaluate("localStorage.getItem('bookq.folderVisibility.v1.ownA')")
    )["revision"] == 7
    # ready() still pends — embed is not a network judgment.
    vpage.evaluate(
        "(()=>{window.__readyFlag=false;"
        "window.FolderVisibility.ready().then(()=>{window.__readyFlag=true;});})()"
    )
    vpage.wait_for_timeout(150)
    assert vpage.evaluate("window.__readyFlag") is False


def test_embedded_snapshot_owner_mismatch_ignored(vpage):
    _boot(
        vpage,
        "(u,o)=>{window.__calls.push({u:String(u),o:o||{}});"
        "return new Promise(()=>{})}",
    )
    vpage.evaluate(
        "window.FolderVisibility.init({owner:'ownA',"
        "snapshot:{owner:'other',revision:9,hiddenRoots:['x']},"
        "onChange:()=>{}})"
    )
    vpage.wait_for_timeout(150)
    # No snapshot applied: fail-closed, nothing hidden, cache untouched.
    assert vpage.evaluate("window.FolderVisibility.hasSnapshot") is False
    assert vpage.evaluate("window.FolderVisibility.isHidden('x')") is False
    assert vpage.evaluate(
        "localStorage.getItem('bookq.folderVisibility.v1.other')"
    ) is None


def test_hidden_match_kinds_cover_and_ancestor(vpage):
    """hiddenMatch: self/descendant covering kinds deny and badge; the
    ancestor kind (path is a strict ancestor of a hidden key — the
    shallow window's raw dir name) is filtered from the grid but never
    denied and never editable."""
    _boot(vpage, _json_response(
        "{owner:'ownA',revision:1,hiddenRoots:['wrap/work','hide_me']}"
    ))
    _init(vpage)
    # 'wrap' is a real top-level in this scenario (shallow payload) — even
    # so, an ancestor match must not produce a PUT-404 toggle.
    vpage.evaluate("window.FolderVisibility.setTopLevelPaths(['wrap','wrap/work','hide_me'])")
    out = vpage.evaluate(
        """(() => {const FV = window.FolderVisibility; return {
          anc:    FV.hiddenMatch('wrap'),
          self:   FV.hiddenMatch('wrap/work'),
          desc:   FV.hiddenMatch('wrap/work/vol1'),
          other:  FV.hiddenMatch('keep_a'),
          ancVis: FV.isVisible('wrap'),
          ancDeny:FV.isHidden('wrap'),
          ancGuard: FV.guardPath('wrap'),
          covGuard: FV.guardPath('wrap/work/x'),
        };})()"""
    )
    assert out["anc"] == {"key": "wrap/work", "kind": "ancestor"}
    assert out["self"] == {"key": "wrap/work", "kind": "self"}
    assert out["desc"] == {"key": "wrap/work", "kind": "descendant"}
    assert out["other"] is None
    assert out["ancVis"] is False          # filtered from the grid
    assert out["ancDeny"] is False         # but never denied
    assert out["ancGuard"] == "allow"      # drilling in is allowed
    assert out["covGuard"] == "hidden"     # covering still denies
    pres = vpage.evaluate(
        "window.FolderVisibility.presentation('wrap')"
    )
    assert "配下に非表示" in pres["badgeHtml"]
    assert pres["buttonHtml"] == ""        # no PUT-404 toggle ever
    assert pres["className"] == "is-hidden-ancestor"


def test_hidden_match_case_and_unicode_insensitive(vpage):
    """Matching is NFC + lowercase: a case-variant direct URL
    (#/view/HIDE_ME for hidden key 'hide_me') still matches, and a
    case-only rename stays hidden. _hidden keeps original names."""
    _boot(vpage, _json_response(
        "{owner:'ownA',revision:1,hiddenRoots:['hide_me','Café X']}"
    ))
    _init(vpage)
    out = vpage.evaluate(
        """(() => {const FV = window.FolderVisibility; return [
          FV.isHidden('HIDE_ME'), FV.isHidden('Hide_Me/x'),
          FV.hiddenRootFor('HIDE_ME'),
          // NFC: 'Café X' precomposed vs 'Cafe\\u0301 X' combining mark
          FV.isHidden('Cafe\\u0301 X'), FV.isHidden('CAFÉ X'),
          FV.hiddenMatch('HIDE_ME').kind,
          FV.hiddenMatch('café x/sub').kind,
          FV.isVisible('HIDE_ME'), FV.guardPath('HIDE_ME'),
        ];})()"""
    )
    assert out == [
        True, True,
        "hide_me",            # original key returned, not the query's case
        True, True,
        "self", "descendant",
        False, "hidden",
    ]


def test_stale_put_response_not_adopted_converges_once(vpage):
    """A PUT resolving BEHIND the current revision (another device's write
    landed via GET while this write was in flight): the stale payload is
    not adopted; exactly one convergence GET pulls the true head."""
    _boot(
        vpage,
        "(u,o)=>{window.__calls.push({u:String(u),o:o||{}});"
        "if(o&&o.method==='PUT'){"
        "return Promise.resolve(new Response(JSON.stringify("
        "{owner:'ownA',revision:2,hiddenRoots:['hide_me']}),"
        "{status:200,headers:{'Content-Type':'application/json'}}));}"
        "const gets=window.__calls.filter(c=>!(c.o&&c.o.method==='PUT')).length;"
        "if(gets<=1){return Promise.resolve(new Response(JSON.stringify("
        "{owner:'ownA',revision:1,hiddenRoots:[]}),{status:200,"
        "headers:{'Content-Type':'application/json'}}));}"
        "if(gets===2){return Promise.resolve(new Response(JSON.stringify("
        "{owner:'ownA',revision:3,hiddenRoots:['keep_a']}),{status:200,"
        "headers:{'Content-Type':'application/json'}}));}"
        "return new Promise(res=>{(window.__held=window.__held||[]).push(res)});}",
    )
    _init(vpage)
    # Pull a NEWER head via GET (another device's write) — revision 3.
    vpage.evaluate("(()=>{window.FolderVisibility.refresh();})()")
    vpage.wait_for_function(
        "window.FolderVisibility.snapshot().revision === 3")
    gets0 = vpage.evaluate(
        "window.__calls.filter(c=>!(c.o&&c.o.method==='PUT')).length")
    # The PUT then resolves with the OLDER rev 2 — not adopted.
    res = vpage.evaluate("window.FolderVisibility.setHidden('hide_me', true)")
    assert res["ok"] is True
    snap = vpage.evaluate("window.FolderVisibility.snapshot()")
    assert snap["revision"] == 3
    assert snap["hiddenRoots"] == ["keep_a"]
    # Exactly ONE bounded convergence GET is issued (coalesce ~400ms) —
    # never a refresh storm.
    vpage.wait_for_function(
        "window.__calls.filter(c=>!(c.o&&c.o.method==='PUT'))"
        f".length >= {gets0 + 1}",
        timeout=5000)
    vpage.wait_for_timeout(700)
    gets = vpage.evaluate(
        "window.__calls.filter(c=>!(c.o&&c.o.method==='PUT')).length")
    assert gets == gets0 + 1
    # The convergence response lands the true head.
    vpage.evaluate(
        "(window.__held||[]).splice(0).forEach(r=>r(new Response(JSON.stringify("
        "{owner:'ownA',revision:4,hiddenRoots:['keep_a','hide_me']}),{status:200,"
        "headers:{'Content-Type':'application/json'}})))"
    )
    vpage.wait_for_function(
        "window.FolderVisibility.snapshot().revision === 4")
    snap = vpage.evaluate("window.FolderVisibility.snapshot()")
    assert sorted(snap["hiddenRoots"]) == ["hide_me", "keep_a"]
