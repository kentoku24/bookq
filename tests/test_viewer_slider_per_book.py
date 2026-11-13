"""Per-book (chapter-local) slider behavior (viewer-slider-per-book Task 9).

Proves the T1-T8 wiring: the page slider's ``max``/``value`` and the counter
are scoped to the opened chapter, slider input / swipe / keyboard clamp inside
the chapter, and only the tap zones (``goNext``/``goPrev`` with
``crossChapter:true``) cross into the sibling book.

Synthetic sibling tree (raw depth 3, so sibling autoload concatenates it —
``getSiblingLeafFolders`` requires ``fullPath.split("/").length - 1 >= 2``;
``slidergroup`` keeps two children so the scanner never flattens the path):

    slidergroup/sliderseries/chA/   p1.png .. p5.png   (5 pages)
    slidergroup/sliderseries/chB/   p1.png .. p3.png   (3 pages)
    slidergroup/other/              1.png              (flatten guard)

Viewport is portrait (390x844) so the viewer stays in single-page mode, where
one spread == one page and chapter-local ``max`` == chapter pages - 1.

Run:
    uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_viewer_slider_per_book.py -q
"""

import struct
import zlib
from pathlib import Path

import pytest
from playwright.sync_api import expect


def _make_png(path: Path, w: int = 40, h: int = 60, rgb: tuple = (200, 60, 60)) -> None:
    """Minimal solid-color RGB PNG without external deps (mirrors conftest)."""

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


@pytest.fixture(scope="module")
def slider_tree(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("slider_downloads")
    for ch, count in [("chA", 5), ("chB", 3)]:
        d = root / "slidergroup" / "sliderseries" / ch
        d.mkdir(parents=True)
        for i in range(1, count + 1):
            _make_png(d / f"p{i}.png")
    other = root / "slidergroup" / "other"
    other.mkdir(parents=True)
    _make_png(other / "1.png")
    return root


@pytest.fixture(scope="module")
def slider_server(slider_tree: Path) -> str:
    """Test-mode server against the slider tree (mirrors conftest.server).

    Respects BOOKQ_TEST_SERVER=rs via tests.helpers.spawn_test_server.
    """
    from tests.helpers import (
        ViewerRsUnavailable,
        spawn_test_server,
        terminate_test_server,
    )

    try:
        proc, base_url = spawn_test_server(slider_tree)
    except ViewerRsUnavailable as exc:
        pytest.skip(f"viewer-rs unavailable: {exc}")

    yield base_url

    terminate_test_server(proc)


@pytest.fixture
def slider_page(browser, slider_server: str):
    """Portrait viewport (single-page mode) on the slider tree server."""
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    page.goto(slider_server)
    page.wait_for_selector(".tile")  # folder tree fetched and rendered
    yield page
    context.close()


def test_slider_is_chapter_local_across_sibling_books(slider_page, slider_server: str):
    page = slider_page
    page.goto(slider_server + "/#/view/slidergroup/sliderseries/chA?p=0")

    # 1. Initial state: slider max == chA spread count - 1 (single mode: 5-1),
    #    counter chapter-local at 1 / 5.
    expect(page.locator("#counter")).to_have_text("1 / 5")
    expect(page.locator("#folder-name")).to_contain_text("chA")
    expect(page.locator("#page-slider")).to_have_attribute("max", "4")

    # 2. Slider dragged to its max stays inside chA (no chapter crossing).
    page.locator("#page-slider").evaluate("s => { s.value = s.max; s.dispatchEvent(new Event('input')); }")
    expect(page.locator("#counter")).to_have_text("5 / 5")
    expect(page.locator("#folder-name")).to_contain_text("chA")
    expect(page.locator("#page-slider")).to_have_attribute("max", "4")
    # Drop slider focus so subsequent End/Home/Arrow keys hit the viewer
    # key handler, not the native range-input key bindings.
    page.evaluate("document.activeElement && document.activeElement.blur()")

    # 3. Tapping the next-book zone (left zone = NEXT in RTL manga) crosses
    #    into chB: counter resets toward 1 / 3 and max becomes 3 - 1.
    page.locator("#zone-prev").click()
    expect(page.locator("#counter")).to_have_text("1 / 3")
    expect(page.locator("#folder-name")).to_contain_text("chB")
    expect(page.locator("#page-slider")).to_have_attribute("max", "2")

    # 4. End / ArrowLeft clamp inside chB; Home returns to chB's first page.
    page.keyboard.press("End")
    expect(page.locator("#counter")).to_have_text("3 / 3")
    expect(page.locator("#folder-name")).to_contain_text("chB")
    page.keyboard.press("ArrowLeft")  # goNext direction at chapter end: no crossing
    expect(page.locator("#counter")).to_have_text("3 / 3")
    expect(page.locator("#folder-name")).to_contain_text("chB")
    page.keyboard.press("Home")
    expect(page.locator("#counter")).to_have_text("1 / 3")
    expect(page.locator("#folder-name")).to_contain_text("chB")
    # Backward clamp at the chapter head does not cross back into chA either.
    page.keyboard.press("ArrowRight")  # goPrev direction at chapter head: stays
    expect(page.locator("#counter")).to_have_text("1 / 3")
    expect(page.locator("#folder-name")).to_contain_text("chB")
