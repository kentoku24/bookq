"""Shared iOS helpers for viewer + PWA tests."""

import os

_TOL = 2.5
_GAP_INNER_EXPECT = 5
_GAP_OUTER_EXPECT = 10

_IOS_GEOM_JS = """() => {
  const spreads = [...document.querySelectorAll('#img-track .spread-container')].map(sc => {
    const r = sc.getBoundingClientRect();
    return {
      left: r.left, right: r.right, top: r.top, bottom: r.bottom,
      width: r.width, height: r.height,
      pages: [...sc.querySelectorAll('.page-cell')].map(c => {
        const cr = c.getBoundingClientRect();
        const im = c.querySelector('img');
        const ir = im ? im.getBoundingClientRect() : {left:0,right:0,bottom:0,width:0,height:0};
        return {
          idx: Number(im ? im.style.getPropertyValue('--i') : -1),
          cellLeft: cr.left, cellRight: cr.right,
          cellWidth: cr.width, cellHeight: cr.height,
          imgLeft: ir.left, imgRight: ir.right, imgBottom: ir.bottom,
          imgWidth: ir.width, imgHeight: ir.height,
          natWidth: im ? im.naturalWidth : 0, natHeight: im ? im.naturalHeight : 0,
          loaded: im ? (im.complete && im.naturalWidth > 0) : false,
        };
      }),
    };
  });
  spreads.sort((a, b) => b.left - a.left);
  return {vw: innerWidth, vh: innerHeight, spreads};
}"""

def _open(page, path: str, expect_pages: int):
    from playwright.sync_api import expect as _expect
    page.locator(f'.tile[data-path="{path}"]').click()
    _expect(page.locator("#counter")).to_have_text(f"1 / {expect_pages}")

def _geom(page, settle: int = 400):
    page.wait_for_selector("#img-track .spread-container")
    # Images decode async; measuring before naturalWidth lands reads the
    # pre-load fallback aspect (~0.7 cells) instead of the real geometry.
    page.wait_for_function(
        "() => [...document.querySelectorAll("
        "  '#img-track .spread-container img')]"
        "  .every(im => im.complete && im.naturalWidth > 0)",
        timeout=10000)
    page.wait_for_timeout(settle)
    g = page.evaluate(_IOS_GEOM_JS)
    assert g["spreads"], "no spread containers rendered"
    return g

def _spread_with(g, idx: int):
    for s in g["spreads"]:
        if any(p["idx"] == idx for p in s["pages"]):
            return s
    raise AssertionError(f"page {idx} not in any spread")

from tests.safari.sw import ensure_sw_interception as _ensure_sw_interception


def wait_for_sw(page, timeout=15000):
    for _ in range(2):
        try:
            try:
                page.wait_for_function(
                    "() => navigator.serviceWorker && navigator.serviceWorker.controller !== null",
                    timeout=4000,
                )
                _ensure_sw_interception(page)
                return True
            except Exception:
                pass
            page.wait_for_function(
                "() => navigator.serviceWorker && !!navigator.serviceWorker.ready",
                timeout=timeout,
            )
            page.wait_for_timeout(500)
            ok = page.evaluate(
                "async () => { try { const r = await navigator.serviceWorker.ready; return !!r; } catch { return !!navigator.serviceWorker.controller; } }"
            )
            if ok:
                _ensure_sw_interception(page)
                return True
        except Exception as e:
            if "Execution context was destroyed" in str(e):
                page.wait_for_timeout(800)
                try:
                    page.wait_for_selector(".tile", timeout=10000)
                except Exception:
                    pass
                continue
            raise
    return False

def goto_offline(page, server):
    base = server.url if hasattr(server, "url") else server
    # use query ?offline=1 to enable SW gate in test mode
    page.goto(base + "/?offline=1")
    page.wait_for_selector(".tile", timeout=10000)

def is_webkit():
    return os.environ.get("BROWSER") == "webkit"
