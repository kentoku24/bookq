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
    ):
        status, _ = _req(server, "PUT", "/api/reading-position", body)
        assert status == 400, body


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
    """10 rapid turns → coalesced PUTs, not one per turn."""
    puts = []

    def on_route(route):
        if route.request.method == "PUT" and "/api/reading-position" in route.request.url:
            puts.append(route.request.url)
        route.continue_()

    page.route("**/api/reading-position", on_route)
    page.locator('.tile[data-path="long"]').click()
    expect(page.locator("#counter")).to_have_text("1 / 30")
    for _ in range(10):
        page.keyboard.press("ArrowLeft")
    expect(page.locator("#counter")).to_have_text("11 / 30")
    page.wait_for_timeout(5000)  # save debounce (0.5s) + push debounce (2s) + margin
    assert 1 <= len(puts) <= 3, f"expected coalesced PUTs, got {len(puts)}"


def test_boot_pull_merges_newer_remote(page, server):
    """A strictly-newer remote record lands in localStorage and drives the pill."""
    _req(server, "PUT", "/api/reading-position",
         {"folder": "series/vol1", "page": 1, "total": 2, "ts": 9999999999999})
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
        "JSON.stringify({'series/vol1': {page: 0, total: 2, ts: 9999999999999}}))"
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
