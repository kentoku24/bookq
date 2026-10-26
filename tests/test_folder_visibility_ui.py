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
    FLAT_VIS_ROOTS,
    VIS_ROOTS,
    build_flat_vis_tree,
    build_vis_tree,
    request_browser,
    spawn_vis_server,
    swipe_root_icon,
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
    swipe_root_icon(vpage)
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


# ── flattened scanner roots (wrap/wA etc. ARE the top-level nodes) ──────


@pytest.fixture(scope="module")
def flat_vis_env(tmp_path_factory):
    tree = build_flat_vis_tree(tmp_path_factory.mktemp("vis_flat_dl"))
    state = tmp_path_factory.mktemp("vis_flat_state")
    proc, base = spawn_vis_server(tree, state)
    yield base
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def fvpage(flat_vis_env, request):
    vis_reset(flat_vis_env, FLAT_VIS_ROOTS)
    b = request_browser(request)
    context = b.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    page.goto(flat_vis_env)
    page.wait_for_selector(".tile")
    yield page
    context.close()


def test_flat_top_level_tiles_are_flattened_names(fvpage):
    """Every top-level tile carries the flattened _fullPath — 'wrap' itself
    is never a node, and isTopLevel keys on the full name."""
    paths = _paths(fvpage)
    assert sorted(p for p in paths if p) == ["wrap/arc.zip", "wrap/wA", "wrap/wB"]
    assert fvpage.evaluate("window.FolderVisibility.isTopLevel('wrap/wA')") is True
    assert fvpage.evaluate("window.FolderVisibility.isTopLevel('wrap')") is False


def test_flat_normal_mode_hides_root(fvpage, flat_vis_env):
    vis_put(flat_vis_env, "wrap/wA", True)
    fvpage.reload()
    fvpage.wait_for_selector(".tile")
    paths = _paths(fvpage)
    assert "wrap/wA" not in paths
    assert "wrap/wB" in paths            # sibling must not over-match
    assert "wrap/arc.zip" in paths


def test_flat_show_all_badge_and_toggle(fvpage, flat_vis_env):
    vis_put(flat_vis_env, "wrap/wA", True)
    fvpage.reload()
    fvpage.wait_for_selector(".tile")
    _show_all(fvpage)
    tile = fvpage.locator('#tile-grid .tile[data-path="wrap/wA"]')
    assert "非表示" in tile.locator(".visibility-badge").inner_text()
    btn = tile.locator(".visibility-toggle")
    assert btn.count() == 1             # flattened root is a real top-level — editable
    assert btn.get_attribute("aria-pressed") == "true"
    # unhide via the button → badge gone, server cleared
    btn.click()
    fvpage.wait_for_function("!window.FolderVisibility.isHidden('wrap/wA')")
    assert "wrap/wA" not in vis_get(flat_vis_env)["hiddenRoots"]


def test_flat_toggle_button_hides_multi_segment_root(fvpage, flat_vis_env):
    """The delegated toggle sends the full flattened path as the root key."""
    _show_all(fvpage)
    fvpage.locator('#tile-grid .tile[data-path="wrap/wB"] .visibility-toggle').click()
    fvpage.wait_for_function("window.FolderVisibility.isHidden('wrap/wB')")
    assert "wrap/wB" in vis_get(flat_vis_env)["hiddenRoots"]
    fvpage.click("#visibility-normal")
    assert "wrap/wB" not in _paths(fvpage)


def test_flat_archive_root_toggle(fvpage):
    _show_all(fvpage)
    tile = fvpage.locator('#tile-grid .tile[data-path="wrap/arc.zip"]')
    tile.locator(".visibility-toggle").click()
    fvpage.wait_for_function("window.FolderVisibility.isHidden('wrap/arc.zip')")
    fvpage.click("#visibility-normal")
    assert "wrap/arc.zip" not in _paths(fvpage)


def test_flat_hidden_view_url_redirects(fvpage, flat_vis_env):
    vis_put(flat_vis_env, "wrap/wA", True)
    fvpage.goto("about:blank")
    fvpage.goto(flat_vis_env + "/#/view/wrap/wA?p=0")
    fvpage.wait_for_function("location.hash === ''")
    assert fvpage.evaluate("window.currentFolder || null") is None


def test_flat_hidden_browse_url_redirects(fvpage, flat_vis_env):
    vis_put(flat_vis_env, "wrap/wA", True)
    fvpage.goto("about:blank")
    fvpage.goto(flat_vis_env + "/#/browse/wrap/wA")
    fvpage.wait_for_function("location.hash === ''")
    assert fvpage.evaluate("window.currentBrowsePath || null") is None


# ── hidden entry gesture (root-icon swipe) ──────────────────────────────


def test_mode_row_hidden_in_normal_mode(vpage):
    # The feature leaves no trace in normal mode: no row, no body marker.
    assert vpage.locator("#visibility-row").is_hidden()
    assert vpage.locator("body.fv-showall").count() == 0


def test_swipe_toggles_show_all_and_marks_icon(vpage, vis_env):
    vis_put(vis_env, "hide_me", True)
    vpage.reload()
    vpage.wait_for_selector(".tile")
    assert "hide_me" not in _paths(vpage)
    swipe_root_icon(vpage)
    vpage.wait_for_function("window.FolderVisibility.showAll === true")
    assert vpage.locator("#visibility-row").is_visible()
    assert vpage.locator("body.fv-showall").count() == 1
    assert "hide_me" in _paths(vpage)
    # same gesture exits back to normal mode
    swipe_root_icon(vpage)
    vpage.wait_for_function("window.FolderVisibility.showAll === false")
    assert vpage.locator("#visibility-row").is_hidden()
    assert "hide_me" not in _paths(vpage)


def test_short_swipe_does_not_enter(vpage):
    icon = vpage.locator("#browse-root-icon")
    box = icon.bounding_box()
    x0 = box["x"] + box["width"] / 2
    y0 = box["y"] + box["height"] / 2
    vpage.mouse.move(x0, y0)
    vpage.mouse.down()
    vpage.mouse.move(x0 + 80, y0, steps=5)  # ~20% of a 390px viewport — no edge
    vpage.mouse.up()
    assert vpage.evaluate("window.FolderVisibility.showAll") is False
    assert vpage.locator("#visibility-row").is_hidden()


def test_gesture_icon_only_at_browse_root(vpage):
    # Subfolder header is "↩ name" with no #browse-root-icon — no gesture.
    vpage.click('#tile-grid .tile[data-path="hide_series"]')
    vpage.wait_for_function("window.currentBrowsePath === 'hide_series'")
    assert vpage.locator("#browse-root-icon").count() == 0
    assert vpage.locator("#visibility-row").is_hidden()


# ── review round 2 ─────────────────────────────────────────────────────


def test_shallow_window_filters_hidden_ancestor_tile(fvpage, flat_vis_env):
    """fix3: while only the shallow (depth=1, raw dir names) tree is up, the
    raw dir 'wrap' is an *ancestor* of hidden 'wrap/wA' — it must be filtered
    from the normal-mode grid and must never get a toggle (PUT 'wrap' 404s),
    even in 全表示 where it shows the 配下に非表示 badge."""
    vis_put(flat_vis_env, "wrap/wA", True)
    held_full = []

    def _route(route):
        if "depth=" in route.request.url:
            # Deterministic live-FS shallow payload: raw dir name only.
            route.fulfill(
                status=200,
                content_type="application/json",
                body='[{"name":"wrap","type":"folder","children":[],'
                     '"thumb":null}]')
        else:
            held_full.append(route)

    fvpage.route("**/api/folders*", _route)
    fvpage.reload()
    fvpage.wait_for_function(
        "window.FolderVisibility && "
        "window.FolderVisibility.readyState !== 'loading'")
    fvpage.wait_for_function(
        "window.__BOOKQ_DEBUG && window.__BOOKQ_DEBUG.shallow === true")
    fvpage.wait_for_timeout(300)
    # Normal mode: 'wrap' tile filtered — the shallow window does not leak
    # the hidden root's ancestor, and nothing unfiltered is painted.
    assert fvpage.locator("#tile-grid .tile").count() == 0
    # Provisional shallow indexing must not publish editable top-levels.
    assert fvpage.evaluate(
        "window.FolderVisibility.isTopLevel('wrap')") is False
    assert fvpage.evaluate(
        "window.FolderVisibility.isTopLevel('wrap/wB')") is False
    # 全表示 during the shallow window: ancestor badge, still no toggle.
    fvpage.evaluate("window.FolderVisibility.setShowAll(true)")
    tile = fvpage.locator('#tile-grid .tile[data-path="wrap"]')
    tile.wait_for(state="attached")
    assert "配下に非表示" in tile.locator(".visibility-badge").inner_text()
    assert tile.locator(".visibility-toggle").count() == 0
    assert fvpage.evaluate(
        "window.FolderVisibility.isTopLevel('wrap')") is False
    # Release the full fetch — flattened tiles arrive, hidden self filtered.
    fvpage.evaluate("window.FolderVisibility.setShowAll(false)")
    for r in list(held_full):
        r.fallback()
    fvpage.wait_for_selector('#tile-grid .tile[data-path="wrap/wB"]')
    fvpage.wait_for_function(
        "window.__BOOKQ_DEBUG.shallow === false")
    paths = _paths(fvpage)
    assert "wrap/wA" not in paths
    assert "wrap/wB" in paths
    assert "wrap/arc.zip" in paths
    # Full-tree top-levels are now published — 'wrap' (non-node) never is.
    assert fvpage.evaluate(
        "window.FolderVisibility.isTopLevel('wrap/wA')") is True
    assert fvpage.evaluate(
        "window.FolderVisibility.isTopLevel('wrap')") is False


def test_gesture_release_on_document_clears_drag(vpage):
    """fix11: a pointerup routed to document (release outside the header /
    OS-level interruption) must still close the drag — a later hover must
    never move or re-arm the icon."""
    icon = vpage.locator("#browse-root-icon")
    icon.wait_for(state="attached")
    vpage.evaluate(
        """() => {
          const icon = document.getElementById('browse-root-icon');
          icon.dispatchEvent(new PointerEvent('pointerdown',
            {pointerId:7, clientX:10, clientY:10, bubbles:true}));
          document.dispatchEvent(new PointerEvent('pointermove',
            {pointerId:7, clientX:80, clientY:10, bubbles:true}));
        }""")
    vpage.wait_for_function(
        "document.getElementById('browse-root-icon').style.transform !== ''")
    # Release lands on document, not on the icon/header.
    vpage.evaluate(
        "document.dispatchEvent(new PointerEvent('pointerup',"
        "{pointerId:7, clientX:80, clientY:10, bubbles:true}))")
    # Hover afterwards: no drag state left behind, icon stays put, showAll
    # was not toggled by the interrupted gesture.
    vpage.evaluate(
        "document.dispatchEvent(new PointerEvent('pointermove',"
        "{pointerId:7, clientX:200, clientY:10, bubbles:true}))")
    vpage.wait_for_timeout(100)
    assert vpage.evaluate(
        "document.getElementById('browse-root-icon').style.transform") == ""
    assert vpage.evaluate("window.FolderVisibility.showAll") is False


def test_gesture_window_blur_clears_drag(vpage):
    """fix11: window blur mid-drag (app switch / notification) abandons the
    gesture — icon snaps back, later moves do nothing."""
    vpage.wait_for_selector("#browse-root-icon")
    vpage.evaluate(
        """() => {
          const icon = document.getElementById('browse-root-icon');
          icon.dispatchEvent(new PointerEvent('pointerdown',
            {pointerId:9, clientX:10, clientY:10, bubbles:true}));
          document.dispatchEvent(new PointerEvent('pointermove',
            {pointerId:9, clientX:80, clientY:10, bubbles:true}));
        }""")
    vpage.wait_for_function(
        "document.getElementById('browse-root-icon').style.transform !== ''")
    vpage.evaluate("window.dispatchEvent(new Event('blur'))")
    vpage.evaluate(
        "document.dispatchEvent(new PointerEvent('pointermove',"
        "{pointerId:9, clientX:200, clientY:10, bubbles:true}))")
    vpage.wait_for_timeout(100)
    assert vpage.evaluate(
        "document.getElementById('browse-root-icon').style.transform") == ""
    assert vpage.evaluate("window.FolderVisibility.showAll") is False


_ITEM_SEL = {"tile": ".tile", "list": ".item", "card": ".card"}
_ART_SEL = {"tile": ".tile-img", "list": ".thumb", "card": ".card-img"}


def _cs(el, prop):
    return el.evaluate("(e,p)=>getComputedStyle(e)[p]", prop)


def _set_view_mode(vpage, mode):
    for _ in range(3):
        if vpage.evaluate("window.viewMode") == mode:
            break
        vpage.click("#footer-refresh-btn")
    assert vpage.evaluate("window.viewMode") == mode
    vpage.wait_for_function(
        "() => {const c=document.getElementById('tile-grid').classList;"
        "return c.contains('list-mode')===" + str(mode == "list").lower() +
        "&&c.contains('card-mode')===" + str(mode == "card").lower() + ";}"
    )


def _assert_no_dimming(hidden, sibling, mode):
    """Hidden vs visible sibling share artwork/name computed styles —
    is-hidden* contributes badges/buttons only, never dimming."""
    if mode == "list":
        h_thumb = hidden.locator(".thumb")
        s_thumb = sibling.locator(".thumb")
        assert h_thumb.count() == 1
        assert _cs(h_thumb, "opacity") == _cs(s_thumb, "opacity") == "0.75"
        assert _cs(h_thumb, "filter") == "none"
        assert _cs(hidden.locator(".name"), "color") == _cs(
            sibling.locator(".name"), "color")
    else:
        art = hidden.locator(_ART_SEL[mode])
        assert art.count() == 1
        assert _cs(art, "opacity") == "1"
        assert _cs(art, "filter") == "none"


def _assert_hidden_badge_and_toggle(item):
    assert "非表示" in item.locator(".visibility-badge").inner_text()
    btn = item.locator(".visibility-toggle")
    assert btn.get_attribute("aria-pressed") == "true"
    assert btn.get_attribute("aria-label") == "再表示する"


@pytest.mark.parametrize("mode", ["tile", "list", "card"])
def test_show_all_hidden_not_dimmed_browse(vpage, vis_env, mode):
    vis_put(vis_env, "hide_me", True)
    vpage.reload()
    vpage.wait_for_selector(".tile")
    _show_all(vpage)
    _set_view_mode(vpage, mode)
    sel = _ITEM_SEL[mode]
    hidden = vpage.locator(f'#tile-grid {sel}[data-path="hide_me"]')
    sibling = vpage.locator(f'#tile-grid {sel}[data-path="hide_me_b"]')
    hidden.wait_for(state="attached")
    sibling.wait_for(state="attached")
    _assert_no_dimming(hidden, sibling, mode)
    _assert_hidden_badge_and_toggle(hidden)


@pytest.mark.parametrize("mode", ["tile", "list", "card"])
def test_show_all_hidden_not_dimmed_search(vpage, vis_env, mode):
    vis_put(vis_env, "hide_me", True)
    vpage.reload()
    vpage.wait_for_selector(".tile")
    _show_all(vpage)
    _set_view_mode(vpage, mode)
    vpage.fill("#search-input", "hide")
    vpage.wait_for_function(
        "document.getElementById('folder-count').textContent === '4 件'")
    sel = _ITEM_SEL[mode]
    hidden = vpage.locator(f'#tile-grid {sel}[data-path="hide_me"]')
    sibling = vpage.locator(f'#tile-grid {sel}[data-path="hide_me_b"]')
    hidden.wait_for(state="attached")
    sibling.wait_for(state="attached")
    if mode == "list":
        assert hidden.locator(".thumb").count() == 0
        assert _cs(hidden.locator(".name"), "color") == _cs(
            sibling.locator(".name"), "color")
    else:
        _assert_no_dimming(hidden, sibling, mode)
    _assert_hidden_badge_and_toggle(hidden)


@pytest.mark.parametrize("mode", ["tile", "list", "card"])
def test_show_all_hidden_descendant_not_dimmed(vpage, vis_env, mode):
    vis_put(vis_env, "hide_series", True)
    vpage.reload()
    vpage.wait_for_selector(".tile")
    _show_all(vpage)
    _set_view_mode(vpage, mode)
    sel = _ITEM_SEL[mode]
    root_el = vpage.locator(f'#tile-grid {sel}[data-path="hide_series"]')
    sibling = vpage.locator(f'#tile-grid {sel}[data-path="keep_a"]')
    root_el.wait_for(state="attached")
    sibling.wait_for(state="attached")
    if mode == "tile":
        multi = root_el.locator(".tile-multi")
        multi.wait_for(state="attached")
        assert _cs(multi, "opacity") == "1"
        assert _cs(multi, "filter") == "none"
    else:
        _assert_no_dimming(root_el, sibling, mode)
    ref_name = ref_thumb = None
    if mode == "list":
        ref_name = _cs(sibling.locator(".name"), "color")
        ref_thumb = _cs(sibling.locator(".thumb"), "opacity")
    root_el.click()
    child = vpage.locator(f'#tile-grid {sel}[data-path="hide_series/vol1"]')
    child.wait_for(state="attached")
    assert "is-hidden-descendant" in (child.get_attribute("class") or "")
    if mode == "list":
        assert _cs(child.locator(".thumb"), "opacity") == ref_thumb
        assert _cs(child.locator(".thumb"), "filter") == "none"
        assert _cs(child.locator(".name"), "color") == ref_name
    else:
        art = child.locator(_ART_SEL[mode])
        assert art.count() == 1
        assert _cs(art, "opacity") == "1"
        assert _cs(art, "filter") == "none"
    assert "上位フォルダが非表示" in child.locator(
        ".visibility-badge").inner_text()
    assert child.locator(".visibility-toggle").count() == 0
