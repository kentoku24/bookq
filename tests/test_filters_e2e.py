"""E2E tests for the filter facet sheet + in-place browse filtering.

The downloads tree is deliberately NESTED (metadata.toml only at depth 2-3):
the current metadata index scans only depth 1 (app/metadata/index.py), so
`/api/metadata/facets` is EMPTY against this tree and the sheet shows zero
chips. Every test in this file is therefore RED against the current code and
turns green only after the recursive-index + subtree-facets work lands.

Expected RED failures (current code):
- tests 1-2: `expect(... .chip).to_have_count(3|5)` times out on 0 chips
  (the sheet falls back to the "No artists" placeholder span, no .chip).
- tests 3-4: the atelier-chip locator click times out (no chip exists).

Run:
    uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_filters_e2e.py -q
"""

import os
import struct
import zlib
from pathlib import Path

import pytest
from playwright.sync_api import expect

REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_png(path: Path, w: int = 40, h: int = 60, rgb: tuple = (200, 60, 60)) -> None:
    """Write a minimal solid-color RGB PNG without external deps."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(rgb) * w
    idat = zlib.compress(row * h)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
    )


def _start_server(downloads: Path):
    """Boot a test-mode server against *downloads*; yield base URL.

    Respects BOOKQ_TEST_SERVER=rs via tests.helpers.spawn_test_server.
    """
    from tests.helpers import (
        ViewerRsUnavailable,
        spawn_test_server,
        terminate_test_server,
    )

    try:
        proc, base_url = spawn_test_server(downloads)
    except ViewerRsUnavailable as exc:
        pytest.skip(f"viewer-rs unavailable: {exc}")
    try:
        yield base_url
    finally:
        terminate_test_server(proc)


def _metadata_toml(source: str, artist: str, title_jp: str = "テスト作品") -> str:
    """Minimal parseable metadata.toml (shape mirrors tests/test_ui_logic.py:419-432)."""
    return (
        "[source]\n"
        f'name = "{source}"\n'
        "\n"
        "[comic]\n"
        f'title_jp = "{title_jp}"\n'
        "\n"
        "[tags]\n"
        f'artist = ["{artist}"]\n'
    )


@pytest.fixture(scope="module")
def facet_downloads(tmp_path_factory) -> Path:
    """Dedicated NESTED metadata tree for the filter-facet tests.

    Anti-flattening shape — scan_tree collapses a folder with no images and
    exactly ONE subdir (app/scanner.py:129-134). Every artist/series folder
    that must survive as a browse node therefore gets TWO subfolders (the
    "other" trick from tests/test_ui_logic.py:412). example-artist and the sample_library
    chain are DELIBERATELY single-child: their sub-chains (example-artist/g2,
    sample_library/シリーズ/ch-1) collapse into image leaves, so the artist inside
    reaches the browse node ONLY via subtree aggregation — the load-bearing
    case the whole subtree_facets design exists for.

    Current index scans only depth 1, so against this tree /api/metadata/facets
    is EMPTY (the tomls live at depth 2-3) — that is the RED probe.
    """
    root = tmp_path_factory.mktemp("facet_downloads")

    def gallery(rel: str, toml: str) -> None:
        folder = root / rel
        folder.mkdir(parents=True, exist_ok=True)
        _make_png(folder / "1.png")
        (folder / "metadata.toml").write_text(toml)

    # COMEDY/atelier-sun — two galleries: survives flattening AND provides the
    # drill-down level (COMEDY/atelier-sun/g1, COMEDY/atelier-sun/g2).
    gallery(
        "COMEDY/atelier-sun/g1",
        "[source]\n"
        'name = "generic"\n'
        "\n"
        "[comic]\n"
        'title_jp = "DEADLINE SKETCHBOOK"\n'
        "\n"
        "[tags]\n"
        'artist = ["atelier-sun"]\n'
        'tag = ["full color"]\n',
    )
    gallery("COMEDY/atelier-sun/g2", _metadata_toml("generic", "atelier-sun"))
    # COMEDY/example-artist — single gallery: COMEDY has 3 subdirs so example-artist
    # survives as a folder node, but its single-child sub-chain (g2) flattens
    # into an image leaf; "example-artist" reaches the browse node only via subtree_facets.
    gallery("COMEDY/example-artist/g2", _metadata_toml("generic", "example-artist"))
    # COMEDY/standalone — depth-1 work directly under COMEDY.
    gallery("COMEDY/standalone", _metadata_toml("generic", "other artist"))
    # sample_library/シリーズ/ch-1 — nested single-child chain: sample_library survives as a
    # folder node at root, its chain (シリーズ → ch-1) flattens into an image leaf.
    gallery("sample_library/シリーズ/ch-1", _metadata_toml("generic", "sample artist"))
    # COMEDY2/x — prefix-boundary sibling (must never leak into COMEDY's facets).
    gallery("COMEDY2/x", _metadata_toml("generic", "annex artist"))
    return root


@pytest.fixture(scope="module")
def facet_server(facet_downloads: Path):
    yield from _start_server(facet_downloads)


@pytest.fixture(scope="module")
def facet_page(request, facet_server):
    """A fresh browser page (portrait viewport) against the nested tree."""
    b = request.getfixturevalue("webkit_browser" if os.environ.get("BROWSER") == "webkit" else "browser")
    context = b.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    page.goto(facet_server)
    page.wait_for_selector(".tile")  # folder tree fetched and rendered
    yield page
    context.close()


def _goto_root(page, base_url: str) -> None:
    """Fresh load at the root browse level (clears any browse/filter state)."""
    page.goto(base_url)
    page.wait_for_selector(".tile")


def test_filter_sheet_lists_subtree_artists(facet_page, facet_server):
    """The sheet at COMEDY level lists only COMEDY-subtree artists (3), sorted casefold."""
    page = facet_page
    _goto_root(page, facet_server)
    page.click('.tile[data-path="COMEDY"]')
    expect(page.locator('.tile[data-path="COMEDY/atelier-sun"]')).to_have_count(1)

    page.click("#filter-btn")
    artists = page.locator("#facet-artists .chip")
    expect(artists).to_have_count(3)
    chip_texts = artists.all_inner_texts()
    assert chip_texts == ["atelier-sun", "example-artist", "other artist"]
    assert "sample artist" not in chip_texts
    assert "annex artist" not in chip_texts


def test_filter_sheet_at_root_lists_all_artists(facet_page, facet_server):
    """The sheet at the root lists every artist in the library (5)."""
    page = facet_page
    _goto_root(page, facet_server)

    page.click("#filter-btn")
    artists = page.locator("#facet-artists .chip")
    expect(artists).to_have_count(5)
    assert artists.all_inner_texts() == [
        "annex artist",
        "atelier-sun",
        "example-artist",
        "other artist",
        "sample artist",
    ]


def test_filter_sheet_in_search_mode_lists_all_artists(facet_page, facet_server):
    """Search mode: the sheet lists whole-library options, not the stale browse subtree.

    currentBrowsePath stays at COMEDY after drilling down, but /api/search applies
    filters library-wide — so opening the sheet mid-search must list all 5
    artists, not just COMEDY's 3.
    """
    page = facet_page
    _goto_root(page, facet_server)
    page.click('.tile[data-path="COMEDY"]')
    expect(page.locator('.tile[data-path="COMEDY/atelier-sun"]')).to_have_count(1)

    page.locator("#search-input").fill("dead")
    page.wait_for_timeout(600)  # 300 ms debounce + fetch

    page.click("#filter-btn")
    artists = page.locator("#facet-artists .chip")
    expect(artists).to_have_count(5)
    assert artists.all_inner_texts() == [
        "annex artist",
        "atelier-sun",
        "example-artist",
        "other artist",
        "sample artist",
    ]


def test_apply_artist_filter_filters_browse_in_place(facet_page, facet_server):
    """Applying a filter narrows the browse listing in place, at every level."""
    page = facet_page
    _goto_root(page, facet_server)
    page.click('.tile[data-path="COMEDY"]')
    # unfiltered COMEDY level: atelier-sun / example-artist (flattened) / standalone
    expect(page.locator(".tile")).to_have_count(3)

    page.click("#filter-btn")
    page.locator("#facet-artists .chip", has_text="atelier-sun").click()
    page.click("#filter-apply-btn")
    # only the matching folder survives; the counter reflects the filtered set
    expect(page.locator(".tile")).to_have_count(1)
    assert page.locator('.tile[data-path="COMEDY/atelier-sun"]').count() == 1
    assert page.locator("#folder-count").inner_text() == "1 件"
    # drill into the surviving folder — filtering continues at depth 2
    page.click('.tile[data-path="COMEDY/atelier-sun"]')
    expect(page.locator(".tile")).to_have_count(2)
    assert page.locator('.tile[data-path="COMEDY/atelier-sun/g1"]').count() == 1
    assert page.locator('.tile[data-path="COMEDY/atelier-sun/g2"]').count() == 1


def test_filter_reset_and_chip_remove_restore_browse(facet_page, facet_server):
    """chip-remove and sheet Reset both restore the unfiltered listing."""
    page = facet_page
    _goto_root(page, facet_server)
    page.click('.tile[data-path="COMEDY"]')
    expect(page.locator(".tile")).to_have_count(3)

    def apply_atelier() -> None:
        page.click("#filter-btn")
        page.locator("#facet-artists .chip", has_text="atelier-sun").click()
        page.click("#filter-apply-btn")
        expect(page.locator(".tile")).to_have_count(1)

    # apply → active-filter chip shown in the sidebar header
    apply_atelier()
    expect(page.locator("#filter-chips")).to_be_visible()
    expect(page.locator("#filter-chips")).to_contain_text("atelier-sun")

    # chip-remove → full listing restored, chips hidden
    page.click("#filter-chips .chip-remove")
    expect(page.locator(".tile")).to_have_count(3)
    expect(page.locator("#filter-chips")).to_be_hidden()

    # apply again, then reset from the sheet → full listing restored
    apply_atelier()
    page.click("#filter-btn")
    page.click("#filter-reset-btn")
    page.click("#bottom-sheet-close")
    expect(page.locator(".tile")).to_have_count(3)
