"""Perf HUD toggle via the offline harness (#29): PWA has no URL bar, so
?perf=1 is unreachable there — localStorage bookq_perf (toggled from
/static/offline-harness.html §7) enables the same overlay on next load."""

from playwright.sync_api import expect

from viewer_touch import open_book


def _goto_harness(page, server):
    page.goto(server + "/static/offline-harness.html?harness=1")
    page.wait_for_selector("#harness-perf-status", timeout=10000)


def _flag(page):
    return page.evaluate("localStorage.getItem('bookq_perf')")


def test_perf_toggle_on_off(ios_page, server):
    page = ios_page
    page.evaluate("localStorage.removeItem('bookq_perf')")
    _goto_harness(page, server)
    expect(page.locator("#harness-perf-status")).to_contain_text("perf: OFF")
    page.locator("#btn-perf-toggle").click()
    expect(page.locator("#harness-perf-status")).to_contain_text("perf: ON")
    assert _flag(page) == "1"
    page.locator("#btn-perf-toggle").click()
    expect(page.locator("#harness-perf-status")).to_contain_text("perf: OFF")
    assert _flag(page) is None


def test_perf_flag_shows_overlay_without_query(ios_page, server):
    page = ios_page
    _goto_harness(page, server)
    page.evaluate("localStorage.setItem('bookq_perf', '1')")
    open_book(page, server, "long", 0, 30)
    assert page.locator("#perf-overlay").count() == 1
    page.evaluate("localStorage.removeItem('bookq_perf')")
    page.reload()
    page.wait_for_selector("#counter", timeout=10000)
    assert page.locator("#perf-overlay").count() == 0


def test_perf_query_still_works(ios_page, server):
    page = ios_page
    page.evaluate("localStorage.removeItem('bookq_perf')")
    page.goto(server + "/?perf=1#/view/long?p=0")
    expect(page.locator("#counter")).to_have_text("1 / 30")
    assert page.locator("#perf-overlay").count() == 1
