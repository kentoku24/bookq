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
VIS_SRC = (REPO_ROOT / "app" / "static" / "browse" / "visibility.js").read_text(encoding="utf-8")

_STUB_HTML = """<!DOCTYPE html><html><body>
<div id="tile-grid"></div>
<span id="visibility-status"></span>
<button id="visibility-normal"></button>
<button id="visibility-all"></button>
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


def test_hidden_set_apply_and_revision_monotonic(vpage):
    _boot(vpage, _json_response(
        "{owner:'ownA',revision:3,hiddenRoots:['hide_me','hide_series']}"
    ))
    _init(vpage)
    assert vpage.evaluate("window.FolderVisibility.isHidden('hide_me/x')") is True
    assert vpage.evaluate("window.FolderVisibility.isVisible('hide_series/vol1')") is False
    assert vpage.evaluate("window.FolderVisibility.isVisible('keep_a')") is True
    # Older revision must not apply (monotonic per owner).
    vpage.evaluate(
        "window.fetch=(u,o)=>{window.__calls.push({u:String(u),o:o||{}});"
        "return Promise.resolve(new Response(JSON.stringify("
        "{owner:'ownA',revision:1,hiddenRoots:[]}),{status:200}))}"
    )
    vpage.evaluate("window.FolderVisibility.refresh()")
    vpage.wait_for_function("window.FolderVisibility._debug().fetchGen >= 2")
    snap = vpage.evaluate("window.FolderVisibility.snapshot()")
    assert snap["revision"] == 3
    assert sorted(snap["hiddenRoots"]) == ["hide_me", "hide_series"]


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


def test_no_snapshot_error_fail_closed_with_retry(vpage):
    _boot(vpage, "(u,o)=>{window.__calls.push({u:String(u),o:o||{}});"
                 "return Promise.reject(new TypeError('down'))}")
    _init(vpage)
    vpage.wait_for_function("window.FolderVisibility.readyState === 'error'")
    # Fail closed: nothing is "visible" without any snapshot.
    assert vpage.evaluate("window.FolderVisibility.isVisible('keep_a')") is False
    assert vpage.evaluate("!!document.getElementById('visibility-retry')") is True


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
