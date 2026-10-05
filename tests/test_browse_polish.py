"""本棚磨き込み（監査 B2/B3/B5/O2）。"""
import os
import re

from playwright.sync_api import expect


def test_flattened_tile_shows_leaf_and_parent(page, server):
    # "wrap" has a single child ("inner"), so scanner.py flattens it away and
    # its grandchildren (vol1/vol2) surface as tiles named "inner/vol1" etc.
    # once you drill into "wrap" -- see conftest.py's downloads_tree docstring.
    # There is no standalone "wrap/inner" tile; the flattened name only shows
    # up on the leaf tiles themselves.
    page.goto(server + "/")
    page.locator('#tile-grid .tile[data-path="wrap"]').click()
    tile = page.locator('#tile-grid .tile[data-path="wrap/inner/vol1"]')
    expect(tile.locator(".tile-name-text")).to_have_text("vol1")
    expect(tile.locator(".tile-name-parent")).to_have_text("inner")


def test_plain_tile_has_no_parent_line(page, server):
    page.goto(server + "/")
    tile = page.locator('#tile-grid .tile[data-path="simple_manga"]')
    expect(tile.locator(".tile-name-text")).to_have_text("simple_manga")
    expect(tile.locator(".tile-name-parent")).to_have_count(0)


def test_split_display_name_unit(page, server):
    page.goto(server + "/")
    page.wait_for_selector("#tile-grid .tile")
    assert page.evaluate("window.BrowseRender.splitDisplayName('a/b/c')") == {"leaf": "c", "parent": "a/b"}
    assert page.evaluate("window.BrowseRender.splitDisplayName('x')") == {"leaf": "x", "parent": ""}


def _make_progress(page, folder, pg=3):
    page.evaluate(
        "([f,p]) => localStorage.setItem('bookq_progress', JSON.stringify({[f]:{page:p,total:30,ts:Date.now()}}))",
        [folder, pg],
    )


def test_resume_pill_text_and_middle_truncation(page, server):
    page.goto(server + "/")
    _make_progress(page, "long")
    page.reload()
    expect(page.locator("#resume-btn-text")).to_have_text("📖 続きから: long p.4")
    assert page.evaluate("window.__BOOKQ_DEBUG.truncateMiddle('abcdefghijklmnopqrstuvwxyz0123', 24)") == "abcdefghijkl" + "…" + "wxyz0123"


def test_resume_pill_does_not_cover_last_tile(page, server):
    page.goto(server + "/")
    _make_progress(page, "long")
    page.reload()
    expect(page.locator("#resume-btn")).to_be_visible()
    page.evaluate("document.getElementById('sidebar-scroll').scrollTop = 1e9")
    page.wait_for_timeout(200)
    r = page.evaluate(
        "() => { const tiles=[...document.querySelectorAll('#tile-grid .tile')]; const last=tiles[tiles.length-1].getBoundingClientRect(); const pill=document.getElementById('resume-btn').getBoundingClientRect(); return {lastBottom:last.bottom, pillTop:pill.top}; }"
    )
    assert r["lastBottom"] <= r["pillTop"], r


def test_storage_text_in_footer_with_free_space(page, server):
    page.goto(server + "/")
    page.wait_for_selector("#tile-grid .tile")
    expect(page.locator("#storage-bar")).to_have_count(0)
    t = page.locator("#sidebar-footer #storage-text")
    if os.environ.get("BROWSER") == "webkit":
        # WebKit's 0/0 storage estimate fallback has no quota, so only the
        # usage half of the string ("💾 <usage>") is ever rendered.
        expect(t).to_have_text(re.compile(r"^💾 "))
    else:
        expect(t).to_have_text(re.compile(r"^💾 [\d.]+ [KMG]?B / 空き [\d.]+ [KMG]?B$"))


def test_truncate_middle_is_grapheme_safe(page, server):
    page.goto(server + "/")
    page.wait_for_selector("#tile-grid .tile")
    s = "a" * 11 + "😀" + "b" * 20
    result = page.evaluate("(s) => window.__BOOKQ_DEBUG.truncateMiddle(s)", s)
    assert not any(0xD800 <= ord(c) <= 0xDFFF for c in result), result
    assert result == "a" * 11 + "😀" + "…" + "b" * 8, result
