"""Rapid tap-zone bursts must land pages immediately, not smear behind a
restarting 300ms slide.

Measured before the fix (Chromium, 15 taps at 70ms): the visual strip sat a
constant 0.8 page behind `currentIdx`, only ONE transitionend fired for the
whole burst, and the strip kept sliding ~250ms after the last tap. Root cause:
every `snapTo(idx, animate=true)` restarted the .3s CSS transition from the
current mid-flight position. The fix cuts instantly (the existing
`animate=false` path) whenever a turn arrives while a slide is in flight, and
keeps the single-tap slide untouched.
"""
from playwright.sync_api import expect

FOLDER = "long"  # 30 pages, single chapter


def _visual_vs_target_lag_pages(page):
    return page.evaluate(
        """() => {
          const tr = getComputedStyle(document.getElementById('img-track')).transform;
          const m = tr.match(/matrix\\(([^)]+)\\)/);
          const vis = m ? parseFloat(m[1].split(',')[4]) : 0;
          const target = window.SpreadView.pageOffsetPx(window.currentIdx);
          return Math.abs(target - vis) / (window.viewW || window.innerWidth);
        }"""
    )


def test_burst_of_taps_lands_each_page_without_trailing_slide(ios_page, server):
    page = ios_page
    page.goto(f"{server}/#/view/{FOLDER}?p=0")
    expect(page.locator("#counter")).to_have_text("1 / 30")
    page.wait_for_timeout(300)
    page.evaluate(
        """() => { window.__te = 0; document.getElementById('img-track').addEventListener('transitionend',
             e => { if (e.target.id === 'img-track' && e.propertyName === 'transform') window.__te++; }); }"""
    )
    taps = 8
    for _ in range(taps):
        page.touchscreen.tap(60, 500)
        page.wait_for_timeout(60)
    # Right after the last tap the strip must already sit on the target page:
    # no trailing slide, no accumulated lag.
    assert page.evaluate("window.currentIdx") == taps
    page.wait_for_timeout(40)
    assert _visual_vs_target_lag_pages(page) < 0.05
    assert page.evaluate("window.sliding") is False
    expect(page.locator("#counter")).to_have_text(f"{taps + 1} / 30")


def test_single_tap_still_slides(ios_page, server):
    page = ios_page
    page.goto(f"{server}/#/view/{FOLDER}?p=0")
    expect(page.locator("#counter")).to_have_text("1 / 30")
    page.wait_for_timeout(300)
    page.evaluate(
        """() => { window.__te = 0; document.getElementById('img-track').addEventListener('transitionend',
             e => { if (e.target.id === 'img-track' && e.propertyName === 'transform') window.__te++; }); }"""
    )
    page.touchscreen.tap(60, 500)
    page.wait_for_function("window.sliding === true")
    tr = page.evaluate("document.getElementById('img-track').style.transition")
    assert "transform" in tr and ".3s" in tr
    page.wait_for_function("window.__te === 1 && window.sliding === false")
    expect(page.locator("#counter")).to_have_text("2 / 30")


def test_instant_turn_during_slide_clears_sliding_flag(ios_page, server):
    """`sliding` must not stay true after an instant snap supersedes a slide
    (the superseded settle() is gen-guarded and never clears it)."""
    page = ios_page
    page.goto(f"{server}/#/view/{FOLDER}?p=0")
    expect(page.locator("#counter")).to_have_text("1 / 30")
    page.wait_for_timeout(300)
    page.touchscreen.tap(60, 500)
    page.wait_for_function("window.sliding === true")
    page.evaluate("window.viewerSnapTo(window.currentIdx + 1, false)")
    page.wait_for_timeout(50)
    assert page.evaluate("window.sliding") is False
