# tests/test_url_layout_params.py
"""レイアウト設定（1P/2P, 見開きずらし, 表示モード）は URL に載る（決定 Q11/Q17）。
既定値はパラメータを書かない。本をまたいで引き継がない。localStorage には保存しない。"""
import re

from playwright.sync_api import expect


def _open_long(page, server):
    page.goto(server + "/#/view/long?p=0")
    page.wait_for_selector("#img-track img")


def test_default_layout_writes_no_params(landscape_page, server):
    _open_long(landscape_page, server)
    landscape_page.keyboard.press("ArrowLeft")
    # Landscape defaults to a 2-page spread, so navigation moves by spread
    # (0 -> 2), not by single page (0 -> 1) — CLAUDE.md "the spread is the
    # unit of both layout and navigation".
    expect(landscape_page).to_have_url(re.compile(r"#/view/long\?p=2$"))


def test_spread_toggle_and_shift_write_s_and_o(landscape_page, server):
    _open_long(landscape_page, server)
    landscape_page.locator("#spread-shift").click()          # o=1
    expect(landscape_page).to_have_url(re.compile(r"#/view/long\?p=0&o=1$"))
    landscape_page.locator("#spread-toggle").click()         # 2P → 1P: s=1（o は 1P では無意味なので落ちる）
    expect(landscape_page).to_have_url(re.compile(r"#/view/long\?p=0&s=1$"))
    landscape_page.locator("#spread-toggle").click()         # 1P → 2P: 既定に戻る
    expect(landscape_page).to_have_url(re.compile(r"#/view/long\?p=0$"))


def test_layout_params_restore_from_url(landscape_page, server):
    landscape_page.goto(server + "/#/view/long?p=2&s=1")
    landscape_page.wait_for_selector("#img-track img")
    assert landscape_page.evaluate("window.pageMode") == "single"
    assert landscape_page.evaluate("window.landscapeSpread") is False
    # Same folder/page, different query only -> same-document navigation
    # (hashchange fires but applyUrlState's st.page===cur.page short-circuits
    # the reload per the Task 2 note in the brief). A reload simulates the
    # realistic case of opening a shared deep link fresh.
    landscape_page.goto(server + "/#/view/long?p=2&o=1")
    landscape_page.reload()
    landscape_page.wait_for_selector("#img-track img")
    assert landscape_page.evaluate("window.spreadOffset") == 1


def test_layout_change_uses_replace_state_not_push(landscape_page, server):
    _open_long(landscape_page, server)
    before = landscape_page.evaluate("history.length")
    landscape_page.locator("#spread-shift").click()
    expect(landscape_page).to_have_url(re.compile(r"&o=1$"))
    assert landscape_page.evaluate("history.length") == before


def test_layout_not_carried_to_next_book(landscape_page, server):
    landscape_page.goto(server + "/#/view/long?p=0&o=1")
    landscape_page.wait_for_selector("#img-track img")
    landscape_page.keyboard.press("f")                       # サイドバーを開く
    landscape_page.locator('#tile-grid .tile[data-path="simple_manga"]').click()
    landscape_page.wait_for_url(re.compile(r"#/view/simple_manga\?p=0$"))
    assert landscape_page.evaluate("window.spreadOffset") == 0


def test_no_localstorage_for_layout(landscape_page, server):
    _open_long(landscape_page, server)
    landscape_page.locator("#spread-shift").click()
    keys = landscape_page.evaluate("Object.keys(localStorage)")
    assert all(k in ("bookq_progress", "bookq_persist_toast") for k in keys), keys


def test_view_mode_in_browse_url(page, server):
    page.goto(server + "/")
    page.wait_for_selector("#tile-grid .tile")
    page.locator("#footer-refresh-btn").click()   # tile → list
    expect(page).to_have_url(re.compile(r"#/browse/\?v=list$"))
    page.locator('#tile-grid [data-path="series"]').click()
    expect(page).to_have_url(re.compile(r"#/browse/series\?v=list$"))
    page.locator("#footer-refresh-btn").click()   # list → card
    expect(page).to_have_url(re.compile(r"#/browse/series\?v=card$"))
    page.locator("#footer-refresh-btn").click()   # card → tile（既定: パラメータ無し）
    expect(page).to_have_url(re.compile(r"#/browse/series$"))


def test_view_mode_restores_from_url(page, server):
    page.goto(server + "/#/browse/series?v=list")
    page.wait_for_selector("#tile-grid .item, #tile-grid .tile")
    assert page.evaluate("window.viewMode") == "list"
    expect(page.locator("#footer-refresh-btn")).to_have_text("📋")


def test_breadcrumb_back_after_mode_change_uses_history(page, server):
    # Root(tile) -> drill "series" -> toggle list -> breadcrumb up to root.
    # The urlStack ancestor lookup must dedup on the browse PATH only (not
    # the full URL incl. ?v=), so popping back to root reuses the existing
    # root entry via history.go() instead of pushing a new one. That root
    # entry was recorded before any mode toggle (bare, v-less URL), so
    # landing on it is a real "Back onto a pre-toggle entry" and — per the
    # URL-is-source-of-truth contract — resets viewMode to tile.
    page.goto(server + "/")
    page.wait_for_selector("#tile-grid .tile")
    page.locator('[data-path="series"]').click()
    expect(page).to_have_url(re.compile(r"#/browse/series$"))
    page.locator("#footer-refresh-btn").click()
    expect(page).to_have_url(re.compile(r"#/browse/series\?v=list$"))
    before = page.evaluate("history.length")
    page.locator("#browse-header").click()  # breadcrumb ↩ back to root
    # Ancestor pop reuses the pre-toggle root entry (bare, no hash) rather
    # than pushing a new "#/browse/?v=list" entry.
    expect(page).to_have_url(re.compile(r"^" + re.escape(server) + r"/?$"))
    after = page.evaluate("history.length")
    assert after == before, f"breadcrumb pop after a mode change grew history: {before} -> {after}"
    assert page.evaluate("window.viewMode") == "tile"
    expect(page.locator("#footer-refresh-btn")).to_have_text("🎨")
    # The DOM must actually re-render to match — a stale list-mode grid
    # under a tile-glyph footer was the fix-round-1 regression (the ↩
    # handler's own synchronous render happened before the async
    # hashchange reset viewMode, so nothing re-rendered afterwards).
    expect(page.locator("#tile-grid")).not_to_have_class(re.compile(r"list-mode"))
    expect(page.locator("#tile-grid .tile").first).to_be_visible()


def test_browse_url_without_v_resets_to_tile(page, server):
    page.goto(server + "/#/browse/series?v=list")
    page.wait_for_selector("#tile-grid .item, #tile-grid .tile")
    assert page.evaluate("window.viewMode") == "list"
    page.goto(server + "/#/browse/series")
    page.wait_for_selector("#tile-grid .tile")
    assert page.evaluate("window.viewMode") == "tile"
    expect(page.locator("#footer-refresh-btn")).to_have_text("🎨")


def test_dead_folder_with_v_clears_hash(page, server):
    # A dead/nonexistent folder must clear the hash back to canonical root
    # regardless of view mode — CLAUDE.md's "a dead or empty folder clears
    # the hash" is unconditional, not just for tile mode (fix round 2: the
    # v==="tile" guard on restoreBrowse's hash-clearing branch was written
    # for root normalization but also accidentally gated the dead-folder
    # case, letting a bogus "?v=list" hash survive).
    page.goto(server + "/#/browse/bogus_nonexistent?v=list")
    # Root count includes the parent-level images-tile ("mixed").
    expect(page.locator("#folder-count")).to_have_text("17 件")
    expect(page).to_have_url(re.compile(r"^" + re.escape(server) + r"/?$"))
