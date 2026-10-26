"""Server-held reading position (「続きから」resume sync) — API + e2e contract.

Contract:
  * One latest record per user, in-memory on app.state, last-writer-wins.
    PUT stores, GET returns ``{"position": ...|null}``, DELETE clears.
  * The client mirrors every ReadingProgress.save as a trailing-edge PUT
    (2s debounce, single in-flight, keepalive) — a page-turn burst must not
    produce a request per turn.
  * Boot pull merges a strictly-newer remote record into localStorage, so the
    resume pill, tile bars and loadImages all pick it up unchanged; a newer
    LOCAL newest self-heals back up to the server.

Session-server state is reset per test by the conftest autouse fixture.

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt \\
        pytest tests/test_reading_position.py -q
"""

import json
import time
import urllib.error
import urllib.request

from playwright.sync_api import expect


def _req(server: str, method: str, path: str, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(server + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except Exception:
            return e.code, None


def _get_position(server):
    _, body = _req(server, "GET", "/api/reading-position")
    return (body or {}).get("position")


def _wait_position(server, pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        pos = _get_position(server)
        if pos and pred(pos):
            return pos
        time.sleep(0.2)
    return None


# ── API contract ──────────────────────────────────────────────────────


def test_put_get_roundtrip(server):
    status, _ = _req(server, "PUT", "/api/reading-position",
                     {"folder": "long", "page": 4, "total": 30, "ts": 12345})
    assert status == 200
    pos = _get_position(server)
    assert pos["folder"] == "long"
    assert pos["page"] == 4 and pos["total"] == 30
    assert pos["ts"] == 12345
    assert pos["updated_at"] > 0


def test_get_empty_then_delete(server):
    assert _get_position(server) is None
    _req(server, "PUT", "/api/reading-position", {"folder": "long", "page": 1, "total": 30})
    assert _get_position(server)["folder"] == "long"
    status, _ = _req(server, "DELETE", "/api/reading-position")
    assert status == 200
    assert _get_position(server) is None


def test_put_overwrites_single_slot(server):
    _req(server, "PUT", "/api/reading-position", {"folder": "long", "page": 1, "total": 30})
    _req(server, "PUT", "/api/reading-position", {"folder": "series/vol1", "page": 0, "total": 2})
    pos = _get_position(server)
    assert pos["folder"] == "series/vol1" and pos["page"] == 0


def test_put_defaults_ts(server):
    _req(server, "PUT", "/api/reading-position", {"folder": "long", "page": 2, "total": 30})
    assert _get_position(server)["ts"] > 0


def test_put_validation(server):
    for body in (
        "not-a-dict",
        {"page": 0, "total": 1},                            # folder missing
        {"folder": "", "page": 0, "total": 1},              # empty folder
        {"folder": "/abs", "page": 0, "total": 1},          # leading slash
        {"folder": "f", "page": -1, "total": 1},            # negative page
        {"folder": "f", "page": True, "total": 1},          # bool page
        {"folder": "f", "page": 0},                         # total missing
        {"folder": "f", "page": 0, "total": 0},             # zero total
        {"folder": "f", "page": 0, "total": "x"},           # non-int total
        {"folder": "f", "page": 0, "total": 1, "ts": -5},   # negative ts
        # json.loads accepts NaN/Infinity — int() would 500, must be 400
        {"folder": "f", "page": 0, "total": 1, "ts": float("nan")},
        {"folder": "f", "page": 0, "total": 1, "ts": float("inf")},
    ):
        status, _ = _req(server, "PUT", "/api/reading-position", body)
        assert status == 400, body


def test_put_stale_ts_rejected(server):
    """The server compares client ts — an older PUT can't clobber newer."""
    base = int(time.time() * 1000)
    _req(server, "PUT", "/api/reading-position",
         {"folder": "newer", "page": 5, "total": 30, "ts": base})
    status, _ = _req(server, "PUT", "/api/reading-position",
                     {"folder": "older", "page": 0, "total": 30, "ts": base - 1000})
    assert status == 200
    pos = _get_position(server)
    assert pos["folder"] == "newer" and pos["page"] == 5
    # Equal-or-newer still lands (idempotent resends, genuinely newer reads).
    status, _ = _req(server, "PUT", "/api/reading-position",
                     {"folder": "newest", "page": 1, "total": 30, "ts": base + 1000})
    assert status == 200
    assert _get_position(server)["folder"] == "newest"


def test_delete_tombstone_blocks_resurrection(server):
    """DELETE leaves a tombstone: the wiped record can't come back."""
    base = int(time.time() * 1000)
    _req(server, "PUT", "/api/reading-position",
         {"folder": "long", "page": 2, "total": 30, "ts": base})
    _req(server, "DELETE", "/api/reading-position")
    assert _get_position(server) is None
    # The same record (another device's boot self-heal) and anything older
    # are both rejected — only a genuinely newer read lands.
    for stale_ts in (base, base - 1000):
        _req(server, "PUT", "/api/reading-position",
             {"folder": "long", "page": 2, "total": 30, "ts": stale_ts})
        assert _get_position(server) is None, f"ts={stale_ts} resurrected the wipe"
    _req(server, "PUT", "/api/reading-position",
         {"folder": "long", "page": 9, "total": 30, "ts": base + 5000})
    assert _get_position(server)["page"] == 9


def test_put_future_ts_rejected(server):
    """A far-future ts is a 400, not a poisoned slot — an unchecked one
    would reject every honest PUT as stale and survive even DELETE."""
    future = int(time.time() * 1000) + 365 * 86400 * 1000
    status, _ = _req(server, "PUT", "/api/reading-position",
                     {"folder": "wrong/clock", "page": 3, "total": 30, "ts": future})
    assert status == 400
    assert _get_position(server) is None
    # Within-slack drift (~minutes) is still accepted — NTP isn't perfect.
    status, _ = _req(server, "PUT", "/api/reading-position",
                     {"folder": "f/b", "page": 1, "total": 30,
                      "ts": int(time.time() * 1000) + 60_000})
    assert status == 200


# ── e2e: two devices ─────────────────────────────────────────────────


def test_resume_position_syncs_across_contexts(browser, server):
    """Device A's read position becomes device B's resume pill + open page."""
    ctx_a = browser.new_context(viewport={"width": 390, "height": 844})
    page_a = ctx_a.new_page()
    try:
        page_a.goto(server)
        page_a.wait_for_selector(".tile")
        page_a.locator('.tile[data-path="long"]').click()
        expect(page_a.locator("#counter")).to_have_text("1 / 30")
        for _ in range(3):  # ArrowLeft advances in the RTL reader
            page_a.keyboard.press("ArrowLeft")
        expect(page_a.locator("#counter")).to_have_text("4 / 30")
        # Skip the 2s trailing debounce — flush deterministically.
        page_a.evaluate("() => window.ProgressSync.flush()")
        pos = _wait_position(server, lambda p: p.get("folder") == "long" and p.get("page") == 3)
        assert pos, "page turns never reached the server"
    finally:
        ctx_a.close()

    ctx_b = browser.new_context(viewport={"width": 390, "height": 844})
    page_b = ctx_b.new_page()
    try:
        page_b.goto(server)
        page_b.wait_for_selector(".tile")
        expect(page_b.locator("#resume-btn-text")).to_have_text(
            "📖 続きから: long p.4", timeout=8000
        )
        page_b.locator("#resume-btn").click()
        expect(page_b.locator("#counter")).to_have_text("4 / 30", timeout=8000)
    finally:
        ctx_b.close()


def test_turn_burst_coalesces_puts(page, server):
    """Multiple SAVES inside the 2s push window still send only once.

    Turns are spaced 650ms apart — past the 500ms saveDebounce — so each
    one really calls push(); the trailing-edge timer must coalesce them.
    """
    puts = []

    def on_route(route):
        if route.request.method == "PUT" and "/api/reading-position" in route.request.url:
            puts.append(route.request.url)
        route.continue_()

    page.route("**/api/reading-position", on_route)
    page.locator('.tile[data-path="long"]').click()
    expect(page.locator("#counter")).to_have_text("1 / 30")
    baseline = len(puts)
    for _ in range(5):
        page.keyboard.press("ArrowLeft")
        page.wait_for_timeout(650)
    expect(page.locator("#counter")).to_have_text("6 / 30")
    page.wait_for_timeout(4500)  # last save (0.5s) + push debounce (2s) + margin
    sent = len(puts) - baseline
    assert sent == 1, f"expected 1 coalesced PUT, got {sent}"


def test_put_failure_retries_with_backoff(page, server):
    """A failing PUT must not hot-loop — retries are bounded by backoff."""
    puts = []

    def on_route(route):
        if route.request.method == "PUT" and "/api/reading-position" in route.request.url:
            puts.append(route.request.url)
            route.fulfill(status=500)
        else:
            route.continue_()

    page.route("**/api/reading-position", on_route)
    page.locator('.tile[data-path="long"]').click()
    expect(page.locator("#counter")).to_have_text("1 / 30")
    page.keyboard.press("ArrowLeft")
    # Sends: ~2.5s (open debounce) then exponential backoff (4s+).
    # Without backoff this loop produces hundreds of PUTs per second.
    page.wait_for_timeout(6500)
    assert 1 <= len(puts) <= 4, f"PUT retries should be bounded, got {len(puts)}"


def test_failed_pull_never_selfheals(page, server):
    """A failed boot pull must not push local state up over a remote record."""
    base = int(time.time() * 1000)
    _req(server, "PUT", "/api/reading-position",
         {"folder": "remote/book", "page": 7, "total": 30, "ts": base})
    puts = []

    def on_route(route):
        if "/api/reading-position" not in route.request.url:
            route.continue_()
            return
        if route.request.method == "GET":
            route.fulfill(status=500)
        else:
            puts.append(route.request.url)
            route.continue_()

    page.route("**/api/reading-position", on_route)
    page.evaluate(
        "localStorage.setItem('bookq_progress', "
        "JSON.stringify({'local/book': {page: 0, total: 10, ts: %d}}))" % (base + 1000)
    )
    page.reload()
    page.wait_for_selector(".tile")
    page.wait_for_timeout(3000)
    pos = _get_position(server)
    assert pos and pos["folder"] == "remote/book", "failed pull clobbered the remote record"
    assert not puts, "a blind self-heal PUT fired despite the failed GET"


def test_pagehide_flush_sends_current_position(page, server):
    """The last save on pagehide flushes immediately — the 2s timer dies
    with the page, so the final position must not depend on it."""
    page.locator('.tile[data-path="long"]').click()
    expect(page.locator("#counter")).to_have_text("1 / 30")
    page.keyboard.press("ArrowLeft")   # still inside the 500ms saveDebounce
    page.evaluate("() => window.dispatchEvent(new Event('pagehide'))")
    pos = _wait_position(server, lambda p: p.get("page") == 1, timeout=1.5)
    assert pos, "pagehide did not flush the final save promptly"


def test_visibilitychange_flush_sends_current_position(page, server):
    """The iOS-reliable path: visibilitychange(hidden) must also flush —
    it fires on document while app.js saves on window, so ordering is
    the thing under test."""
    page.locator('.tile[data-path="long"]').click()
    expect(page.locator("#counter")).to_have_text("1 / 30")
    page.keyboard.press("ArrowLeft")
    page.evaluate(
        """() => {
            Object.defineProperty(document, 'visibilityState',
                { get: () => 'hidden', configurable: true });
            document.dispatchEvent(new Event('visibilitychange', { bubbles: true }));
        }"""
    )
    pos = _wait_position(server, lambda p: p.get("page") == 1, timeout=1.5)
    assert pos, "visibilitychange(hidden) did not flush the final save promptly"


def test_boot_pull_merges_newer_remote(page, server):
    """A strictly-newer remote record lands in localStorage and drives the pill."""
    _req(server, "PUT", "/api/reading-position",
         {"folder": "series/vol1", "page": 1, "total": 2,
          "ts": int(time.time() * 1000)})
    page.evaluate(
        "localStorage.setItem('bookq_progress', "
        "JSON.stringify({'long': {page: 0, total: 30, ts: 1}}))"
    )
    page.reload()
    page.wait_for_selector(".tile")
    page.wait_for_function(
        """() => {
            const p = JSON.parse(localStorage.getItem('bookq_progress') || '{}');
            return p['series/vol1'] && p['series/vol1'].page === 1;
        }""",
        timeout=8000,
    )
    expect(page.locator("#resume-btn-text")).to_have_text("📖 続きから: vol1 p.2")


def test_boot_pushes_newer_local_to_server(page, server):
    """A newer LOCAL newest self-heals the (stale/wiped) server record."""
    _req(server, "PUT", "/api/reading-position",
         {"folder": "long", "page": 1, "total": 30, "ts": 1})
    page.evaluate(
        "localStorage.setItem('bookq_progress', "
        "JSON.stringify({'series/vol1': {page: 0, total: 2, ts: %d}}))"
        % int(time.time() * 1000)
    )
    page.reload()
    page.wait_for_selector(".tile")
    pos = _wait_position(server, lambda p: p.get("folder") == "series/vol1")
    assert pos, "local newest was never pushed up to the server"


def test_wipe_all_clears_server_position(page, server):
    _req(server, "PUT", "/api/reading-position", {"folder": "long", "page": 2, "total": 30})
    page.goto(server + "/static/offline-harness.html?harness=1")
    page.wait_for_selector("#harness-status")
    page.wait_for_function("() => window.OfflineCore && window.doWipeAll")
    page.evaluate("async () => { await window.doWipeAll(); }")
    # doWipeAll awaits wipeClientData, which awaits ProgressSync.remove() —
    # the DELETE has already completed when the evaluate returns.
    assert _get_position(server) is None
