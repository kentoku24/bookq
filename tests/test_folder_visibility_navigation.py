"""E2E for hidden-root navigation guards: direct URLs, stash, resume, mode
switch mid-view, Back/Forward. Dedicated server + state dir via
tests/folder_visibility_helpers.py.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from folder_visibility_helpers import (  # noqa: E402
    build_vis_tree,
    request_browser,
    spawn_vis_server,
    vis_put,
    vis_reset,
)


@pytest.fixture(scope="module")
def vis_env(tmp_path_factory):
    tree = build_vis_tree(tmp_path_factory.mktemp("vis_nav_dl"))
    state = tmp_path_factory.mktemp("vis_nav_state")
    proc, base = spawn_vis_server(tree, state)
    yield base
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture(autouse=True)
def _vis_clean(vis_env):
    vis_reset(vis_env)
    yield


@pytest.fixture
def vpage(vis_env, request):
    b = request_browser(request)
    context = b.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    page.goto(vis_env)
    page.wait_for_selector(".tile")
    yield page
    context.close()


def _no_vis_toast(vpage):
    """Silent redirect = no *visibility-explanation* toast. Unrelated toasts
    (SW "更新あり" first-install notice, viewer tap-hint) legitimately fire in
    the same window, so assert on content, not presence."""
    t = vpage.locator("#toast.visible")
    if t.count():
        assert "非表示" not in t.inner_text()


def _fresh_goto(vpage, url: str) -> None:
    """Full-document navigation (hash-only goto is a same-document nav that
    never re-reads the visibility snapshot)."""
    vpage.goto("about:blank")
    vpage.goto(url)


def test_direct_view_url_hidden_redirects_to_top(vpage, vis_env):
    vis_put(vis_env, "hide_me", True)
    _fresh_goto(vpage, vis_env + "/#/view/hide_me?p=0")
    vpage.wait_for_function("location.hash === ''")
    vpage.wait_for_selector(".tile")
    assert vpage.evaluate("window.currentFolder || null") is None
    assert vpage.locator("#sidebar.open").count() == 1
    _no_vis_toast(vpage)


def test_direct_browse_url_hidden_redirects_to_top(vpage, vis_env):
    vis_put(vis_env, "hide_series", True)
    _fresh_goto(vpage, vis_env + "/#/browse/hide_series")
    vpage.wait_for_function("location.hash === ''")
    assert vpage.evaluate("window.currentBrowsePath || null") is None
    _no_vis_toast(vpage)


def test_descendant_view_url_redirects(vpage, vis_env):
    vis_put(vis_env, "hide_series", True)
    _fresh_goto(vpage, vis_env + "/#/view/hide_series/vol1?p=0")
    vpage.wait_for_function("location.hash === ''")
    assert vpage.evaluate("window.currentFolder || null") is None
    _no_vis_toast(vpage)


def test_return_hash_stash_hidden_redirects(vpage, vis_env):
    vis_put(vis_env, "hide_me", True)
    vpage.evaluate(
        "sessionStorage.setItem('bookq_return_hash', "
        "JSON.stringify({hash:'#/view/hide_me?p=0',ts:Date.now()}))"
    )
    vpage.reload()
    vpage.wait_for_function("location.hash === ''")
    assert vpage.evaluate("window.currentFolder || null") is None
    _no_vis_toast(vpage)


def test_show_all_allows_hidden_view_url(vpage, vis_env):
    vis_put(vis_env, "hide_me", True)
    vpage.reload()
    vpage.wait_for_selector(".tile")
    vpage.click("#visibility-all")
    vpage.evaluate("location.hash = '#/view/hide_me?p=0'")
    vpage.wait_for_function("window.currentFolder === 'hide_me'")
    assert vpage.evaluate("location.hash").startswith("#/view/hide_me")


def test_switch_to_normal_exits_hidden_book(vpage, vis_env):
    vpage.click("#visibility-all")
    vpage.wait_for_function("window.FolderVisibility.showAll === true")
    vis_put(vis_env, "hide_me", True)
    vpage.evaluate("window.FolderVisibility.refresh()")
    vpage.wait_for_function("window.FolderVisibility.isHidden('hide_me')")
    vpage.click('#tile-grid .tile[data-path="hide_me"]')
    vpage.wait_for_function("window.currentFolder === 'hide_me'")
    # The mode toggle row lives in the sidebar — unreachable while the viewer
    # covers it — so flip the same code path directly.
    vpage.evaluate("window.FolderVisibility.setShowAll(false)")
    vpage.wait_for_function("window.currentFolder === null")
    vpage.wait_for_function("location.hash === ''")
    assert vpage.locator("#sidebar.open").count() == 1
    _no_vis_toast(vpage)


def test_refresh_newly_hidden_book_exits_to_top(vpage, vis_env):
    vpage.click('#tile-grid .tile[data-path="hide_me"]')
    vpage.wait_for_function("window.currentFolder === 'hide_me'")
    vis_put(vis_env, "hide_me", True)   # "another device" hid it
    vpage.evaluate("window.FolderVisibility.refresh()")
    vpage.wait_for_function("window.currentFolder === null")
    vpage.wait_for_function("location.hash === ''")
    _no_vis_toast(vpage)


def test_refresh_newly_hidden_browse_path_exits(vpage, vis_env):
    vpage.click('#tile-grid .tile[data-path="hide_series"]')
    vpage.wait_for_function("window.currentBrowsePath === 'hide_series'")
    vis_put(vis_env, "hide_series", True)
    vpage.evaluate("window.FolderVisibility.refresh()")
    vpage.wait_for_function("window.currentBrowsePath === null")
    vpage.wait_for_function("location.hash === ''")
    _no_vis_toast(vpage)


def test_back_forward_after_manual_hidden_hash_edit(vpage, vis_env):
    vis_put(vis_env, "hide_me", True)
    vpage.evaluate("window.FolderVisibility.refresh()")
    vpage.wait_for_function("window.FolderVisibility.isHidden('hide_me')")
    vpage.click('#tile-grid .tile[data-path="keep_a"]')
    vpage.wait_for_function("window.currentFolder === 'keep_a'")
    vpage.evaluate("window.returnToBrowse()")
    vpage.wait_for_selector(".tile")
    vpage.evaluate("location.hash = '#/view/hide_me?p=0'")
    vpage.wait_for_function("location.hash === ''")
    assert vpage.evaluate("window.currentFolder || null") is None
    vpage.go_back()
    vpage.wait_for_selector(".tile")
    # Back lands on a real browse root, never resurrects the hidden URL
    assert "hide_me" not in vpage.evaluate("location.hash")
    _no_vis_toast(vpage)


def test_resume_button_skips_hidden_newest(vpage, vis_env):
    vpage.evaluate(
        "localStorage.setItem('bookq_progress', JSON.stringify({"
        "'keep_a':{page:0,total:1,ts:1000},'hide_me':{page:1,total:2,ts:2000}}))"
    )
    vis_put(vis_env, "hide_me", True)
    vpage.reload()
    vpage.wait_for_selector(".tile")
    txt = vpage.locator("#resume-btn-text").inner_text()
    assert "keep_a" in txt
    assert "hide_me" not in txt
    # data itself is not deleted — 全显示 mode offers the hidden work again
    vpage.click("#visibility-all")
    vpage.wait_for_function("window.FolderVisibility.showAll === true")
    txt = vpage.locator("#resume-btn-text").inner_text()
    assert "hide_me" in txt


# ── loading / error judgment gates ──────────────────────────────────────


def _wait_held(held, timeout=10.0):
    """Poll until the route handler captured at least one request."""
    deadline = time.time() + timeout
    while not held and time.time() < deadline:
        time.sleep(0.05)
    return held


def _drop_vis_snapshot(vpage):
    """Remove the owner-scoped visibility snapshot so the next document
    loads cold — the vpage fixture's first successful load caches one, and
    with a snapshot the grid correctly renders it while refreshing (the
    loading/error gates only apply when NO snapshot exists at all)."""
    vpage.evaluate(
        "Object.keys(localStorage)"
        ".filter(k=>k.startsWith('bookq.folderVisibility.'))"
        ".forEach(k=>localStorage.removeItem(k))")


def test_loading_gate_shows_no_unfiltered_tiles(vpage, vis_env):
    """While the first visibility GET is outstanding the grid must never
    paint an unfiltered list — release converges to the filtered grid."""
    vis_put(vis_env, "hide_me", True)
    held = []
    vpage.route("**/api/folder-visibility", lambda r: held.append(r))
    _drop_vis_snapshot(vpage)
    vpage.reload()
    assert _wait_held(held), "visibility GET was never issued"
    vpage.wait_for_timeout(300)
    assert vpage.locator("#tile-grid .tile").count() == 0
    for r in list(held):
        r.fallback()
    vpage.wait_for_selector("#tile-grid .tile")
    paths = vpage.locator("#tile-grid .tile").evaluate_all(
        "els=>els.map(e=>e.dataset.path)")
    assert "hide_me" not in paths
    assert "keep_a" in paths


def test_loading_queues_browse_url_then_applies(vpage, vis_env):
    """Deep link while the judgment is outstanding: queued, never denied —
    the hash survives and the nav completes once visibility lands."""
    held = []
    vpage.route("**/api/folder-visibility", lambda r: held.append(r))
    _drop_vis_snapshot(vpage)
    _fresh_goto(vpage, vis_env + "/#/browse/hide_series")
    assert _wait_held(held), "visibility GET was never issued"
    vpage.wait_for_timeout(300)
    # pending ≠ deny: the hash is left intact for the post-ready drain.
    assert vpage.evaluate("location.hash") == "#/browse/hide_series"
    assert vpage.locator("#tile-grid .tile").count() == 0
    for r in list(held):
        r.fallback()
    vpage.wait_for_function("window.currentBrowsePath === 'hide_series'")


def test_loading_queues_view_url_then_applies(vpage, vis_env):
    held = []
    vpage.route("**/api/folder-visibility", lambda r: held.append(r))
    _drop_vis_snapshot(vpage)
    _fresh_goto(vpage, vis_env + "/#/view/keep_a?p=0")
    assert _wait_held(held), "visibility GET was never issued"
    vpage.wait_for_timeout(300)
    assert vpage.evaluate("location.hash").startswith("#/view/keep_a")
    assert vpage.evaluate("window.currentFolder || null") is None
    for r in list(held):
        r.fallback()
    vpage.wait_for_function("window.currentFolder === 'keep_a'")


def test_error_state_gates_grid_and_retry_recovers(vpage, vis_env):
    """GET failure with no snapshot: the grid renders the error gate (never
    an unfiltered list) and its retry converges to the filtered grid."""
    calls = {"n": 0}

    def handler(route):
        calls["n"] += 1
        if calls["n"] == 1:
            route.fulfill(status=500, content_type="application/json", body="{}")
        else:
            route.fallback()

    vpage.route("**/api/folder-visibility", handler)
    _drop_vis_snapshot(vpage)
    vpage.reload()
    vpage.wait_for_function("window.FolderVisibility.readyState === 'error'")
    vpage.wait_for_timeout(300)
    assert vpage.locator("#tile-grid .tile").count() == 0
    assert vpage.locator("#tile-grid .fv-grid-retry").count() == 1
    vpage.click("#tile-grid .fv-grid-retry")
    vpage.wait_for_selector("#tile-grid .tile")
    assert "keep_a" in vpage.locator("#tile-grid .tile").evaluate_all(
        "els=>els.map(e=>e.dataset.path)")
