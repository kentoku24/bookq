"""iOS viewport fixtures + viewer/responsive + gesture/hash (T1-T3) — PWA exhaustive in test_ios_pwa.py."""

import re

from playwright.sync_api import expect

from tests.fixtures.ios_helpers import (
    _GAP_INNER_EXPECT,
    _GAP_OUTER_EXPECT,
    _TOL,
    _geom,
    _open,
    _spread_with,
    goto_offline,
    wait_for_sw,
)


def test_ios_fixture_viewport(ios_page):
    assert ios_page.viewport_size == {"width": 390, "height": 844}
    assert ios_page.evaluate("window.matchMedia('(pointer: coarse)').matches") is True
    assert ios_page.evaluate("window.devicePixelRatio") == 3
    assert "iPhone" in ios_page.evaluate("navigator.userAgent")


# ── T2: iPad spread + responsive ──────────────────────────────────────────
def test_ipad_spread_geometry(ipad_landscape_page):
    page = ipad_landscape_page
    assert page.viewport_size == {"width": 1180, "height": 820}
    expect(page.locator("body")).to_have_class(re.compile(r"\blandscape\b"))
    _open(page, "tight_fit", expect_pages=8)
    g = _geom(page)
    cur = _spread_with(g, 0)
    assert len(cur["pages"]) == 2, f"expected 2P spread at 1180x820: {cur}"
    right, left = sorted(cur["pages"], key=lambda p: -p["cellLeft"])
    assert abs(right["cellLeft"] - left["cellRight"] - _GAP_INNER_EXPECT) < _TOL, "inner gap wrong"
    nxt = _spread_with(g, 2)
    outer = cur["left"] - nxt["right"]
    assert 2 < outer <= _GAP_OUTER_EXPECT + 0.5, f"outer gap {outer:.1f} not in (2,10]"
    ordered = g["spreads"]
    i = next(k for k, s in enumerate(ordered) if abs((s["left"] + s["right"]) / 2 - g["vw"] / 2) < 4)
    cur_c, nxt_c = ordered[i], ordered[i + 1]
    assert nxt_c["right"] > 0, "next page pushed off-screen"
    assert cur_c["height"] <= g["vh"] + _TOL


def test_ipad_mixed_ar_page_mode(ipad_page, ipad_landscape_page):
    p = ipad_page
    assert p.viewport_size == {"width": 820, "height": 1180}
    expect(p.locator("body")).to_have_class(re.compile(r"\bportrait\b"))
    _open(p, "mixed_ar", expect_pages=6)
    g = _geom(p, settle=350)
    s = _spread_with(g, 0)
    assert len(s["pages"]) == 1, f"portrait must be 1P: {s}"
    expect(p.locator("#spread-toggle")).to_have_text("1P")
    lp = ipad_landscape_page
    assert lp.viewport_size == {"width": 1180, "height": 820}
    expect(lp.locator("body")).to_have_class(re.compile(r"\blandscape\b"))
    _open(lp, "mixed_ar", expect_pages=6)
    g2 = _geom(lp, settle=350)
    s2 = _spread_with(g2, 0)
    assert len(s2["pages"]) == 2, f"landscape must be 2P: {s2}"
    expect(lp.locator("#spread-toggle")).to_have_text("2P")
    tall, wide = sorted(s2["pages"], key=lambda x: x["imgWidth"])
    assert wide["imgWidth"] - tall["imgWidth"] > 8, f"widths did not diverge: {s2}"
    assert abs(wide["imgHeight"] - tall["imgHeight"]) < _TOL


def test_orientation_switch(ipad_page):
    page = ipad_page
    expect(page.locator("body")).to_have_class(re.compile(r"\bportrait\b"))
    _open(page, "tight_fit", expect_pages=8)
    page.set_viewport_size({"width": 1180, "height": 820})
    expect(page.locator("body.landscape")).to_be_visible(timeout=500)
    expect(page.locator("body")).to_have_class(re.compile(r"\blandscape\b"))
    page.set_viewport_size({"width": 820, "height": 1180})
    expect(page.locator("body.portrait")).to_be_visible(timeout=500)
    expect(page.locator("body")).to_have_class(re.compile(r"\bportrait\b"))


def test_ios_grid_adaptive(ios_page):
    page = ios_page
    assert page.viewport_size == {"width": 390, "height": 844}
    expect(page.locator(".tile").first).to_be_visible()
    widths = page.evaluate(
        """() => {
          const grid = document.getElementById('tile-grid');
          const tiles = [...grid.querySelectorAll('.tile')].slice(0,2).map(e=>e.getBoundingClientRect().width);
          return {gridW: grid.getBoundingClientRect().width, tiles};
        }"""
    )
    assert widths["tiles"], "no tiles measured"
    assert abs(widths["tiles"][0] - widths["gridW"] * 0.5) < 18, f"tile not ~50% at 390: {widths}"
    btn = page.locator("#footer-refresh-btn")
    btn.click()
    expect(page.locator("#tile-grid")).to_have_class(re.compile(r"\blist-mode\b"))
    expect(page.locator(".item.folder").first).to_be_visible()
    btn.click()
    expect(page.locator("#tile-grid")).to_have_class(re.compile(r"\bcard-mode\b"))
    expect(page.locator(".card").first).to_be_visible()
    btn.click()
    expect(page.locator("#tile-grid")).not_to_have_class(re.compile(r"\blist-mode\b"))
    expect(page.locator("#tile-grid")).not_to_have_class(re.compile(r"\bcard-mode\b"))
    expect(page.locator(".tile").first).to_be_visible()


# ── T3: gesture / history ─────────────────────────────────────────────────
def test_ios_tap_zone(ios_page):
    page = ios_page
    dims = page.evaluate("""() => {
      const r = n => document.getElementById(n).getBoundingClientRect();
      return {w: innerWidth, prev: r('zone-prev').width, tog: r('zone-toggle').width, nxt: r('zone-next').width};
    }""")
    assert abs(dims["prev"] / dims["w"] - 0.30) < 0.02, dims
    assert abs(dims["tog"] / dims["w"] - 0.40) < 0.02, dims
    assert abs(dims["nxt"] / dims["w"] - 0.30) < 0.02, dims
    _open(page, "simple_manga", expect_pages=5)
    page.locator("#zone-prev").click()
    expect(page.locator("#counter")).to_have_text("2 / 5")
    assert page.evaluate("getComputedStyle(document.getElementById('page-slider')).direction") == "rtl"
    page.keyboard.press("ArrowRight")
    expect(page.locator("#counter")).to_have_text("1 / 5")
    page.keyboard.press("ArrowLeft")
    expect(page.locator("#counter")).to_have_text("2 / 5")
    page.locator("#zone-next").click()
    expect(page.locator("#counter")).to_have_text("1 / 5")
    assert page.evaluate("!document.getElementById('toolbar').classList.contains('hidden')")
    page.locator("#zone-toggle").click()
    expect(page.locator("#toolbar")).to_have_class(re.compile(r"hidden"))
    page.locator("#zone-toggle").click()
    expect(page.locator("#toolbar")).not_to_have_class(re.compile(r"hidden"))


def test_ios_hash_deep_link_reload(ios_page, server):
    ios_page.goto(server + "/#/view/simple_manga?p=2")
    expect(ios_page.locator("#counter")).to_have_text("3 / 5")
    ios_page.reload()
    expect(ios_page.locator("#counter")).to_have_text("3 / 5")
    expect(ios_page).to_have_url(re.compile(r"#/view/simple_manga\?p=2$"))


def test_ios_hash_back_to_top(ios_page):
    _open(ios_page, "simple_manga", expect_pages=5)
    ios_page.keyboard.press("f")
    _open(ios_page, "long", expect_pages=30)
    ios_page.go_back()
    expect(ios_page.locator("#counter")).to_have_text("1 / 5")
    ios_page.go_back()
    expect(ios_page.locator("#empty")).to_have_text("📂 サイドバーからフォルダを選んでください")


def test_ios_hash_p_clamp(ios_page, server):
    ios_page.goto(server + "/#/view/simple_manga?p=99")
    expect(ios_page.locator("#counter")).to_have_text("1 / 5")


def test_ios_hash_malformed_view(ios_page, server):
    ios_page.goto(server + "/#/view/%zz")
    expect(ios_page.locator("#empty")).to_have_text("📂 サイドバーからフォルダを選んでください")
    h = ios_page.evaluate("location.hash")
    assert h in ("", "#/view/%zz"), h


def test_ios_hash_malformed_browse(ios_page, server):
    # Root count includes the parent-level images-tile ("mixed").
    ios_page.goto(server + "/#/browse/%zz")
    expect(ios_page.locator("#folder-count")).to_have_text("18 件")
    # Hash clearing happens asynchronously after the full tree resolves
    # (applyUrlState post-full in app.js); the shallow paint already satisfies
    # the count above, so wait instead of asserting synchronously (flaky on
    # slow CI — same async behavior test_ios_hash_malformed_view tolerates).
    ios_page.wait_for_function("location.hash === ''", timeout=10000)


def test_ios_hash_history_delta(ios_page):
    _open(ios_page, "simple_manga", expect_pages=5)
    before = ios_page.evaluate("history.length")
    ios_page.locator("#zone-prev").click()
    ios_page.locator("#zone-prev").click()
    ios_page.locator("#zone-prev").click()
    ios_page.wait_for_timeout(350)
    after = ios_page.evaluate("history.length")
    assert after - before <= 1, f"scheduleUrlSync burst grew history: {before}->{after}"


# ── T4 minimal smoke for -k test_ios_offline in this file ────────────────
def test_ios_offline_viewport_smoke(ios_page, server):
    """viewport no user-scalable=no + offline-btn 44px + SW smoke (exhaustive in test_ios_pwa.py)."""
    page = ios_page
    assert page.viewport_size == {"width": 390, "height": 844}
    vp = page.evaluate("() => document.querySelector('meta[name=\"viewport\"]')?.content || ''")
    assert "user-scalable=no" not in vp
    assert "width=device-width" in vp
    goto_offline(page, server)
    assert wait_for_sw(page), "SW not ready on ios_page ?offline=1"
    btn = page.locator('.tile[data-path="simple_manga"] .offline-btn')
    expect(btn).to_be_visible(timeout=5000)
    box = btn.bounding_box()
    assert box is not None and box["width"] >= 44 and box["height"] >= 44, f"offline-btn >=44px required, got {box}"
    has_inert = page.evaluate("() => document.getElementById('bottom-sheet').hasAttribute('inert')")
    assert has_inert, "bottom-sheet must have inert when hidden"
