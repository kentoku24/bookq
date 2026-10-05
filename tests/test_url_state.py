"""Viewer URL-state behavior tests (Todo 1: URL helpers + writers).

T1 covers the writer half of the URL-state feature: opening a folder pushes
`#/view/<folder>?p=<page>` and every page turn rewrites it in place with
replaceState so the browser history never grows page-by-page.

The reader half (deep links, reload restore, Back/Forward re-sync) is wired in
Todo 2 and covered by tests added there.

Run:
    uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_url_state.py -q
"""

import re

import pytest
from playwright.sync_api import expect


def open_folder(page, path: str, expect_pages: int):
    page.locator(f'.tile[data-path="{path}"]').click()
    expect(page.locator("#counter")).to_have_text(f"1 / {expect_pages}")


def turn_page(page, from_pages: int):
    """Turn one page forward (left tap zone = next page in RTL manga) and wait."""
    page.locator("#zone-prev").click()
    expect(page.locator("#counter")).to_have_text(f"{from_pages + 1} / 5")


# ── T1: URL writers ────────────────────────────────────────────────────────────


def test_opening_folder_sets_url_hash(page):
    open_folder(page, "simple_manga", expect_pages=5)
    expect(page).to_have_url(re.compile(r"#/view/simple_manga\?p=0$"))


def test_page_turn_updates_url_hash(page):
    open_folder(page, "simple_manga", expect_pages=5)
    turn_page(page, from_pages=1)
    expect(page).to_have_url(re.compile(r"#/view/simple_manga\?p=1$"))


def test_page_turn_does_not_grow_history(page):
    open_folder(page, "simple_manga", expect_pages=5)
    # Opening pushed exactly one entry; record that baseline.
    before = page.evaluate("history.length")
    turn_page(page, from_pages=1)
    turn_page(page, from_pages=2)
    after = page.evaluate("history.length")
    assert after == before, f"page turns grew history: {before} -> {after}"
    expect(page).to_have_url(re.compile(r"#/view/simple_manga\?p=2$"))


# ── T2: URL readers + re-sync ─────────────────────────────────────────────────


def test_reload_keeps_position(page):
    open_folder(page, "long", expect_pages=30)
    page.keyboard.press("End")
    expect(page.locator("#counter")).to_have_text("30 / 30")
    expect(page).to_have_url(re.compile(r"#/view/long\?p=29$"))
    page.reload()
    expect(page.locator("#counter")).to_have_text("30 / 30")
    expect(page).to_have_url(re.compile(r"#/view/long\?p=29$"))


def test_deep_link_opens_directly(page, server):
    page.goto(server + "/#/view/simple_manga?p=3")
    expect(page.locator("#counter")).to_have_text("4 / 5")
    expect(page).to_have_url(re.compile(r"#/view/simple_manga\?p=3$"))


def test_deep_link_empty_folder_clears_hash(page, server):
    page.goto(server + "/#/view/empty_folder?p=0")
    expect(page.locator("#empty")).to_have_text("📭 このフォルダに画像はありません")
    expect(page).to_have_url(re.compile(r"^" + re.escape(server) + r"/?$"))


def test_manual_hash_edit_navigates(page):
    page.evaluate("location.hash = '#/view/simple_manga?p=3'")
    expect(page.locator("#counter")).to_have_text("4 / 5")


def test_out_of_range_page_clamps_to_first(page, server):
    page.goto(server + "/#/view/simple_manga?p=99")
    expect(page.locator("#counter")).to_have_text("1 / 5")


def test_dead_folder_hash_is_cleared(page, server):
    page.goto(server + "/#/view/nonexistent?p=0")
    expect(page.locator("#empty")).to_have_text("⚠️ 画像を読み込めませんでした")
    expect(page).to_have_url(re.compile(r"^" + re.escape(server) + r"/?$"))
    expect(page.locator("#sidebar")).to_have_class(re.compile(r"open"))


def test_back_returns_to_previous_folder(page):
    open_folder(page, "simple_manga", expect_pages=5)
    # H4(b) makes the closed sidebar inert (its controls leave the tab order
    # and cannot be clicked off-canvas). Reopen it with the F toggle — the
    # documented sidebar shortcut — like a user would, before opening the
    # next book. The F handler never touches the URL/history, so the Back
    # semantics asserted below are unchanged.
    page.keyboard.press("f")
    open_folder(page, "long", expect_pages=30)
    page.go_back()
    expect(page.locator("#counter")).to_have_text("1 / 5")
    expect(page).to_have_url(re.compile(r"#/view/simple_manga\?p=0$"))


def test_back_twice_reaches_top(page, server):
    open_folder(page, "simple_manga", expect_pages=5)
    # Same H4(b) sidebar reopen as above (see test_back_returns_to_previous_folder).
    page.keyboard.press("f")
    open_folder(page, "long", expect_pages=30)
    page.go_back()
    expect(page.locator("#counter")).to_have_text("1 / 5")
    page.go_back()
    expect(page).to_have_url(re.compile(r"^" + re.escape(server) + r"/?$"))
    expect(page.locator("#empty")).to_have_text("📂 サイドバーからフォルダを選んでください")
    expect(page.locator("#sidebar")).to_have_class(re.compile(r"open"))


def test_deep_link_unicode_folder(page, server):
    page.goto(server + "/#/view/%E5%A4%8F%20%E3%81%AE%20%E6%9C%AC?p=0")
    expect(page.locator("#counter")).to_have_text("1 / 1")
    expect(page.locator("#folder-name")).to_have_text("夏 の 本")


def test_unicode_round_trip(page, server):
    open_folder(page, "夏 の 本", expect_pages=1)
    assert "%E5%A4%8F%20%E3%81%AE%20%E6%9C%AC" in page.url, page.url
    page.reload()
    expect(page.locator("#counter")).to_have_text("1 / 1")
    expect(page).to_have_url(re.compile(r"#/view/%E5%A4%8F%20%E3%81%AE%20%E6%9C%AC\?p=0$"))


# ── T3: cross-chapter URL semantics ────────────────────────────────────────────
# group/deep_series/vol1 (p1, p2) + vol2 (p1) concatenate into one strip of 3
# real images (chapter concat needs raw-path depth >= SIBLING_AUTOLOAD_MIN_DEPTH
# = 2, so the depth-1 "series/" fixture deliberately does NOT concatenate).
# currentViewerState() reports the CHAPTER being read, so the URL folder must
# follow chapter boundaries, and a deep link must land inside its chapter. The
# committed counter format has NO " — <chapter>" suffix: the chapter name lives
# in #folder-name only.


def test_deep_link_nested_folder(page, server):
    page.goto(server + "/#/view/group/deep_series/vol1?p=0")
    expect(page.locator("#counter")).to_have_text("1 / 2")
    expect(page.locator("#folder-name")).to_contain_text("vol1")


def test_deep_link_cross_chapter_lands_in_chapter(page, server):
    page.goto(server + "/#/view/group/deep_series/vol2?p=0")
    # vol2's page 1 is chapter-local "1 / 1"; must land in vol2, not vol1.
    expect(page.locator("#counter")).to_have_text("1 / 1")
    expect(page.locator("#folder-name")).to_contain_text("vol2")


def test_page_turn_into_next_chapter_updates_url(page, server):
    page.goto(server + "/#/view/group/deep_series/vol1?p=0")
    expect(page.locator("#counter")).to_have_text("1 / 2")
    expect(page.locator("#folder-name")).to_contain_text("vol1")
    # Two forward page turns (left tap zone = next page in RTL manga): vol1/p2 → vol2/p1.
    page.locator("#zone-prev").click()
    expect(page.locator("#counter")).to_have_text("2 / 2")
    page.locator("#zone-prev").click()
    expect(page.locator("#counter")).to_have_text("1 / 1")
    expect(page.locator("#folder-name")).to_contain_text("vol2")
    expect(page).to_have_url(re.compile(r"#/view/group/deep_series/vol2\?p=0$"))


# ── T4: login round-trip (sessionStorage bridge) ──────────────────────────────
# The login-side stash script writes {hash, ts} into sessionStorage while the
# login page renders; T2's init restores it (fresh < 10 min, location.hash
# empty) after the OAuth callback redirect to "/" would have dropped the
# fragment. Tests are driven headlessly via add_init_script because test mode
# (BOOKQ_TEST_MODE=true) never renders the login branch itself.


def test_session_storage_hash_restored(page, server):
    page.add_init_script("sessionStorage.setItem('bookq_return_hash', JSON.stringify({hash: '#/view/simple_manga?p=2', ts: Date.now()}));")
    page.goto(server)
    expect(page.locator("#counter")).to_have_text("3 / 5")


def test_stale_stash_discarded(page, server):
    # Older than the 10-minute freshness window: the deep link must NOT restore.
    page.add_init_script("sessionStorage.setItem('bookq_return_hash', JSON.stringify({hash: '#/view/simple_manga?p=2', ts: Date.now() - 11 * 60 * 1000}));")
    page.goto(server)
    expect(page.locator("#empty")).to_have_text("📂 サイドバーからフォルダを選んでください")
    expect(page).to_have_url(re.compile(r"^" + re.escape(server) + r"/?$"))


def test_url_hash_wins_over_stored(page, server):
    # A real URL hash takes precedence over a stored stash (init only restores
    # when location.hash is empty), so the stored simple_manga link is ignored.
    page.add_init_script("sessionStorage.setItem('bookq_return_hash', JSON.stringify({hash: '#/view/simple_manga?p=2', ts: Date.now()}));")
    page.goto(server + "/#/view/long?p=5")
    expect(page.locator("#counter")).to_have_text("6 / 30")
    expect(page).to_have_url(re.compile(r"#/view/long\?p=5$"))


# ── T5: hardening locks ───────────────────────────────────────────────────────
# Spacer-padded chapter deep links (boundaries point past leading spacers, so a
# restore can never land on a spacer) and corrupt-stash-tolerant init.


def test_spacer_padded_chapter_deep_link(landscape_page, server):
    # With spreadOffset=1 (cover-solo shift) a cross-chapter load inserts
    # leading __spacer__ entries at chapter boundaries — but the padding loop
    # does boundaries[c] += pad, so chapterBoundaries always point at each
    # chapter's first REAL page, never a spacer. A chapter-local deep link is
    # therefore pad-independent: ?p=0 into the padded vol2 must land on its
    # real page (never a spacer), and an out-of-range p falls back to 0.
    page = landscape_page
    # Open a book so the spread-shift button has pages to shift (the handler
    # is a no-op with no images loaded).
    page.goto(server + "/#/view/group/deep_series/vol1?p=0")
    expect(page.locator("#counter")).to_have_text("1 / 2")
    # Cover-solo pairing: spreadOffset=1. The subsequent deep-link loadImages
    # re-runs with this offset, so vol2's boundary carries a leading spacer.
    page.locator("#spread-shift").click()
    page.goto(server + "/#/view/group/deep_series/vol2?p=0")
    # vol2's only real page shows chapter-local "1 / 1", folder vol2, URL keeps
    # the chapter-local page.
    expect(page.locator("#counter")).to_have_text("1 / 1")
    expect(page.locator("#folder-name")).to_contain_text("vol2")
    expect(page).to_have_url(re.compile(r"#/view/group/deep_series/vol2\?p=0$"))


def test_corrupt_stash_does_not_break_boot(page, server):
    # A broken bookq_return_hash (invalid JSON) must not stop the viewer
    # from booting: the init parse is try/catch-guarded (pre-fix it threw at
    # init, so loadFolders() never ran and no tiles rendered). The viewer must
    # boot to the top state — sidebar open, folders rendered, no #/view/ hash.
    page.add_init_script("sessionStorage.setItem('bookq_return_hash', '{this is not json');")
    page.goto(server)
    expect(page.locator("#sidebar")).to_have_class(re.compile(r"open"))
    expect(page.locator("#tile-grid .tile").first).to_be_visible()
    assert "#/view/" not in page.url, page.url
