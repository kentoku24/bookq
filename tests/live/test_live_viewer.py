"""Live viewer smoke (#31): every sampled folder opens, shows 1/N, and turns
via the exposed viewerSnapTo (input handling is covered by fixture tests;
here the serving path is under test). Failure messages carry hash IDs and
counter text (counts only) — never raw paths.

Note: books open at their chapter's global start (currentIdx != 0 in
cross-chapter books), so turns are relative (startIdx+k) while counters stay
chapter-local ("k+1 / N").
"""
import urllib.parse

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.skipif(
    not __import__("os").environ.get("BOOKQ_LIVE_ROOT"),
    reason="live-only: set BOOKQ_LIVE_ROOT to run",
)


def _qpath(path):
    return "/".join(urllib.parse.quote(seg, safe="") for seg in path.split("/"))


def _open(page, base, leaf):
    page.goto(f"{base}/#/view/{_qpath(leaf['path'])}?p=0")
    expect(page.locator("#counter")).to_have_text(f"1 / {len(leaf['images'])}", timeout=60000)


def _idx(page):
    return page.evaluate("window.currentIdx")


def _counter(page):
    el = page.locator("#counter")
    return el.inner_text() if el.count() else "<none>"


def _turn_to(page, target, timeout_s=20):
    """Advance one page at a time until currentIdx reaches target (the open
    cascade and first transitions can be slow on huge books)."""
    import time

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _idx(page) >= target:
            return True
        page.evaluate("window.viewerSnapTo(window.currentIdx + 1)")
        page.wait_for_timeout(1000)
    return _idx(page) >= target


def test_live_viewer_opens_and_turns(live_page, live_server, live_sample):
    assert live_sample, "empty sample"
    failed = []
    for leaf in live_sample:
        page = live_page
        hid = leaf["id"]
        try:
            _open(page, live_server, leaf)
            n = len(leaf["images"])
            start = _idx(page)
            assert _turn_to(page, start + 1), f"{hid}:turn1 idx={_idx(page)}"
            expect(page.locator("#counter")).to_have_text(f"2 / {n}", timeout=15000)
            assert _turn_to(page, start + 2), f"{hid}:turn2 idx={_idx(page)}"
            expect(page.locator("#counter")).to_have_text(f"3 / {n}", timeout=15000)
            assert page.locator("#perf-overlay").count() == 0
        except Exception:
            failed.append(f"{hid}:counter={_counter(page)}")
    assert not failed, f"viewer failed on [{','.join(sorted(failed))}]"
