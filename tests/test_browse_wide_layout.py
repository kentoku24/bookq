"""幅広 (1400px〜) で 1 行に収まる件数だけ中央寄せ+拡大する (#199)。"""

from playwright.sync_api import expect

TOL = 1.5


def _grid_and_tiles(page, selector):
    return page.evaluate(
        """(sel) => {
          const grid = document.getElementById('tile-grid');
          const gr = grid.getBoundingClientRect();
          const items = [...grid.querySelectorAll(sel)].map((e) => {
            const r = e.getBoundingClientRect();
            return {left: r.left, right: r.right, top: r.top, width: r.width};
          });
          return {
            grid: {left: gr.left, right: gr.right, width: gr.width},
            items,
          };
        }""",
        selector,
    )


def test_wide_sparse_tiles_are_centered_and_enlarged(landscape_page, server):
    page = landscape_page
    page.set_viewport_size({"width": 1920, "height": 1200})
    page.goto(server + "/#/browse/group")
    expect(page.locator("#tile-grid > .tile")).to_have_count(4)
    d = _grid_and_tiles(page, "#tile-grid > .tile")
    tiles = d["items"]
    assert len(tiles) == 4
    for t in tiles:
        assert abs(t["width"] - 240) <= TOL, f"tile width not 240: {t}"
    tops = {round(t["top"]) for t in tiles}
    assert len(tops) == 1, f"not a single row: {tiles}"
    left_margin = tiles[0]["left"] - d["grid"]["left"]
    right_margin = d["grid"]["right"] - tiles[-1]["right"]
    assert abs(left_margin - right_margin) <= TOL, (
        f"not centered: left={left_margin:.1f} right={right_margin:.1f}"
    )


def test_wide_full_rows_keep_ten_columns(landscape_page, server, root_folder_names):
    page = landscape_page
    page.set_viewport_size({"width": 1920, "height": 1200})
    page.goto(server + "/#/browse/")
    # Parent-level spec: root grid holds folder tiles plus ONE images-tile
    # per mixed child ("mixed"), so one more tile than scan_tree names.
    expect(page.locator("#tile-grid > .tile")).to_have_count(len(root_folder_names) + 1)
    assert len(root_folder_names) > 10, f"fixture root must exceed 10: {root_folder_names}"
    d = _grid_and_tiles(page, "#tile-grid > .tile")
    tiles = d["items"]
    first_top = round(tiles[0]["top"])
    first_row = [t for t in tiles if round(t["top"]) == first_top]
    assert len(first_row) == 10, f"first row not 10 cols: {len(first_row)}"
    assert abs(first_row[0]["left"] - (d["grid"]["left"] + 8)) <= TOL, (
        f"first tile not at grid+8: {first_row[0]} grid={d['grid']}"
    )
    # 複数行の最終行は左詰めのまま: 最終行の先頭が 1 行目の先頭と同じ x
    last_top = round(tiles[-1]["top"])
    last_row_first = next(t for t in tiles if round(t["top"]) == last_top)
    assert abs(last_row_first["left"] - first_row[0]["left"]) <= TOL, (
        f"last row not left-aligned: {last_row_first} vs {first_row[0]}"
    )


def test_wide_sparse_cards_are_centered(landscape_page, server):
    page = landscape_page
    page.set_viewport_size({"width": 1920, "height": 1200})
    page.goto(server + "/#/browse/series?v=card")
    expect(page.locator("#tile-grid > .card")).to_have_count(2)
    d = _grid_and_tiles(page, "#tile-grid > .card")
    cards = d["items"]
    assert len(cards) == 2
    for c in cards:
        assert abs(c["width"] - 480) <= TOL, f"card width not 480: {c}"
    left_margin = cards[0]["left"] - d["grid"]["left"]
    right_margin = d["grid"]["right"] - cards[-1]["right"]
    assert abs(left_margin - right_margin) <= TOL, (
        f"not centered: left={left_margin:.1f} right={right_margin:.1f}"
    )


def test_wide_full_card_row_is_unchanged(landscape_page, server):
    page = landscape_page
    page.set_viewport_size({"width": 1920, "height": 1200})
    page.goto(server + "/#/browse/group?v=card")
    expect(page.locator("#tile-grid > .card")).to_have_count(4)
    d = _grid_and_tiles(page, "#tile-grid > .card")
    cards = d["items"]
    assert len(cards) == 4
    tops = {round(c["top"]) for c in cards}
    assert len(tops) == 1, f"4 cards must stay on one row: {cards}"
    assert abs(cards[0]["left"] - (d["grid"]["left"] + 12)) <= TOL, (
        f"first card not at grid+12: {cards[0]} grid={d['grid']}"
    )


def test_below_1400_sparse_tiles_stay_left_aligned(landscape_page, server):
    page = landscape_page
    page.set_viewport_size({"width": 1399, "height": 900})
    page.goto(server + "/#/browse/group")
    expect(page.locator("#tile-grid > .tile")).to_have_count(4)
    d = _grid_and_tiles(page, "#tile-grid > .tile")
    tiles = d["items"]
    assert len(tiles) == 4
    assert abs(tiles[0]["left"] - (d["grid"]["left"] + 8)) <= TOL, (
        f"first tile not at grid+8: {tiles[0]} grid={d['grid']}"
    )
    right_margin = d["grid"]["right"] - tiles[-1]["right"]
    assert right_margin > 500, f"right margin should stay wide: {right_margin:.1f}"
