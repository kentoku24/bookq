"""ビューア磨き込み（監査 V3/V4/V5）。"""
import io
import re

import pytest

from PIL import Image
from playwright.sync_api import expect

DARK_MAX = 40  # 背景 #0d0d0d 近傍とみなす RGB 上限

# The anchor spread's own <img>s live inside the .spread-container positioned
# at left:0 (spreadLeft(anchorSpread) is always 0 — see spread.js:spreadLeft).
# Any <img> outside that container must not intersect the viewport
# horizontally: with gapBefore() widened (Task 4 / 監査 V3), a narrower
# (solo/odd) spread's leftover margin must never be filled by the *next*
# spread's real page content peeking in.
_FIND_NEIGHBOUR_PEEK_JS = """
() => {
  const containers = [...document.querySelectorAll('#img-track .spread-container')];
  let anchor = null, anchorAbs = Infinity;
  for (const c of containers) {
    const left = Math.abs(parseFloat(c.style.left || '0'));
    if (left < anchorAbs) { anchorAbs = left; anchor = c; }
  }
  const bad = [];
  for (const img of document.querySelectorAll('#img-track img')) {
    if (anchor && anchor.contains(img)) continue;
    const r = img.getBoundingClientRect();
    if (r.width > 0 && r.right > 0.5 && r.left < innerWidth - 0.5) {
      bad.push({src: img.src, left: r.left, right: r.right});
    }
  }
  return bad;
}
"""


def _px(page, x, y):
    img = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
    return img.getpixel((int(x), int(y)))


def _assert_no_neighbour_peek_and_dark_margins(page):
    bad = page.evaluate(_FIND_NEIGHBOUR_PEEK_JS)
    assert bad == [], bad
    w, h = page.viewport_size["width"], page.viewport_size["height"]
    for x in (w * 0.05, w * 0.95):
        r, g, b = _px(page, x, h / 2)
        assert max(r, g, b) <= DARK_MAX, ((x, h / 2), (r, g, b))


@pytest.mark.strip_only
def test_solo_cover_partner_side_is_dark_not_neighbour_page(landscape_page, server):
    landscape_page.goto(server + "/#/view/long?p=0")
    landscape_page.wait_for_selector("#img-track img")
    landscape_page.locator("#spread-shift").click()  # 表紙ソロ化
    landscape_page.wait_for_timeout(500)
    _assert_no_neighbour_peek_and_dark_margins(landscape_page)


@pytest.mark.strip_only
def test_odd_trailing_page_does_not_show_neighbour(landscape_page, server):
    landscape_page.goto(server + "/#/view/long?p=29")
    landscape_page.wait_for_selector("#img-track img")
    landscape_page.locator("#spread-shift").click()  # 末尾ページをソロ化
    landscape_page.wait_for_timeout(500)
    _assert_no_neighbour_peek_and_dark_margins(landscape_page)


@pytest.mark.strip_only
def test_chapter_start_spread_does_not_show_previous_chapter(landscape_page, server):
    """監査 V3: a chapter-start spread (vol2's solo spread, 1 page) used to use a
    fixed CHAPTER_GAP_PX=20 seam regardless of leftover, so the previous
    chapter's spread (vol1, 2 pages) peeked ~230px into view at 900x600.
    gapBefore() now treats CHAPTER_GAP_PX as a floor under the same
    Math.max(base, leftover(s-1), leftover(s)) rule as any other spread pair.
    "group/deep_series/vol1"+"vol2" is the depth-2 chapter-concat fixture
    (tests/conftest.py); opening vol2 directly makes its solo, chapter-start
    spread the anchor, with vol1 as the previous chapter to its right.
    """
    landscape_page.goto(server + "/#/view/group/deep_series/vol2?p=0")
    landscape_page.wait_for_selector("#img-track img")
    landscape_page.wait_for_timeout(500)
    _assert_no_neighbour_peek_and_dark_margins(landscape_page)


def test_toolbar_height_budget_portrait(page, server):
    page.goto(server + "/#/view/long?p=0")
    page.wait_for_selector("#img-track img")
    h = page.evaluate("document.getElementById('toolbar').getBoundingClientRect().height")
    assert h <= 64, h


def test_slider_hit_area_44px_but_thin_track(page, server):
    """Hit area (element box) is 44px for touch, but the visible track must
    still read as an ~8px band. getComputedStyle(el, '::-webkit-slider-
    runnable-track') is not usable here: -webkit-appearance:none on the host
    makes Chromium stop rendering/exposing that pseudo-element, so the call
    silently falls back to the *host's* computed height (44px, not the
    authored 8px — confirmed empirically, see task-5-report.md). Instead:
    the host background must be transparent (so only the pseudo-element's
    #333 track paints anything inside the 44px box), and the actual
    rendered band is measured with a real screenshot pixel scan.

    #toolbar's own backdrop-filter is neutralized for this measurement only
    (via an injected <style>, not touching app/static/style.css). Empirically
    (see task-5-report.md), Playwright's headless Chromium fails to rasterize
    the ::-webkit-slider-runnable-track pseudo-element at all when the
    ancestor #toolbar has an active backdrop-filter — confirmed both with a
    minimal isolated reproduction (passes) and a real, non-headless Chrome
    screenshot of this exact page (track renders correctly) — so this is a
    Playwright-headless/backdrop-filter rendering quirk, not a real product
    bug, and not something to work around by changing production CSS.

    Hardened for parallel runs (-n 4+, since #191's overview/zoom-layer
    merge): under CPU contention from other xdist workers this was seen to
    fail intermittently — the single hard-coded sample column could land on
    the #hint overlay fading out, on the toolbar mid-repaint, or (since the
    slider is RTL and p=0 puts the thumb at the right edge) too close to the
    thumb once #overview-btn narrowed #page-slider-wrap. #hint is hidden
    outright (it lives at the bottom of the viewport, nowhere near the
    slider, but its opacity transition is one more thing that can still be
    mid-flight when the screenshot is taken, and hiding it costs nothing).
    The slider's rect must also be stable across two animation frames before
    it's trusted, and the whole screenshot+scan is retried a few times
    before failing, since a single unlucky frame under load is exactly what
    causes this class of flake.
    """
    page.goto(server + "/#/view/long?p=0")
    page.wait_for_selector("#img-track img")
    page.wait_for_selector("#toolbar", state="visible")
    page.add_style_tag(
        content=(
            "#toolbar{-webkit-backdrop-filter:none !important;backdrop-filter:none !important}"
            "#hint{opacity:0 !important;display:none !important}"
        )
    )
    # Wait for the slider's own box to stop moving (layout settled) before
    # trusting any rect/screenshot pair.
    page.wait_for_function(
        """() => {
            const s = document.getElementById('page-slider');
            if (!s) return false;
            const r = s.getBoundingClientRect();
            const key = JSON.stringify([r.left, r.top, r.width, r.height]);
            if (window.__prevSliderRectKey === key) return true;
            window.__prevSliderRectKey = key;
            return false;
        }"""
    )
    info = page.evaluate(
        """() => {
            const s = document.getElementById('page-slider');
            const r = s.getBoundingClientRect();
            return {
                bg: getComputedStyle(s).backgroundColor,
                left: r.left, top: r.top, width: r.width, height: r.height,
            };
        }"""
    )
    assert info["bg"] == "rgba(0, 0, 0, 0)", info["bg"]
    assert info["height"] >= 44, info

    left, top = int(info["left"]), int(info["top"])
    width, height = int(info["width"]), int(info["height"])
    ymid = top + height // 2
    TRACK_RGB = (51, 51, 51)
    THUMB_RGB = (170, 170, 170)

    def near(px, target, tol=10):
        return all(abs(c - t) <= tol for c, t in zip(px, target))

    def scan_column(img, x):
        """Row-count of pixels in the slider's 44px box that differ from the
        background sampled just above it (same column)."""
        ref = img.getpixel((x, top - 3))

        def differs(px):
            return max(abs(a - b) for a, b in zip(px, ref)) > 20

        return sum(1 for dy in range(height + 1) if differs(img.getpixel((x, top + dy))))

    last_report = None
    for attempt in range(3):
        img = Image.open(io.BytesIO(page.screenshot())).convert("RGB")

        # Locate the thumb on this frame (RTL + p=0 puts it at the right
        # edge, but don't hard-code that — read it off the actual pixels)
        # so candidate columns within 20px of it can be skipped.
        thumb_dxs = [dx for dx in range(width) if near(img.getpixel((left + dx, ymid)), THUMB_RGB, tol=15)]

        candidates = [round(width * pct) for pct in (0.2, 0.3, 0.4, 0.6, 0.7)]
        report = {}
        for dx in candidates:
            if any(abs(dx - tdx) < 20 for tdx in thumb_dxs):
                report[dx] = "skipped (near thumb)"
                continue
            x = left + dx
            rows = scan_column(img, x)
            report[dx] = rows
            if 7 <= rows <= 9:
                return  # found a column that reads as an ~8px track — pass

        last_report = report
        if attempt < 2:
            page.wait_for_timeout(150)

    assert False, (
        "no candidate column read as a 7-9px track band across 3 attempts; "
        f"per-column row counts (dx -> rows, at width={width}): {last_report}"
    )


def test_center_zone_toggles_toolbar_at_42_percent(page, server):
    """中央ゾーンが 40-60% に広がったので、幅の 42% 位置のタップはツールバー開閉になる
    （旧 45/10/45 では zone-prev が 0-45% を占め、42% はまだ次ページ側だった）。

    brief 原文は 35% 位置を指定していたが、新ゾーンでも zone-prev は 0-40% を占めるため
    35% は依然として次ページ側であり、実測（クリック後にカウンタが 1/30→2/30 に進む）でも
    それを確認した。中央ゾーンの拡大を検証する位置として、新ゾーン(40-60%)には入るが
    旧ゾーン(0-45%)ではまだ zone-prev だった 42% に修正した。
    """
    page.goto(server + "/#/view/long?p=0")
    page.wait_for_selector("#img-track img")
    w, h = page.viewport_size["width"], page.viewport_size["height"]
    was_hidden = page.evaluate("document.getElementById('toolbar').classList.contains('hidden')")
    page.mouse.click(w * 0.42, h * 0.5)
    expect(page.locator("#counter")).to_have_text(re.compile(r"^1 / 30"))
    is_hidden = page.evaluate("document.getElementById('toolbar').classList.contains('hidden')")
    assert is_hidden != was_hidden, (was_hidden, is_hidden)
