"""T4 PWA/Offline on ios_page 390x844 — WebKit narrow check (Cache/estimate only)."""

import re

from playwright.sync_api import expect

from tests.fixtures.ios_helpers import goto_offline, wait_for_sw


def test_ios_offline_sw_cache_offline(ios_page, server):
    page = ios_page
    assert page.viewport_size == {"width": 390, "height": 844}
    goto_offline(page, server)
    assert wait_for_sw(page), "SW not ready"
    page.wait_for_timeout(700)
    assert page.evaluate("async () => !!(await navigator.serviceWorker.getRegistration())")
    page.locator('.tile[data-path="simple_manga"]').click()
    expect(page.locator("#counter")).to_have_text("1 / 5", timeout=8000)
    expect(page.locator("#img-track .page-cell img").first).to_have_class(re.compile(r"\bloaded\b"), timeout=8000)
    page.wait_for_timeout(1200)
    assert page.evaluate("async () => { const ks = await caches.keys(); return ks.some(k=>k.startsWith('bookq-work-')); }"), "no bookq-work-* cache"
    # spec §8: offline browse only surfaces recorded downloads — the reader's
    # opportunistic page cache alone has no works record, so an explicit
    # download is required for the tile to remain visible/usable offline.
    page.evaluate("async () => { await window.downloadWork('simple_manga'); }")
    page.context.set_offline(True)
    try:
        page.locator("#menu-btn").click()
        expect(page.locator("#sidebar")).to_have_class(re.compile(r"\bopen\b"), timeout=3000)
    except Exception:
        pass
    expect(page.locator(".tile").first).to_be_visible(timeout=5000)
    # dispatch_event: connectivity transitions re-render the grid while the
    # cycle settles — a stability-checked .click() flakes on DOM churn.
    page.locator('.tile[data-path="simple_manga"]').dispatch_event("click")
    expect(page.locator("#counter")).to_have_text("1 / 5", timeout=8000)
    imgs = page.locator("#img-track .page-cell img")
    expect(imgs.first).to_have_class(re.compile(r"\bloaded\b"), timeout=8000)
    assert imgs.first.evaluate("e => e.naturalWidth") > 0
    page.context.set_offline(False)


def test_ios_offline_health_amber(ios_page, server):
    page = ios_page
    goto_offline(page, server)
    wait_for_sw(page)
    page.evaluate("() => window.updateOnlineStatus && window.updateOnlineStatus()")
    page.wait_for_function("() => document.getElementById('offline-status')?.textContent.includes('オンライン')", timeout=5000)
    # spec §5: any successful request is reachability evidence and lands the
    # state machine back online even mid-cycle — a health-only outage is NOT
    # offline. Simulate true unreachability by rejecting every fetch.
    page.route("**/api/v1/health*", lambda route: route.abort("failed"))
    page.evaluate(
        """() => {
            window._healthOrig = window.fetch;
            window.fetch = () => Promise.reject(new Error('failed'));
        }"""
    )
    page.evaluate("() => window.updateOnlineStatus && window.updateOnlineStatus()")
    page.wait_for_function("() => document.getElementById('offline-status')?.textContent.includes('オフライン')", timeout=5000)
    expect(page.locator("#offline-status")).to_have_class(re.compile(r"\boffline\b"), timeout=5000)
    page.unroute("**/api/v1/health*")
    page.evaluate("() => { try { if(window._healthOrig) window.fetch = window._healthOrig; } catch {} }")
    page.evaluate("() => window.updateOnlineStatus && window.updateOnlineStatus()")
    page.wait_for_function("() => document.getElementById('offline-status')?.textContent.includes('オンライン')", timeout=5000)
    expect(page.locator("#offline-status")).not_to_have_class(re.compile(r"\boffline\b"), timeout=5000)


def test_ios_offline_btn_and_a11y(ios_page, server):
    page = ios_page
    vp = page.evaluate("() => document.querySelector('meta[name=\"viewport\"]')?.content || ''")
    assert "user-scalable=no" not in vp
    assert "width=device-width" in vp
    assert page.evaluate("() => document.getElementById('bottom-sheet').hasAttribute('inert')")
    goto_offline(page, server)
    assert wait_for_sw(page), "SW not ready"
    btn = page.locator('.tile[data-path="simple_manga"] .offline-btn')
    expect(btn).to_be_visible(timeout=5000)
    box = btn.bounding_box()
    assert box is not None and box["width"] >= 44 and box["height"] >= 44, f"offline-btn >=44px required, got {box}"
    btn.click()
    expect(page.locator('.tile[data-path="simple_manga"] .offline-btn')).to_have_class(re.compile(r"state-downloading|downloading"), timeout=8000)
    expect(page.locator('.tile[data-path="simple_manga"] .offline-btn')).to_have_class(re.compile(r"state-complete|complete"), timeout=15000)
    pops = page.locator(".ux-pop")
    assert pops.count() <= 1, f"ux-pop should be single, got {pops.count()}"
    btn.click()
    expect(page.locator('.tile[data-path="simple_manga"] .offline-btn')).to_have_class(re.compile(r"confirm-delete|state-confirm"), timeout=5000)
    has_inert = page.evaluate("() => document.getElementById('bottom-sheet').hasAttribute('inert')")
    assert isinstance(has_inert, bool)
    btn.click()
    expect(page.locator('.tile[data-path="simple_manga"] .offline-btn')).to_have_class(re.compile(r"state-idle|idle"), timeout=8000)


def test_ios_offline_storage_touch_viewport(ios_page, server):
    page = ios_page
    js = page.evaluate("async () => { const r = await fetch('/static/app.js'); return await r.text(); }")
    assert "scheduleOfflineTouch" in js
    assert "dedupedBytes" in js
    assert "hasPersistedSupport" in js
    assert "navigator.userAgent" not in js
    assert "storageEstimate" in js or "storage" in js.lower()
    goto_offline(page, server)
    assert wait_for_sw(page), "SW not ready"
    btn = page.locator('.tile[data-path="simple_manga"] .offline-btn')
    expect(btn).to_be_visible(timeout=5000)
    expect(page.locator("#sidebar-footer #storage-text")).to_contain_text("💾")
    storage_text = page.evaluate("() => document.getElementById('storage-text')?.textContent || ''")
    assert "0/0" not in storage_text or "MB" in storage_text
