"""Task 7 baseline + failing-first proof.

Baseline pins existing harness displays must stay.
Failing proof asserts new displays (thumb hit, /api/folders cached with ignoreSearch, SHELL hit counts)
that are absent before the fix, so this test FAILS until harness is extended.
"""
import re
import pytest

def _base(server):
    try:
        return server.url
    except AttributeError:
        return server

def goto_harness(page, server):
    base = _base(server)
    page.goto(base + "/static/offline-harness.html?harness=1")
    page.wait_for_selector("#harness-status", timeout=10000)

def clear_all(page):
    try:
        page.evaluate(
            """async () => {
                try { await new Promise((res, rej) => { const r = indexedDB.deleteDatabase('bookq-offline'); r.onsuccess=()=>res(); r.onerror=()=>res(); r.onblocked=()=>res(); }); } catch {}
                try { const ks = await caches.keys(); for (const k of ks) { try { await caches.delete(k); } catch {} } } catch {}
                try { localStorage.clear(); } catch {}
            }"""
        )
    except Exception:
        pass
    page.wait_for_timeout(400)

def test_baseline_existing_harness_still_present(page, server):
    """Pin existing displays must not regress."""
    clear_all(page)
    goto_harness(page, server)
    page.wait_for_timeout(800)
    # existing selectors
    assert page.locator("#harness-status").count() == 1
    assert page.locator("#harness-cache-count").count() == 1
    assert page.locator("#harness-idb-table").count() == 1
    assert page.locator("#harness-idb-dedup").count() == 1
    assert page.locator("#harness-storage-raw").count() == 1
    assert page.locator("#harness-storage-diff").count() == 1
    assert page.locator("#harness-leaf-count").count() == 1
    assert page.locator("#harness-signals").count() == 1
    assert page.locator("#harness-signals .signal").count() == 10
    # ensure StaticFiles still serves harness 200
    ok = page.evaluate("async () => { const r = await fetch('/static/offline-harness.html'); return r.ok && r.status===200; }")
    assert ok, "harness should be 200 via StaticFiles"
    # content grep checks
    harness_html = page.evaluate("async () => { const r = await fetch('/static/offline-harness.html'); return await r.text(); }")
    assert "Offline Harness" in harness_html
    assert "caches.keys" in harness_html or "caches.open" in harness_html
    assert "getLeafPathsFor" in harness_html

def test_failing_new_displays_thumb_api_shell(page, server):
    """Failing-first proof: new displays must exist after Task 7."""
    clear_all(page)
    goto_harness(page, server)
    # wait for harness tree + refresh within 800ms (similar to test_06 expectation)
    page.wait_for_timeout(900)
    # ensure refresh was called
    page.evaluate("async () => { try { await window.refresh(); } catch {} }")
    page.wait_for_timeout(800)

    # 1. thumb hit rate display
    thumb_el = page.locator("#harness-thumb-hit")
    assert thumb_el.count() == 1, "missing #harness-thumb-hit (thumb hit X/Y)"
    thumb_text = thumb_el.text_content() or ""
    assert re.search(r"thumb.*hit", thumb_text, re.I), f"thumb hit text missing 'thumb.*hit', got {thumb_text!r}"
    assert re.search(r"\d+\s*/\s*\d+", thumb_text), f"thumb hit should show X/Y, got {thumb_text!r}"

    # 2. /api/folders cached with ignoreSearch fallback
    api_el = page.locator("#harness-api-folders-cached")
    assert api_el.count() == 1, "missing #harness-api-folders-cached"
    api_text = api_el.text_content() or ""
    assert "/api/v1/tree" in api_text, f"/api/folders missing, got {api_text!r}"
    assert "cached" in api_text.lower(), f"cached missing, got {api_text!r}"
    # must mention ignoreSearch
    assert "ignoreSearch" in api_text or "ignore" in api_text.lower(), f"ignoreSearch fallback not displayed, got {api_text!r}"

    # 3. SHELL precache hit counts including /static/app.js etc
    shell_el = page.locator("#harness-shell-hits")
    assert shell_el.count() == 1, "missing #harness-shell-hits"
    shell_text = shell_el.text_content() or ""
    assert "/static/app.js" in shell_text, f"SHELL hits should list /static/app.js, got {shell_text!r}"
    # hits should update within 800ms after refresh (synchronous check already after wait)
    assert re.search(r"\d+\s*/\s*\d+", shell_text) or "hit" in shell_text.lower(), f"SHELL hit counts missing, got {shell_text!r}"

    # also verify raw file grep would pass
    html = page.evaluate("async () => { const r = await fetch('/static/offline-harness.html'); return await r.text(); }")
    assert re.search(r"thumb.*hit", html, re.I), "file should contain thumb.*hit for grep check"
    assert "harness-shell" in html.lower() or "SHELL" in html
    assert "caches.match" in html and "ignoreSearch" in html
