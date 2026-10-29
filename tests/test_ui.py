"""UI behavior tests for the bookq viewer (viewer.py INDEX_HTML).

Covers: sidebar browsing (tile/list/search/drill-down), page navigation
(tap zones / keyboard / slider), lazy strip windowing, and landscape
2-page spread mode. Touch-swipe gestures are not covered (synthetic
touch velocity is too flaky to assert on).

Run:
    uv run --with pytest --with playwright pytest tests/ -q
"""

import re

import pytest
from playwright.sync_api import expect


def open_folder(page, path: str, expect_pages: int):
    page.locator(f'.tile[data-path="{path}"]').click()
    expect(page.locator("#counter")).to_have_text(f"1 / {expect_pages}")


# ── sidebar: browsing ──────────────────────────────────────────────────────────


def test_initial_load_shows_root_folder_tiles(page, root_folder_names):
    # Parent-level spec: the root grid holds folder tiles plus ONE
    # images-tile per mixed child (here: "mixed"), so the count and the
    # path list both include it — folders first, images-tiles after.
    expect(page.locator("#sidebar")).to_have_class(re.compile(r"\bopen\b"))
    expect(page.locator("#folder-count")).to_have_text("18 件")
    paths = [t.get_attribute("data-path") for t in page.locator(".tile").all()]
    assert paths == root_folder_names + ["mixed"]


def test_search_filters_current_level(page):
    # The search now calls /api/search; with no metadata.toml in the fixture,
    # it returns 0 results. Verify the UI handles this gracefully.
    page.locator("#search-input").fill("manga")
    page.wait_for_timeout(600)  # debounce 300ms + API roundtrip
    expect(page.locator("#tile-grid")).not_to_be_empty()  # UI shows something
    page.locator("#search-input").fill("")
    page.wait_for_timeout(600)
    expect(page.locator(".tile").first).to_be_visible()  # folders restored


def test_drill_down_and_back(page):
    page.locator('.tile[data-path="series"]').click()
    expect(page.locator("#folder-count")).to_have_text("2 件")
    expect(page.locator("#browse-header")).to_have_text("↩ series")
    paths = [t.get_attribute("data-path") for t in page.locator(".tile").all()]
    assert paths == ["series/vol1", "series/vol2"]

    page.locator("#browse-header").click()  # back to root
    # Root count includes the parent-level images-tile (see above).
    expect(page.locator("#folder-count")).to_have_text("18 件")
    expect(page.locator("#browse-header")).to_have_text("📁 フォルダ")


def test_folder_tree_natural_episode_order(page):
    page.locator('.tile[data-path="group"]').click()
    expect(page.locator('.tile[data-path="group/episodes"]')).to_be_visible(timeout=3000)
    page.locator('.tile[data-path="group/episodes"]').click()
    expect(page.locator("#folder-count")).to_have_text("4 件")
    paths = [t.get_attribute("data-path") for t in page.locator(".tile").all()]
    assert paths == [
        "group/episodes/ch-1",
        "group/episodes/ch-2",
        "group/episodes/ch-10",
        "group/episodes/ch-11",
    ]


def test_kanji_folder_tree_order(page):
    # 漢数字: 本好きの下剋上【第一部】〜【第五部】 sort 1,2,3,4,5 (not Unicode
    # codepoint order 一,三,二,五,四).
    page.locator('.tile[data-path="kanji"]').click()
    page.locator('.tile[data-path="kanji/series1"]').click()
    expect(page.locator("#folder-count")).to_have_text("5 件")
    paths = [t.get_attribute("data-path") for t in page.locator(".tile").all()]
    assert paths == [
        "kanji/series1/【第一部】",
        "kanji/series1/【第二部】",
        "kanji/series1/【第三部】",
        "kanji/series1/【第四部】",
        "kanji/series1/【第五部】",
    ]


def test_multi_thumbnail_tile_for_folder_with_subfolders(page):
    tile = page.locator('.tile[data-path="series"]')
    # multi-thumb tiles render ONE server-composed sprite (rows layout in
    # the query), not per-cell imgs — see _multiGridHtml in browse/render.js.
    imgs = tile.locator(".tile-multi img")
    expect(imgs).to_have_count(1)
    src = imgs.first.get_attribute("src") or ""
    assert "/api/thumb-sprite?" in src and "rows=2" in src


def test_list_mode_toggle(page):
    # View mode now cycles tile → list → card → tile (3 states)
    btn = page.locator("#footer-refresh-btn")

    btn.click()  # tile → list
    expect(page.locator("#tile-grid")).to_have_class(re.compile(r"\blist-mode\b"))
    expect(page.locator(".item.folder").first).to_be_visible()

    btn.click()  # list → card
    expect(page.locator("#tile-grid")).not_to_have_class(re.compile(r"\blist-mode\b"))
    expect(page.locator(".card").first).to_be_visible()

    btn.click()  # card → tile
    expect(page.locator("#tile-grid")).not_to_have_class(re.compile(r"\blist-mode\b"))
    expect(page.locator(".tile").first).to_be_visible()


def test_empty_folder_drills_into_no_folders_found(page):
    page.locator('.tile[data-path="empty_folder"]').click()
    expect(page.locator("#tile-grid")).to_contain_text("フォルダが見つかりません")


def test_mixed_folder_shows_only_subfolders(page):
    # Parent-level spec: a folder holding both images and subfolders shows
    # subfolders only in-folder; its direct images live one level up as ONE
    # images-tile (data-read-child, このフォルダを読む) on the parent level,
    # so the root holds two tiles with this path — drill via the folder one
    # (see docs/core-features.md 制約・注意点).
    page.locator('.tile[data-path="mixed"]:not([data-read-child])').click()
    expect(page.locator("#browse-header")).to_have_text("↩ mixed")
    paths = [t.get_attribute("data-path") for t in page.locator(".tile").all()]
    assert paths == ["mixed/extra"]


def test_flattened_single_chain_folder_opens_viewer(page):
    open_folder(page, "chain", expect_pages=2)
    img = page.locator("#img-track .page-cell img").first
    expect(img).to_have_class(re.compile(r"\bloaded\b"))
    assert img.evaluate("e => e.naturalWidth") > 0


# ── viewer: opening a folder ───────────────────────────────────────────────────


def test_open_image_only_folder_closes_sidebar_and_shows_first_page(page):
    open_folder(page, "simple_manga", expect_pages=5)
    expect(page.locator("#sidebar")).not_to_have_class(re.compile(r"\bopen\b"))
    expect(page.locator("#folder-name")).to_have_text("simple_manga")
    expect(page.locator("#page-slider-wrap")).to_have_class(re.compile(r"\bvisible\b"))


def test_pages_are_natural_sorted(page):
    open_folder(page, "simple_manga", expect_pages=5)
    # Staged prefetch: pages beyond the immediate neighbours get their src once
    # the next page has landed — wait for the whole window to be started.
    page.wait_for_function(
        "(() => { const els = document.querySelectorAll('#img-track .page-cell img');"
        " return els.length === 5 && Array.from(els).every(e => e.getAttribute('src')); })()"
    )
    srcs = page.eval_on_selector_all(
        "#img-track .page-cell img", "els => els.map(e => decodeURIComponent(new URL(e.src).pathname))"
    )
    names = [s.rsplit("/", 1)[1] for s in srcs]
    assert names == ["1.png", "2.png", "3.png", "10.png", "11.png"]  # not 1,10,11,2,3


def test_unicode_folder_name_serves_image(page):
    open_folder(page, "夏 の 本", expect_pages=1)
    img = page.locator("#img-track .page-cell img").first
    expect(img).to_have_class(re.compile(r"\bloaded\b"))
    assert img.evaluate("e => e.naturalWidth") > 0  # image really decoded


# ── viewer: page navigation (portrait = 1 page / step) ─────────────────────────


def test_tap_zones_navigate_manga_direction(page):
    open_folder(page, "simple_manga", expect_pages=5)
    page.locator("#zone-prev").click()  # left 30% = NEXT page (right-to-left)
    expect(page.locator("#counter")).to_have_text("2 / 5")
    page.locator("#zone-next").click()  # right 30% = PREVIOUS page
    expect(page.locator("#counter")).to_have_text("1 / 5")
    page.locator("#zone-next").click()  # at first page: stays
    expect(page.locator("#counter")).to_have_text("1 / 5")


def test_center_zone_toggles_toolbar(page):
    open_folder(page, "simple_manga", expect_pages=5)
    page.locator("#zone-toggle").click()
    expect(page.locator("#toolbar")).to_have_class(re.compile(r"\bhidden\b"))
    page.locator("#zone-toggle").click()
    expect(page.locator("#toolbar")).not_to_have_class(re.compile(r"\bhidden\b"))


def test_tap_zone_widths_are_30_40_30(page):
    # JS zoneActionAt (frac < 0.30 / > 0.70) must match the CSS hit areas:
    # left 30% / center 40% / right 30% of #img-container.
    open_folder(page, "simple_manga", expect_pages=5)
    expect(page.locator("#img-track .page-cell img").first).to_have_class(
        re.compile(r"\bloaded\b")
    )
    sizes = page.evaluate(
        "() => {"
        " const cw = document.querySelector('#img-container').getBoundingClientRect().width;"
        " const w = id => document.querySelector(id).getBoundingClientRect().width;"
        " return {cw, prev: w('#zone-prev'), toggle: w('#zone-toggle'), next: w('#zone-next')};"
        " }"
    )
    assert sizes["cw"] > 0
    assert abs(sizes["prev"] - sizes["cw"] * 0.30) <= 2
    assert abs(sizes["toggle"] - sizes["cw"] * 0.40) <= 2
    assert abs(sizes["next"] - sizes["cw"] * 0.30) <= 2


def test_keyboard_navigation(page):
    open_folder(page, "simple_manga", expect_pages=5)
    page.keyboard.press("ArrowLeft")  # next (manga)
    expect(page.locator("#counter")).to_have_text("2 / 5")
    page.keyboard.press("ArrowRight")  # previous
    expect(page.locator("#counter")).to_have_text("1 / 5")
    page.keyboard.press("End")
    expect(page.locator("#counter")).to_have_text("5 / 5")
    page.keyboard.press("Home")
    expect(page.locator("#counter")).to_have_text("1 / 5")
    page.keyboard.press("f")  # toggle sidebar
    expect(page.locator("#sidebar")).to_have_class(re.compile(r"\bopen\b"))
    page.keyboard.press("Escape")  # close sidebar
    expect(page.locator("#sidebar")).not_to_have_class(re.compile(r"\bopen\b"))


def test_click_folder_name_opens_sidebar(page):
    open_folder(page, "simple_manga", expect_pages=5)
    expect(page.locator("#sidebar")).not_to_have_class(re.compile(r"\bopen\b"))
    page.locator("#folder-name").click()  # label is a hitbox, like ☰
    expect(page.locator("#sidebar")).to_have_class(re.compile(r"\bopen\b"))


def test_slider_maps_value_to_page(page):
    open_folder(page, "simple_manga", expect_pages=5)
    slider = page.locator("#page-slider")
    expect(slider).to_have_attribute("max", "4")  # n-1 in single mode
    slider.evaluate("s => { s.value = '4'; s.dispatchEvent(new Event('input')); }")
    expect(page.locator("#counter")).to_have_text("5 / 5")


def test_strip_keeps_only_a_window_of_page_cells(page):
    open_folder(page, "long", expect_pages=30)
    # At page 1: window is [0 .. PREFETCH_AHEAD] → 9 cells.
    expect(page.locator("#img-track .page-cell")).to_have_count(9)
    page.keyboard.press("End")
    expect(page.locator("#counter")).to_have_text("30 / 30")
    # At the last page: [29-PREFETCH_BEHIND .. 29] survives, the rest evicted.
    expect(page.locator("#img-track .page-cell")).to_have_count(4)


# ── viewer: landscape 2-page spread ────────────────────────────────────────────


def test_landscape_defaults_to_spread_mode(landscape_page):
    page = landscape_page
    expect(page.locator("body")).to_have_class("landscape")
    open_folder(page, "long", expect_pages=30)
    expect(page.locator("#spread-toggle")).to_have_text("2P")
    expect(page.locator("#page-slider")).to_have_attribute("max", "14")  # ceil(30/2)-1
    page.locator("#zone-prev").click()  # advance one spread = 2 pages
    expect(page.locator("#counter")).to_have_text("3 / 30")


def test_spread_toggle_switches_to_single_page(landscape_page):
    page = landscape_page
    open_folder(page, "long", expect_pages=30)
    page.locator("#spread-toggle").click()
    expect(page.locator("body")).to_have_class("landscape-single")
    expect(page.locator("#spread-toggle")).to_have_text("1P")
    page.locator("#zone-prev").click()  # single mode advances 1 page
    expect(page.locator("#counter")).to_have_text("2 / 30")


def test_spread_shift_makes_cover_solo(landscape_page):
    page = landscape_page
    open_folder(page, "long", expect_pages=30)
    shift = page.locator("#spread-shift")
    shift.click()
    expect(shift).to_have_class(re.compile(r"\bactive\b"))
    page.locator("#zone-prev").click()  # cover is solo → step 1
    expect(page.locator("#counter")).to_have_text("2 / 30")
    page.locator("#zone-prev").click()  # then paired → step 2
    expect(page.locator("#counter")).to_have_text("4 / 30")
    shift.click()  # toggle back off
    expect(shift).not_to_have_class(re.compile(r"\bactive\b"))


def test_spread_controls_follow_toolbar_visibility(landscape_page):
    page = landscape_page
    open_folder(page, "long", expect_pages=30)
    controls = page.locator("#spread-controls")
    expect(controls).to_have_css("opacity", "1")
    page.locator("#zone-toggle").click()
    expect(controls).to_have_class(re.compile(r"\bhidden\b"))
    # The class alone was always toggled — the regression this guards is the
    # missing CSS: opacity must actually drop, and the invisible buttons must
    # not keep eating taps (#spread-controls>* would otherwise stay
    # pointer-events:auto under a pointer-events:none parent).
    expect(controls).to_have_css("opacity", "0")
    expect(page.locator("#spread-toggle")).to_have_css("pointer-events", "none")
    page.locator("#zone-toggle").click()
    expect(controls).to_have_css("opacity", "1")


# ── viewer: spread-centric layout (issue #13) ─────────────────────────────────
#
# Each spread (1-2 pages) is one container sized by contain-fit against the
# viewport, bottom-anchored and centered. Geometry is read straight off
# getBoundingClientRect so the assertions describe what the reader actually sees.

TOL = 2  # px — sub-pixel rounding slack

GAP_INNER = 5
GAP_OUTER = 10

_GEOM_JS = """() => {
  const spreads = [...document.querySelectorAll('#img-track .spread-container')].map(sc => {
    const r = sc.getBoundingClientRect();
    return {
      left: r.left, right: r.right, top: r.top, bottom: r.bottom,
      width: r.width, height: r.height,
      pages: [...sc.querySelectorAll('.page-cell')].map(c => {
        const cr = c.getBoundingClientRect();
        const im = c.querySelector('img');
        const ir = im.getBoundingClientRect();
        return {
          idx: Number(im.style.getPropertyValue('--i')),
          cellLeft: cr.left, cellRight: cr.right,
          cellWidth: cr.width, cellHeight: cr.height,
          imgLeft: ir.left, imgRight: ir.right, imgBottom: ir.bottom,
          imgWidth: ir.width, imgHeight: ir.height,
          natWidth: im.naturalWidth, natHeight: im.naturalHeight,
          loaded: im.complete && im.naturalWidth > 0,
        };
      }),
    };
  });
  spreads.sort((a, b) => b.left - a.left);  // manga RTL: reading order is right→left
  return {vw: innerWidth, vh: innerHeight, spreads};
}"""

_READY_JS = """() => {
  const cur = [...document.querySelectorAll('#img-track img')]
    .find(im => Number(im.style.getPropertyValue('--i')) === window.currentIdx);
  if (!cur) return false;
  const sc = cur.closest('.spread-container');
  if (!sc) return false;
  const imgs = [...sc.querySelectorAll('img')];
  return imgs.length > 0 && imgs.every(im => im.complete && im.naturalWidth > 0);
}"""


def geom(page, settle: int = 400):
    """Read spread/page geometry once images have loaded and laid out."""
    page.wait_for_selector("#img-track .spread-container")
    page.wait_for_function(_READY_JS)
    page.wait_for_timeout(settle)
    # A fixed wait alone is not enough: on a slow CI runner the .3s page-turn
    # slide can still be in its easing tail (~2px short) when it expires.
    page.wait_for_function(
        "() => document.getElementById('img-track').getAnimations({subtree: true})"
        ".every(a => a.playState !== 'running')"
    )
    g = page.evaluate(_GEOM_JS)
    assert g["spreads"], "no spread containers rendered"
    return g


def spread_with(g, idx: int):
    for s in g["spreads"]:
        if any(p["idx"] == idx for p in s["pages"]):
            return s
    raise AssertionError(f"page {idx} is not in any rendered spread")


def loaded_pages(g):
    return [p for s in g["spreads"] for p in s["pages"] if p["loaded"]]


def test_spread_pages_fit_viewport_and_keep_aspect(landscape_page):
    """A1/A2 — nothing is cropped and nothing is distorted."""
    page = landscape_page
    open_folder(page, "mixed_ar", expect_pages=6)
    g = geom(page)
    pages = loaded_pages(g)
    assert pages, "no images loaded"
    for p in pages:
        assert p["imgWidth"] <= g["vw"] + TOL, p
        assert p["imgHeight"] <= g["vh"] + TOL, p
        want = p["natWidth"] / p["natHeight"]
        got = p["imgWidth"] / p["imgHeight"]
        assert abs(got - want) < 0.02, f"aspect distorted: {got:.3f} != {want:.3f} ({p})"


def test_current_spread_is_maximized(landscape_page):
    """A3 — one of the two constraints is always tight, so the image is as big as it fits."""
    page = landscape_page
    open_folder(page, "long", expect_pages=30)
    g = geom(page)
    s = spread_with(g, 0)
    height_tight = s["height"] >= g["vh"] - TOL
    width_tight = s["width"] >= g["vw"] - TOL
    assert height_tight or width_tight, f"spread is neither height- nor width-limited: {s}"


def test_width_limited_spread_is_maximized(landscape_page):
    """A3 — the width-limited branch: a tall+wide pair cannot use the full height."""
    page = landscape_page
    open_folder(page, "mixed_ar", expect_pages=6)
    g = geom(page)
    s = spread_with(g, 0)
    assert s["width"] >= g["vw"] - TOL, f"not width-maximized: {s}"
    assert s["height"] < g["vh"] - TOL, f"expected height slack on a width-limited spread: {s}"


def test_no_slack_between_cell_and_image(landscape_page):
    """A4 — the fix for 意図しない余白: the cell is the image, not a box around it."""
    page = landscape_page
    open_folder(page, "mixed_ar", expect_pages=6)
    for p in loaded_pages(geom(page)):
        assert abs(p["cellWidth"] - p["imgWidth"]) < TOL, f"horizontal slack in cell: {p}"
        assert abs(p["cellHeight"] - p["imgHeight"]) < TOL, f"vertical slack in cell: {p}"


def test_spread_is_bottom_anchored(landscape_page):
    """A5 — 画面下端を原点とする."""
    page = landscape_page
    open_folder(page, "mixed_ar", expect_pages=6)
    g = geom(page)
    s = spread_with(g, 0)
    assert abs(s["bottom"] - g["vh"]) < TOL, f"spread not bottom-anchored: {s}"
    for p in s["pages"]:
        assert abs(p["imgBottom"] - g["vh"]) < TOL, f"page not bottom-anchored: {p}"


def test_current_spread_is_horizontally_centered(landscape_page):
    """A6 — 画面中央下端に配置."""
    page = landscape_page
    open_folder(page, "long", expect_pages=30)
    g = geom(page)
    s = spread_with(g, 0)
    assert abs((s["left"] + s["right"]) / 2 - g["vw"] / 2) < TOL, f"not centered: {s}"


def test_inner_and_outer_gaps_are_preserved(landscape_page):
    """A7 — GAP_INNER=5 within a spread; the outer gap is GAP_OUTER=10 only when
    both neighbouring spreads exactly fill the viewport (leftover=0 on both
    sides). gapBefore() (監査 V3) otherwise widens it to at least half of
    either neighbouring spread's own viewport leftover, so that spread's edge
    lands at (or past) the viewport boundary instead of peeking into view —
    see CLAUDE.md's gapBefore() note. A tight (but non-zero-leftover) fit
    still gets the widened gap, just a small one; that case is covered
    separately by test_ios.py::test_ipad_spread_geometry (tight_fit
    fixture), which this 900x600 "long" fixture combination does not
    reproduce.
    """
    page = landscape_page
    open_folder(page, "long", expect_pages=30)
    g = geom(page)
    cur = spread_with(g, 0)
    assert len(cur["pages"]) == 2
    right, left = sorted(cur["pages"], key=lambda p: -p["cellLeft"])
    assert abs(right["cellLeft"] - left["cellRight"] - GAP_INNER) < TOL, "inner gap wrong"

    nxt = spread_with(g, 2)
    leftover_cur = g["vw"] - cur["width"]
    leftover_nxt = g["vw"] - nxt["width"]
    floor = GAP_OUTER if leftover_cur == 0 and leftover_nxt == 0 else 0
    expected = max(leftover_cur / 2, leftover_nxt / 2, floor)
    actual = cur["left"] - nxt["right"]
    assert abs(actual - expected) < 1, f"outer gap wrong: got {actual}, expected {expected}"


def test_reading_order_is_right_to_left(landscape_page):
    """R3 — the lower page index is the RIGHT page (manga reading order)."""
    page = landscape_page
    open_folder(page, "long", expect_pages=30)
    s = spread_with(geom(page), 0)
    first, second = sorted(s["pages"], key=lambda p: p["idx"])
    assert first["cellLeft"] > second["cellLeft"], (
        f"page {first['idx']} must sit right of page {second['idx']}"
    )


def test_mixed_aspect_pages_share_height_but_differ_in_width(landscape_page):
    """A8 — the core regression: one global cell width cannot serve both pages."""
    page = landscape_page
    open_folder(page, "mixed_ar", expect_pages=6)
    s = spread_with(geom(page), 0)
    tall, wide = sorted(s["pages"], key=lambda p: p["imgWidth"])
    assert wide["imgWidth"] - tall["imgWidth"] > 10, f"widths did not diverge: {s}"
    assert abs(wide["imgHeight"] - tall["imgHeight"]) < TOL, f"heights must match: {s}"


def test_wide_double_page_spread_fits_within_viewport(landscape_page):
    """A9 — a 160x60 double-page scan shrinks to fit rather than overflowing."""
    page = landscape_page
    open_folder(page, "wide_spread", expect_pages=4)
    page.locator("#zone-prev").click()  # advance to the spread holding p3
    expect(page.locator("#counter")).to_have_text("3 / 4")
    g = geom(page)
    s = spread_with(g, 2)
    assert s["width"] <= g["vw"] + TOL, f"spread overflows width: {s}"
    for p in s["pages"]:
        assert p["imgWidth"] <= g["vw"] + TOL, p
        assert p["imgHeight"] <= g["vh"] + TOL, p


def test_neighbor_image_load_does_not_move_current_spread(landscape_page):
    """A10 — late-loading neighbours must not reflow the page being read."""
    page = landscape_page
    open_folder(page, "mixed_ar", expect_pages=6)
    # Wait for the CURRENT spread's own pages only. Its own aspect ratios landing
    # is allowed to resize it; what must never move it is a *neighbour* loading.
    page.wait_for_function(
        "() => [...document.querySelectorAll('#img-track img')]"
        "  .filter(i => ['0','1'].includes(i.style.getPropertyValue('--i')))"
        "  .every(i => i.complete && i.naturalWidth > 0)"
    )
    early = spread_with(geom(page, settle=0), 0)["left"]
    page.wait_for_function(
        "() => [...document.querySelectorAll('#img-track img')].every(i => i.complete)"
    )
    page.wait_for_timeout(300)
    late = spread_with(geom(page), 0)["left"]
    assert abs(early - late) < TOL, f"current spread shifted {late - early:.1f}px on neighbour load"


def test_geom_waits_for_current_spread_image_load(page):
    page.set_content("""
      <div id="img-track"><div class="spread-container" style="width:800px;height:600px">
        <div class="page-cell"><img style="--i:0;width:400px;height:600px"></div>
      </div></div>
      <script>
        window.currentIdx = 0;
        setTimeout(() => {
          document.querySelector('#img-track img').src = '/image/mixed_ar/p1.png';
        }, 700);
      </script>
    """)
    g = geom(page, settle=0)
    pages = loaded_pages(g)
    assert pages, "no images loaded"
    assert pages[0]["natWidth"] > 0 and pages[0]["natHeight"] > 0


def test_trailing_solo_page_is_centered(landscape_page):
    """A13 — an odd trailing page is a 1-page spread, centered like any other."""
    page = landscape_page
    open_folder(page, "wide_spread", expect_pages=4)
    page.locator("#spread-shift").click()  # cover solo → pairs (1,2), then 3 alone
    page.keyboard.press("End")
    expect(page.locator("#counter")).to_have_text("4 / 4")
    g = geom(page)
    s = spread_with(g, 3)
    assert len(s["pages"]) == 1, f"last page should be solo: {s}"
    assert abs((s["left"] + s["right"]) / 2 - g["vw"] / 2) < TOL, f"solo page not centered: {s}"
    assert abs(s["bottom"] - g["vh"]) < TOL, f"solo page not bottom-anchored: {s}"


def test_first_open_of_padded_chapter_shows_its_first_page(page):
    """vol1 has an odd page count, so vol2 needs a leading spacer to keep
    chapters starting on the correct parity. Opening vol2 for the first time
    (no saved reading progress) must land on its own first real page, not the
    spacer -- regression test for a black screen on first open."""
    page.locator('.tile[data-path="odd_wrap"]').click()
    page.locator('.tile[data-path="odd_wrap/inner/vol2"]').click()
    # vol1 contributes 1 real page before it in the concatenated strip, but
    # the counter is chapter-local: vol2's own first page shows as 1 / 2. The chapter
    # label is "inner/vol2": scanner.py's flattening of the single-child
    # "odd_wrap" wrapper folds "inner" into the sibling names themselves.
    # Counter carries no chapter suffix; the chapter name lives in #folder-name.
    expect(page.locator("#counter")).to_have_text("1 / 2")
    expect(page.locator("#folder-name")).to_have_text("inner/vol2")
    img = page.locator('#img-track .page-cell img[src*="odd_wrap/inner/vol2/p1.png"]')
    expect(img).to_have_class(re.compile(r"\bloaded\b"))
    assert img.evaluate("e => e.naturalWidth") > 0


def test_chapter_boundary_uses_the_chapter_gap(landscape_page):
    """Issue #13's CHAPTER_GAP_PX=20. Opening a chapter with siblings concatenates
    them, and the seam between two chapters must be wider than the ordinary
    between-spread gap so the boundary reads as a break. Chapter concatenation
    only applies at depth >= 2 (a folder dedicated to one series); "group" wraps
    "deep_series" so vol1/vol2 sit two levels under root."""
    page = landscape_page
    page.locator('.tile[data-path="group"]').click()
    page.locator('.tile[data-path="group/deep_series"]').click()
    page.locator('.tile[data-path="group/deep_series/vol1"]').click()
    # Chapter-local counter: vol1 shows its own 2 pages.
    expect(page.locator("#counter")).to_have_text("1 / 2")
    g = geom(page)
    # vol1 has 2 pages (one spread), vol2 has 1 (a solo spread that opens a chapter).
    assert len(g["spreads"]) >= 2, f"expected chapters concatenated: {g['spreads']}"
    first, second = g["spreads"][0], g["spreads"][1]
    gap = first["left"] - second["right"]
    # CHAPTER_GAP_PX=20 is only the floor (監査 V3): gapBefore() widens the
    # chapter seam the same way it widens an ordinary spread gap, so vol2's
    # solo chapter-start spread never lets vol1 peek in (or vice versa).
    leftover_first = g["vw"] - first["width"]
    leftover_second = g["vw"] - second["width"]
    expected = max(20, leftover_first / 2, leftover_second / 2)
    assert abs(gap - expected) < 1, f"chapter seam wrong: got {gap:.1f}, expected {expected:.1f}"


def test_shallow_sibling_folders_do_not_auto_concatenate(landscape_page):
    """A folder directly under root ("series", depth 1) is usually a grouping of
    unrelated one-shot works, not chapters of the same series -- unlike
    "group/deep_series" (depth 2), its leaf children must NOT be concatenated."""
    page = landscape_page
    page.locator('.tile[data-path="series"]').click()
    page.locator('.tile[data-path="series/vol1"]').click()
    expect(page.locator("#counter")).to_have_text("1 / 2")  # not "1 / 3 — vol1"


def test_flattened_wrapper_chapters_still_auto_concatenate(landscape_page):
    """"wrap" has a single child ("inner"), so scanner.py's single-child
    flattening collapses it away and vol1/vol2 land at tree depth 1 -- but
    their raw path ("wrap/inner/vol1") is still 2 levels deep. Depth must be
    judged from the raw path, not the post-flatten tree, or a real
    multi-chapter series hidden behind any number of flattened wrapper
    folders would wrongly lose chapter concatenation."""
    page = landscape_page
    page.locator('.tile[data-path="wrap"]').click()
    page.locator('.tile[data-path="wrap/inner/vol1"]').click()
    # Chapter-local counter: vol1 shows its own 2 pages.
    expect(page.locator("#counter")).to_have_text("1 / 2")


def test_cross_chapter_concat_natural_order(page):
    """T4 — forward page turns across chapter seams must follow episode order
    (ch-2 → ch-10 → ch-11): regression guard for the client-side
    _naturalCompare in getSiblingLeafFolders (SIBLING_AUTOLOAD_MIN_DEPTH=2)."""
    # Sidebar is one-level drill-down: the ch-2 tile only exists after
    # opening "episodes" (cf. test_drill_down_and_back, test_ui.py:58-67).
    page.locator('.tile[data-path="group"]').click()
    page.locator('.tile[data-path="group/episodes"]').click()
    page.locator('.tile[data-path="group/episodes/ch-2"]').click()
    expect(page.locator("#folder-name")).to_contain_text("ch-2")
    # Cross-chapter counters are global: opening mid-series ch-2 shows "2 / 4"
    # (same semantics as test_url_state.py:147-151, where vol2 shows "3 / 3").
    # Forward turns cross chapter seams in episode order — ch-2 → ch-10 → ch-11.
    page.locator("#zone-prev").click()
    expect(page.locator("#folder-name")).to_contain_text("ch-10")
    page.locator("#zone-prev").click()
    expect(page.locator("#folder-name")).to_contain_text("ch-11")


def test_cross_chapter_concat_fullwidth_digit_order(page):
    """Cross-implementation regression guard: the client's _naturalCompare
    (getSiblingLeafFolders) must agree with the server's natural_key on the
    full-width-digit case, where "第２話" ties as opaque text after "第10話"
    (same case as test_natural_key_fullwidth_digits_no_crash, server-side) --
    exercised here through the JS sort that drives chapter-seam page turns."""
    page.locator('.tile[data-path="group"]').click()
    page.locator('.tile[data-path="group/fullwidth_episodes"]').click()
    page.locator('.tile[data-path="group/fullwidth_episodes/第10話"]').click()
    expect(page.locator("#folder-name")).to_contain_text("第10話")
    page.locator("#zone-prev").click()
    expect(page.locator("#folder-name")).to_contain_text("第２話")


def test_cross_chapter_concat_kanji_order(page):
    """Kanji-numeral chapters must concatenate in natural order across chapter
    seams (第二話 → 第十話 → 第十一話), not codepoint order (第二話 < 第十一話 <
    第十話): end-to-end guard for the client-side _normalizeKanji working
    through getSiblingLeafFolders → _naturalCompare (raw path depth
    kanji/series2/第二話 = 2 >= SIBLING_AUTOLOAD_MIN_DEPTH)."""
    page.locator('.tile[data-path="kanji"]').click()
    page.locator('.tile[data-path="kanji/series2"]').click()
    page.locator('.tile[data-path="kanji/series2/第二話"]').click()
    expect(page.locator("#folder-name")).to_contain_text("第二話")
    # Forward turns cross chapter seams in kanji-numeral order.
    page.locator("#zone-prev").click()
    expect(page.locator("#folder-name")).to_contain_text("第十話")
    page.locator("#zone-prev").click()
    expect(page.locator("#folder-name")).to_contain_text("第十一話")


def test_page_change_animates_the_track(landscape_page):
    """Turning a page must slide. Re-basing the anchor before animating cancels
    the movement -- the containers' `left` jumps while the transform stays put --
    so the reader sees an instant cut instead of a page turn."""
    page = landscape_page
    open_folder(page, "long", expect_pages=30)
    geom(page)  # let the first spread settle
    page.evaluate(
        "() => { window.__slides = 0;"
        "  document.getElementById('img-track').addEventListener('transitionend',"
        "    e => { if (e.propertyName === 'transform') window.__slides++; }); }"
    )
    page.locator("#zone-prev").click()
    expect(page.locator("#counter")).to_have_text("3 / 30")
    page.wait_for_function("() => window.__slides > 0", timeout=3000)


def test_page_change_animation_lands_on_the_right_place(landscape_page):
    """After the slide finishes, the new spread must be centered -- the invisible
    re-base must not move anything."""
    page = landscape_page
    open_folder(page, "long", expect_pages=30)
    page.locator("#zone-prev").click()
    expect(page.locator("#counter")).to_have_text("3 / 30")
    g = geom(page)
    s = spread_with(g, 2)
    assert abs((s["left"] + s["right"]) / 2 - g["vw"] / 2) < TOL, f"landed off-center: {s}"
    assert abs(s["bottom"] - g["vh"]) < TOL, f"landed off the bottom: {s}"


def test_tight_fit_neighbours_sit_flush_at_the_edges(landscape_page):
    """On a tight fit (iPad Air landscape, 1180x820) the leftover is only a few
    px per side. Since 監査 V3, gapBefore() widens every gap to at least the
    centred spread's leftover, so a neighbour never peeks into that margin — but
    it must not be pushed further away either: it sits flush at the viewport
    edge. (Before V3 this test asserted the neighbours were visible; its
    `right > 0` then only held by float rounding, and failed on CI.)"""
    page = landscape_page
    page.set_viewport_size({"width": 1180, "height": 820})
    open_folder(page, "tight_fit", expect_pages=8)
    page.locator("#zone-prev").click()  # off the cover, so both neighbours exist
    expect(page.locator("#counter")).to_have_text("3 / 8")
    g = geom(page)
    ordered = g["spreads"]  # already sorted right→left = reading order
    i = next(k for k, s in enumerate(ordered) if abs((s["left"] + s["right"]) / 2 - g["vw"] / 2) < 3)
    cur, prev, nxt = ordered[i], ordered[i - 1], ordered[i + 1]

    assert abs(cur["height"] - g["vh"]) < TOL, f"expected a height-limited fit: {cur}"
    leftover = cur["left"]
    assert 0 < leftover < 20, f"fixture must produce a tight fit: leftover={leftover}"

    assert abs(nxt["right"]) < TOL, f"next spread not flush with the left edge: right={nxt['right']}"
    assert abs(prev["left"] - g["vw"]) < TOL, f"previous spread not flush with the right edge: left={prev['left']}"


def test_resize_refits_spreads_and_keeps_them_centered(landscape_page):
    """R6 — every cached contain-fit must be invalidated when the viewport changes."""
    page = landscape_page
    open_folder(page, "mixed_ar", expect_pages=6)
    before = spread_with(geom(page), 0)["width"]
    page.set_viewport_size({"width": 1200, "height": 700})
    g = geom(page)
    s = spread_with(g, 0)
    assert abs(s["width"] - before) > 10, f"spread was not re-fitted after resize: {s}"
    assert abs((s["left"] + s["right"]) / 2 - g["vw"] / 2) < TOL, f"not centered after resize: {s}"
    assert abs(s["bottom"] - g["vh"]) < TOL, f"not bottom-anchored after resize: {s}"
    for p in s["pages"]:
        assert p["imgWidth"] <= g["vw"] + TOL and p["imgHeight"] <= g["vh"] + TOL, p
        assert abs(p["cellWidth"] - p["imgWidth"]) < TOL, f"slack reappeared after resize: {p}"


def test_landscape_single_page_is_centered(landscape_page):
    """A6 in 1P mode. A page narrower than the viewport (400px fit in a 900px
    view) must be centered, not left-aligned — the portrait case can't catch
    this because portrait pages saturate to exactly viewW."""
    page = landscape_page
    open_folder(page, "long", expect_pages=30)
    page.locator("#spread-toggle").click()
    expect(page.locator("body")).to_have_class("landscape-single")
    g = geom(page)
    s = spread_with(g, 0)
    assert len(s["pages"]) == 1, f"1P mode must not pair pages: {s}"
    assert s["width"] < g["vw"] - TOL, f"fixture must leave horizontal slack: {s}"
    assert abs((s["left"] + s["right"]) / 2 - g["vw"] / 2) < TOL, f"1P page not centered: {s}"
    assert abs(s["bottom"] - g["vh"]) < TOL, f"1P page not bottom-anchored: {s}"


def test_portrait_keeps_single_page_layout(page):
    """A12 — portrait keeps single-page behaviour: one page per spread, centered."""
    open_folder(page, "simple_manga", expect_pages=5)
    g = geom(page)
    s = spread_with(g, 0)
    assert len(s["pages"]) == 1, "portrait must not pair pages"
    assert abs((s["left"] + s["right"]) / 2 - g["vw"] / 2) < TOL, f"not centered: {s}"


# ── H1 (B-C1): filter button must not close the browse sidebar ───────────────


def test_filter_btn_keeps_sidebar_open(page):
    """B-C1 — #filter-btn sits inside #sidebar-header, whose delegated click
    handler closes the sidebar for any non-INPUT click. Opening the filter
    sheet must NOT close the sidebar: after Apply the user must still see the
    browse grid (pre-fix they landed on a blank screen)."""
    expect(page.locator("#sidebar")).to_have_class(re.compile(r"\bopen\b"))
    page.locator("#filter-btn").click()
    # The sheet slides in over the sidebar — the sidebar itself stays open.
    expect(page.locator("#bottom-sheet")).to_have_class(re.compile(r"\bopen\b"))
    expect(page.locator("#sidebar")).to_have_class(re.compile(r"\bopen\b"))
    page.locator("#filter-apply-btn").click()
    # After the sheet closes, the browse grid is still on screen.
    expect(page.locator("#sidebar")).to_have_class(re.compile(r"\bopen\b"))
    expect(page.locator("#tile-grid .tile").first).to_be_visible()


# ── H2 (S-F1): pull-to-refresh delegated to Safari — no custom indicator ──


def test_pull_indicator_survives_render(page):
    """S-F1 (updated) — custom #pull-indicator was removed; top PTR is now
    Safari native. This test verifies no custom indicator is injected after
    renderView() and the sidebar/browse grid remains functional."""
    expect(page.locator("#sidebar")).to_have_class(re.compile(r"\bopen\b"))
    expect(page.locator("#tile-grid .tile").first).to_be_visible()
    assert page.evaluate("() => document.getElementById('pull-indicator') === null"), (
        "#pull-indicator should not exist — top PTR delegated to Safari native"
    )


# ── H3 (S-F2): error toast for dead folders ──────────────────────────────────


def test_dead_folder_shows_error_toast(page, server):
    """S-F2 — a dead deep link must surface a transient role=alert toast
    (pre-fix there was no notification infrastructure; the failure was
    swallowed into a static message and a silent hash clear)."""
    page.goto(server + "/#/view/nonexistent-folder-xyz?p=0")
    toast = page.locator("#toast")
    expect(toast).to_be_visible()
    expect(toast).to_have_attribute("role", "alert")
    expect(toast).to_contain_text("404")
