"""Staged prefetch: the next page must not share bandwidth with the far-ahead
window. Neighbours (|d|<=1) start fetching at once; the rest wait for the
next page's load (or a 400ms fallback), then start nearest-first."""
from playwright.sync_api import expect

FOLDER = "long"  # 30 pages


def _src_state(page):
    return page.evaluate(
        """() => Object.entries(window.stripImgs).map(([i, img]) => ({
              i: Number(i), src: !!img.getAttribute('src'), deferred: !!img.dataset.deferredSrc,
              prio: img.fetchPriority })).sort((a, b) => a.i - b.i)"""
    )


def test_far_pages_start_after_next_page_loads(page, server):
    # Hold every image response so the staging is observable.
    gate = {"open": False}
    held = []

    def handler(route):
        if gate["open"]:
            route.continue_()
        else:
            held.append(route)

    page.route("**/image/**", handler)
    page.goto(f"{server}/#/view/{FOLDER}?p=0")
    page.wait_for_function("window.stripImgs && Object.keys(window.stripImgs).length >= 9")
    st = _src_state(page)
    near = [s for s in st if s["i"] <= 1]
    far = [s for s in st if s["i"] > 1]
    assert all(s["src"] and not s["deferred"] for s in near), near
    assert all(s["deferred"] and not s["src"] for s in far), far
    assert all(s["prio"] == "high" for s in near) and all(s["prio"] == "low" for s in far)
    # Fallback timer (400ms) flushes the deferred window even if page 2 never lands.
    page.wait_for_function(
        "Object.values(window.stripImgs).every(img => img.getAttribute('src'))", timeout=2000
    )
    gate["open"] = True
    for r in held:
        r.continue_()
    expect(page.locator("#img-track img.loaded").first).to_be_visible()


def test_turning_promotes_the_new_neighbours(page, server):
    page.goto(f"{server}/#/view/{FOLDER}?p=0")
    expect(page.locator("#counter")).to_have_text("1 / 30")
    page.wait_for_function("Object.values(window.stripImgs).every(img => img.getAttribute('src'))")
    page.locator("#zone-prev").click()  # next
    expect(page.locator("#counter")).to_have_text("2 / 30")
    page.wait_for_function("!!window.stripImgs[9]")  # the window job runs after the acknowledging frame
    page.wait_for_function("Object.values(window.stripImgs).every(img => img.getAttribute('src'))")
    st = {s["i"]: s for s in _src_state(page)}
    # Newly created page 9 (idx+8) is deferred-then-started, never left without src.
    assert st[9]["src"] and not st[9]["deferred"]
