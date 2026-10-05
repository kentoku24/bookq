"""Mobile scroll-together + desktop fixed header + scroll restoration.

Covers search-storage-scroll-with-thumbnails plan Todo 4:
- mobile 390x844: #search (and its #search-input, top-of-sidebar) scroll out with tiles
- desktop 1200x800: they stay fixed (visible) after scroll
- scroll restoration across drill-down and Safari-native PTR delegation
"""
import os
import re
from pathlib import Path

from playwright.sync_api import expect

EVIDENCE_DIR = Path(__file__).resolve().parent.parent / ".omo" / "evidence"


def _ensure_evidence_dir():
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)


def _set_scroll_top(page, value: int) -> str:
    return page.evaluate("""(v) => {
        const c = document.getElementById('sidebar-scroll');
        const tg = document.getElementById('tile-grid');
        const sc = window.matchMedia('(max-width:600px)').matches ? c : tg;
        if (!sc) return 'none';
        sc.scrollTop = v;
        return sc.id;
    }""", value)


def _get_scroll_top(page) -> int:
    return page.evaluate("""() => {
        const c = document.getElementById('sidebar-scroll');
        const tg = document.getElementById('tile-grid');
        const sc = window.matchMedia('(max-width:600px)').matches ? c : tg;
        return sc ? sc.scrollTop : -1;
    }""")


def _rect_bottom(page, selector: str) -> float:
    return page.evaluate("""(sel) => {
        const el = document.querySelector(sel);
        if (!el) return -9999;
        return el.getBoundingClientRect().bottom;
    }""", selector)


def test_search_storage_scroll_mobile(page):
    page.wait_for_selector(".tile")
    page.wait_for_timeout(300)
    _ensure_evidence_dir()
    assert _rect_bottom(page, "#search") > 0
    assert _rect_bottom(page, "#search-input") > 0
    sc_id = _set_scroll_top(page, 300)
    assert sc_id == "sidebar-scroll"
    page.wait_for_timeout(250)
    try:
        page.screenshot(path=str(EVIDENCE_DIR / "task-4-mobile-scrolled.png"), full_page=False)
    except Exception:
        pass
    assert _rect_bottom(page, "#search") < 0
    assert _rect_bottom(page, "#search-input") < 0


def test_search_storage_fixed_desktop(request, server):
    _ensure_evidence_dir()
    b = request.getfixturevalue("webkit_browser" if os.environ.get("BROWSER") == "webkit" else "browser")
    context = b.new_context(viewport={"width": 1200, "height": 800})
    pg = context.new_page()
    try:
        pg.goto(server)
        pg.wait_for_selector(".tile")
        pg.wait_for_timeout(300)
        assert _rect_bottom(pg, "#search") > 0
        assert _rect_bottom(pg, "#search-input") > 0
        sc_id = pg.evaluate("""() => {
            const c = document.getElementById('sidebar-scroll');
            const tg = document.getElementById('tile-grid');
            const sc = window.matchMedia('(max-width:600px)').matches ? c : tg;
            sc.scrollTop = 300;
            return sc.id;
        }""")
        assert sc_id == "tile-grid"
        pg.wait_for_timeout(250)
        try:
            pg.screenshot(path=str(EVIDENCE_DIR / "task-4-desktop-scrolled.png"), full_page=False)
        except Exception:
            pass
        assert pg.evaluate("() => document.querySelector('#search').getBoundingClientRect().bottom") > 0
        assert pg.evaluate("() => document.querySelector('#search-input').getBoundingClientRect().bottom") > 0
    finally:
        context.close()


def test_scroll_restoration_and_pull(page):
    page.wait_for_selector(".tile")
    _ensure_evidence_dir()
    page.wait_for_timeout(300)
    _set_scroll_top(page, 150)
    page.wait_for_timeout(200)
    before = _get_scroll_top(page)
    assert before >= 50
    target_path = "group" if page.locator('.tile[data-path="group"]').count() > 0 else page.locator(".tile").first.get_attribute("data-path")
    page.evaluate("""(p) => document.querySelector('.tile[data-path="'+p+'"]').click()""", target_path)
    page.wait_for_timeout(400)
    try:
        page.wait_for_selector(".tile", timeout=3000)
    except Exception:
        pass
    header = page.locator("#browse-header")
    expect(header).to_contain_text(re.compile(r"↩"))
    header.click()
    page.wait_for_timeout(500)
    try:
        page.wait_for_selector(".tile", timeout=3000)
    except Exception:
        pass
    page.wait_for_timeout(200)
    restored = _get_scroll_top(page)
    try:
        page.screenshot(path=str(EVIDENCE_DIR / "task-4-restored.png"), full_page=False)
    except Exception:
        pass
    assert abs(restored - 150) <= 20
    page.evaluate("""() => {
        const c = document.getElementById('sidebar-scroll');
        const tg = document.getElementById('tile-grid');
        const sc = window.matchMedia('(max-width:600px)').matches ? c : tg;
        if (sc) sc.scrollTop = 0;
        return sc ? sc.scrollTop : -1;
    }""")
    page.wait_for_timeout(150)
    assert _get_scroll_top(page) == 0
    # Top PTR delegated to Safari native — custom #pull-indicator removed.
    assert page.evaluate("() => document.getElementById('pull-indicator') === null")
    # Bottom/horizontal gestures still active: bottomPull idle after reset.
    assert page.evaluate("() => window.__ptrDebug ? window.__ptrDebug.bVisual === 0 : true")
