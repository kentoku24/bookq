"""E2E for the hidden-roots UI (tile/list/card × browse/search, mode toggle).

Dedicated synthetic library + dedicated BOOKQ_STATE_DIR + own uvicorn via
tests/folder_visibility_helpers.py — the shared conftest server/tree keep
their exact root count and never see a hidden flag.
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
    vis_get,
    vis_put,
    vis_reset,
)


@pytest.fixture(scope="module")
def vis_env(tmp_path_factory):
    tree = build_vis_tree(tmp_path_factory.mktemp("vis_dl"))
    state = tmp_path_factory.mktemp("vis_state")
    proc, base = spawn_vis_server(tree, state)
    yield base
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture(autouse=True)
def _vis_clean(vis_env):
    # Module-scoped server: un-hide every root before each test so state never
    # leaks between tests.
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


def _paths(vpage, sel="#tile-grid .tile"):
    return vpage.locator(sel).evaluate_all("els=>els.map(e=>e.dataset.path)")


def _show_all(vpage):
    vpage.click("#visibility-all")
    vpage.wait_for_function("window.FolderVisibility.showAll === true")


def test_normal_mode_hides_root_and_prefix_sibling_safe(vpage, vis_env):
    vis_put(vis_env, "hide_me", True)
    vpage.reload()
    vpage.wait_for_selector(".tile")
    paths = _paths(vpage)
    assert "hide_me" not in paths
    assert "hide_me_b" in paths   # "A" hidden must not hide "AB"
    assert "keep_a" in paths
    # no visibility UI markup at all in normal mode
    assert vpage.locator(".visibility-toggle").count() == 0
    assert vpage.locator(".visibility-badge").count() == 0


def test_show_all_reveals_hidden_with_badge_and_toggle(vpage, vis_env):
    vis_put(vis_env, "hide_me", True)
    vpage.reload()
    vpage.wait_for_selector(".tile")
    _show_all(vpage)
    assert "hide_me" in _paths(vpage)
    tile = vpage.locator('#tile-grid .tile[data-path="hide_me"]')
    assert "非表示" in tile.locator(".visibility-badge").inner_text()
    btn = tile.locator(".visibility-toggle")
    assert btn.count() == 1
    assert btn.get_attribute("aria-label") == "再表示する"
    assert btn.get_attribute("aria-pressed") == "true"


def test_toggle_button_hides_then_normal_reasserts(vpage, vis_env):
    _show_all(vpage)
    tile = vpage.locator('#tile-grid .tile[data-path="hide_me"]')
    tile.locator(".visibility-toggle").click()
    vpage.wait_for_function("window.FolderVisibility.isHidden('hide_me')")
    # still listed in 全表示, marked
    assert tile.locator(".visibility-badge").inner_text() == "非表示"
    vpage.click("#visibility-normal")
    assert "hide_me" not in _paths(vpage)
    assert "hide_me" in vis_get(vis_env)["hiddenRoots"]


def test_unhide_via_toggle_restores(vpage, vis_env):
    vis_put(vis_env, "hide_me", True)
    vpage.reload()
    vpage.wait_for_selector(".tile")
    _show_all(vpage)
    vpage.locator('#tile-grid .tile[data-path="hide_me"] .visibility-toggle').click()
    vpage.wait_for_function("!window.FolderVisibility.isHidden('hide_me')")
    assert vpage.locator('#tile-grid .tile[data-path="hide_me"] .visibility-badge').count() == 0
    assert "hide_me" not in vis_get(vis_env)["hiddenRoots"]


def test_reload_resets_to_normal_mode(vpage):
    _show_all(vpage)
    vpage.reload()
    vpage.wait_for_selector(".tile")
    assert vpage.evaluate("window.FolderVisibility.showAll") is False
    assert vpage.locator("#visibility-normal").get_attribute("aria-pressed") == "true"


def test_data_read_self_never_hidden(vpage, vis_env):
    for r in VIS_ROOTS:
        vis_put(vis_env, r, True)
    vpage.reload()
    vpage.wait_for_selector(".tile")
    _show_all(vpage)
    self_tile = vpage.locator('#tile-grid .tile[data-read-self="1"]')
    assert self_tile.count() == 1
    assert self_tile.locator(".visibility-toggle").count() == 0
    assert self_tile.locator(".visibility-badge").count() == 0


def test_descendant_badges_and_read_child_dup(vpage, vis_env):
    vis_put(vis_env, "hide_series", True)
    vis_put(vis_env, "hide_mixed", True)
    vpage.reload()
    vpage.wait_for_selector(".tile")
    _show_all(vpage)
    # read-child duplicate: same root badge, no edit button
    rc = vpage.locator('#tile-grid .tile[data-path="hide_mixed"][data-read-child="1"]')
    assert rc.locator(".visibility-badge").inner_text() == "非表示"
    assert rc.locator(".visibility-toggle").count() == 0
    parent = vpage.locator('#tile-grid .tile[data-path="hide_mixed"]:not([data-read-child])')
    assert parent.locator(".visibility-toggle").count() == 1
    # drill into the hidden series — descendants carry the descendant badge
    vpage.click('#tile-grid .tile[data-path="hide_series"]')
    vpage.wait_for_selector('#tile-grid .tile[data-path="hide_series/vol1"]')
    vol = vpage.locator('#tile-grid .tile[data-path="hide_series/vol1"]')
    assert vol.locator(".visibility-badge").inner_text() == "上位フォルダが非表示"
    assert vpage.locator("#tile-grid .visibility-toggle").count() == 0


def test_hidden_filtered_from_search_in_all_modes(vpage, vis_env):
    vis_put(vis_env, "hide_me", True)
    vis_put(vis_env, "hide_series", True)
    vpage.reload()
    vpage.wait_for_selector(".tile")
    for _mode in ("tile", "list", "card"):
        vpage.fill("#search-input", "hide")
        vpage.wait_for_timeout(700)  # 300ms debounce + fetch
        paths = _paths(vpage, "#tile-grid .tile,#tile-grid .item,#tile-grid .card")
        assert "hide_me" not in paths
        assert "hide_series" not in paths
        assert "hide_me_b" in paths
        assert "hide_mixed" in paths
        n = len(paths)
        assert vpage.locator("#folder-count").inner_text() == f"{n} 件"
        vpage.click("#footer-refresh-btn")  # tile→list→card
    # 全表示: hidden results return but carry badges, no per-child buttons
    _show_all(vpage)
    vpage.fill("#search-input", "hide")
    vpage.wait_for_timeout(700)
    paths = _paths(vpage, "#tile-grid .tile,#tile-grid .item,#tile-grid .card")
    assert "hide_me" in paths
    assert "hide_series" in paths
    assert vpage.locator(".visibility-badge").count() >= 2


def test_folder_count_visible_in_all_modes(vpage, vis_env):
    vis_put(vis_env, "hide_me", True)
    vpage.reload()
    vpage.wait_for_selector(".tile")
    for _ in range(3):
        tiles = _paths(vpage, "#tile-grid .tile,#tile-grid .item,#tile-grid .card")
        assert vpage.locator("#folder-count").inner_text() == f"{len(tiles)} 件"
        vpage.click("#footer-refresh-btn")


def test_keyboard_toggle_no_viewer_no_double_put(vpage):
    vpage.add_init_script(
        "window.__putCount=0;const _f=window.fetch.bind(window);"
        "window.fetch=(u,o)=>{if(o&&o.method==='PUT'&&String(u).includes('folder-visibility'))window.__putCount++;return _f(u,o)}"
    )
    vpage.reload()
    vpage.wait_for_selector(".tile")
    _show_all(vpage)
    btn = vpage.locator('#tile-grid .tile[data-path="hide_me"] .visibility-toggle')
    btn.focus()
    vpage.keyboard.press("Enter")
    vpage.wait_for_function("window.FolderVisibility.isHidden('hide_me')")
    assert vpage.evaluate("window.__putCount") == 1
    assert vpage.evaluate("window.currentFolder || null") is None
    # The committed snapshot re-rendered the grid — the focused node is gone,
    # so refocus the live toggle before Space.
    vpage.locator('#tile-grid .tile[data-path="hide_me"] .visibility-toggle').focus()
    vpage.keyboard.press(" ")
    vpage.wait_for_function("!window.FolderVisibility.isHidden('hide_me')")
    assert vpage.evaluate("window.__putCount") == 2
    assert vpage.evaluate("window.currentFolder || null") is None


def test_identical_refresh_does_not_rerender(vpage):
    vpage.evaluate(
        "window.__muts=0;new MutationObserver(()=>window.__muts++)"
        ".observe(document.getElementById('tile-grid'),{childList:true,subtree:true})"
    )
    vpage.evaluate("window.FolderVisibility.refresh()")
    vpage.wait_for_timeout(700)
    assert vpage.evaluate("window.__muts") == 0


def test_archive_root_toggle(vpage, vis_env):
    _show_all(vpage)
    tile = vpage.locator('#tile-grid .tile[data-path="hide_arc.zip"]')
    tile.locator(".visibility-toggle").click()
    vpage.wait_for_function("window.FolderVisibility.isHidden('hide_arc.zip')")
    vpage.click("#visibility-normal")
    assert "hide_arc.zip" not in _paths(vpage)
