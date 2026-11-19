"""Viewport-scoped thumb prefetch + probe privacy contract (#396).

Prefetch may only fetch what the viewport scan deems eligible (on-screen or
within one screen above/below, verbatim rendered img[src], capped at 50):
no arbitrary first-50 fallback, nothing while the sidebar is closed. The
eligibility probe itself stays module-private — only the documented
ThumbPrefetch surface is reachable from window.
"""

from tests.test_frontend_perf_lifecycle import (
    _new_prefetch_page,
    _quiesce_prefetch,
    _seed_many_tiles,
    _stop_fill_poll,
    ELIGIBLE_URLS_JS,
)


def test_prefetch_viewport_scope_boundary(browser, server):
    """Only on/near-viewport tiles prefetch: a far-below tile's cover is
    excluded while every eligible URL is fetched verbatim."""
    context, page = _new_prefetch_page(browser, width=1280, height=600)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        _stop_fill_poll(page)
        _seed_many_tiles(page, 80)
        page.wait_for_selector("#tile-grid .tile")
        _quiesce_prefetch(page)
        far_url = page.evaluate(
            """() => {
                const els = document.querySelectorAll('#tile-grid .tile[data-path]');
                const img = els[els.length - 1].querySelector('img[src]');
                return img ? img.getAttribute('src') : null;
            }"""
        )
        assert far_url, "seeded grid must render a last tile cover"
        page.evaluate("() => window.__BOOKQ_DEBUG.prefetchThumbs()")
        calls = page.evaluate("() => window.__pf.calls")
        puts = page.evaluate("() => window.__pf.puts")
        expected = page.evaluate(ELIGIBLE_URLS_JS)
        assert len(expected) > 0, "viewport must mark some tiles eligible"
        assert far_url not in calls, (
            f"far-below tile must stay outside the prefetch scope: {far_url}"
        )
        assert set(calls) == set(expected), (
            "prefetch must fetch exactly the viewport-eligible URLs, "
            f"got {len(set(calls))} calls vs {len(expected)} eligible"
        )
        assert set(puts) == set(calls), "fetched URLs must land in the thumb caches"
        assert page.evaluate("() => window.__pf.maxInflight") <= 3
    finally:
        context.close()


def test_prefetch_closed_sidebar_fetches_nothing_and_probe_private(
    browser, server
):
    """Closed sidebar prefetches nothing (no first-50 fallback), and the
    eligibility probe internals are not reachable from window."""
    context, page = _new_prefetch_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        _stop_fill_poll(page)
        _quiesce_prefetch(page)
        page.evaluate(
            "() => { document.getElementById('sidebar').classList.remove('open'); }"
        )
        page.wait_for_timeout(150)
        page.evaluate("() => window.__BOOKQ_DEBUG.prefetchThumbs()")
        assert page.evaluate("() => window.__pf.calls.length") == 0, (
            "closed sidebar must fetch nothing — no arbitrary fallback"
        )
        surface = page.evaluate("() => Object.keys(window.ThumbPrefetch).sort()")
        assert surface == [
            "PREFETCH_CONCURRENCY",
            "cancelThumbPrefetch",
            "collectThumbPrefetchUrls",
            "prefetchThumbs",
            "scheduleThumbPrefetch",
        ], f"only the documented surface may be public: {surface}"
        leaked = page.evaluate(
            """() => [
                '_eligibleTargets', '_viewportRect', '_rankOf', '_dirForUrl',
                '_isThumbUrl', '_sidebarScroller', '_sidebarOpen',
                '_batchGen', '_activeController', '_idlePending',
            ].filter(k => k in window)"""
        )
        assert leaked == [], f"prefetch probe internals must stay private: {leaked}"
    finally:
        context.close()
