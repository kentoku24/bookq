"""Connectivity state machine + offline browse filtering (docs/offline-spec.md).

Covers:
  - 3-state machine: offline→connecting→online, chip labels (接続中…)
  - reportFail demotes online→connecting; reportSuccess lands online mid-cycle
  - no auto-retry while offline (zero probe fetches once the budget is spent)
  - explicit retry via the #offline-status chip; OS `online` event recovery
  - offline-first boot: tiles render from IDB before connectivity resolves
  - §6 browse filter: downloaded branches only, greyed episodes, tap guard
  - spec §5: SW-cache-marked responses must NOT count as online evidence

Run:
    uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_connectivity.py -q
"""
import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

REPO_ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_DIR = REPO_ROOT / ".omo" / "evidence"
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)


def _is_webkit():
    return os.environ.get("BROWSER") == "webkit"


def goto_offline(page, server):
    page.goto(server + "/?offline=1")
    page.wait_for_selector(".tile", timeout=10000)


def wait_for_sw(page, timeout=15000):
    try:
        page.wait_for_function(
            "() => navigator.serviceWorker && (navigator.serviceWorker.controller !== null || !!navigator.serviceWorker.ready)",
            timeout=timeout,
        )
        return True
    except Exception:
        return False


def _conn_state(page):
    return page.evaluate("() => window.Connectivity ? window.Connectivity.state : 'missing'")


def _wait_state(page, state, timeout=20000):
    page.wait_for_function(
        "(s) => window.Connectivity && window.Connectivity.state === s",
        arg=state, timeout=timeout,
    )


def _reject_all_fetch(page):
    page.evaluate(
        """() => {
            if (!window.__origFetchConn) window.__origFetchConn = window.fetch;
            window.fetch = (input, init) => Promise.reject(new Error('unreachable'));
        }"""
    )


def _restore_fetch(page):
    page.evaluate(
        "() => { try { if(window.__origFetchConn) { window.fetch = window.__origFetchConn; delete window.__origFetchConn; } } catch {} }"
    )


# ── state machine ─────────────────────────────────────────────────────────────

def test_boot_lands_online(page, server):
    """Startup: offline-first boot, connecting cycle, lands online (spec §7)."""
    goto_offline(page, server)
    wait_for_sw(page)
    _wait_state(page, "online")
    status = page.locator("#offline-status").text_content() or ""
    assert "オンライン" in status, f"chip must show online after boot connect: {status!r}"
    # and once online, the full tree is fetched and tiles are unfiltered
    assert page.locator('.tile[data-path="group"]').count() == 1


def test_report_fail_demotes_and_budget_lands_offline(page, server):
    """online → (all requests dead) → connecting → offline in the finite budget."""
    goto_offline(page, server)
    wait_for_sw(page)
    _wait_state(page, "online")
    _reject_all_fetch(page)
    try:
        page.evaluate("() => window.updateOnlineStatus()")
        # connecting shows 接続中 before the budget is spent
        _wait_state(page, "connecting", timeout=3000)
        chip = page.locator("#offline-status").text_content() or ""
        assert "接続中" in chip, f"connecting chip must show 接続中: {chip!r}"
        # finite budget (3 attempts + backoff) then lands offline
        _wait_state(page, "offline", timeout=20000)
        assert "offline" in (page.locator("#offline-status").get_attribute("class") or "")
    finally:
        _restore_fetch(page)
        page.evaluate("() => window.Connectivity && window.Connectivity.connect()")
        _wait_state(page, "online")


def test_no_auto_retry_while_offline(page, server):
    """spec §3: once offline the machine must not keep probing on its own."""
    goto_offline(page, server)
    wait_for_sw(page)
    _wait_state(page, "online")
    _reject_all_fetch(page)
    try:
        page.evaluate("() => window.updateOnlineStatus()")
        _wait_state(page, "offline", timeout=20000)
        # count health probes for ~4s — must stay zero (budget exhausted)
        page.evaluate(
            """() => {
                window.__probeHits = 0;
                const orig = window.fetch;
                window.fetch = (input, init) => {
                    const u = typeof input === 'string' ? input : (input && input.url) || '';
                    if (u.includes('/api/v1/health')) window.__probeHits++;
                    return Promise.reject(new Error('unreachable'));
                };
            }"""
        )
        page.wait_for_timeout(4000)
        hits = page.evaluate("() => window.__probeHits")
        assert hits == 0, f"offline must not auto-retry — got {hits} probes in 4s"
    finally:
        _restore_fetch(page)
        page.evaluate("() => window.Connectivity && window.Connectivity.connect()")
        _wait_state(page, "online")


def test_explicit_retry_via_chip(page, server):
    """spec §3: chip tap is the user's explicit retry — offline → connecting → online."""
    goto_offline(page, server)
    wait_for_sw(page)
    _wait_state(page, "online")
    _reject_all_fetch(page)
    try:
        page.evaluate("() => window.updateOnlineStatus()")
        _wait_state(page, "offline", timeout=20000)
        _restore_fetch(page)
        page.locator("#offline-status").click()
        _wait_state(page, "online", timeout=10000)
        assert "オフライン" not in (page.locator("#offline-status").text_content() or "")
    finally:
        _restore_fetch(page)


def test_online_event_recovers(page, server):
    """spec §3: an OS `online` event while offline starts a connect cycle."""
    goto_offline(page, server)
    wait_for_sw(page)
    _wait_state(page, "online")
    _reject_all_fetch(page)
    try:
        page.evaluate("() => window.updateOnlineStatus()")
        _wait_state(page, "offline", timeout=20000)
        _restore_fetch(page)
        page.evaluate("() => window.dispatchEvent(new Event('online'))")
        _wait_state(page, "online", timeout=10000)
    finally:
        _restore_fetch(page)


def test_report_success_lands_online_mid_cycle(page, server):
    """spec §5: a real (non-SW-cache) response proves reachability even
    mid-connect — the cycle short-circuits to online."""
    goto_offline(page, server)
    wait_for_sw(page)
    _wait_state(page, "online")
    _reject_all_fetch(page)
    try:
        page.evaluate("() => window.updateOnlineStatus()")
        _wait_state(page, "connecting", timeout=3000)
        # a real (non-SW-marked) response landing mid-cycle proves
        # reachability — the running cycle must short-circuit to online.
        _restore_fetch(page)
        page.evaluate("() => window.Connectivity.fetch('/api/v1/health?online_check=1').catch(()=>{})")
        _wait_state(page, "online", timeout=3000)
        # the cycle stays stopped — no demotion back to connecting
        page.wait_for_timeout(500)
        assert _conn_state(page) == "online"
    finally:
        _restore_fetch(page)


def test_swcache_marked_response_is_not_online_evidence(page, server):
    """spec §5: an ok Response carrying X-Bookq-SWCache means the network leg
    failed — it must demote online→connecting, never confirm online."""
    goto_offline(page, server)
    wait_for_sw(page)
    _wait_state(page, "online")
    # Fabricate the real-world shape: app requests get a marked ok response
    # (SW cache fallback) while the network-only probe still fails — the
    # mark must read as failure evidence, never as being online. (Probes
    # use cache:'no-store', so a marked probe response cannot happen in
    # production; letting the stub return one would ping-pong the machine.)
    page.evaluate(
        """() => {
            const orig = window.fetch;
            window.__origMarked = orig;
            window.fetch = (input, init) => {
                const u = typeof input === 'string' ? input : (input && input.url) || '';
                if (u.includes('/api/v1/health')) return Promise.reject(new Error('unreachable'));
                return Promise.resolve(new Response('{"ok":true}', {
                    status: 200,
                    headers: { 'Content-Type': 'application/json', 'X-Bookq-SWCache': '1' },
                }));
            };
        }"""
    )
    try:
        page.evaluate("() => window.Connectivity.fetch('/api/v1/tree').catch(()=>{})")
        _wait_state(page, "connecting", timeout=5000)
        # probes keep failing → the finite budget lands it in offline
        _wait_state(page, "offline", timeout=20000)
    finally:
        page.evaluate("() => { if(window.__origMarked) window.fetch = window.__origMarked; }")
        page.evaluate("() => window.Connectivity && window.Connectivity.connect()")
        _wait_state(page, "online")


def test_offline_first_boot_renders_before_connect(page, server):
    """spec §7: an offline relaunch renders the (filtered) grid from IDB
    immediately — tiles must exist while connectivity is still connecting."""
    goto_offline(page, server)
    wait_for_sw(page)
    _wait_state(page, "online")
    page.evaluate("async () => { await window.downloadWork('simple_manga'); }")
    expect(page.locator('.tile[data-path="simple_manga"] .offline-btn.state-complete').first).to_be_visible(timeout=15000)
    if _is_webkit():
        pytest.skip("WebKit Playwright cannot navigate while context is offline")
    if os.environ.get("BROWSER") == "safari":
        pytest.skip(
            "safaridriver has no pre-boot script hook — the add_init_script "
            "fetch stub lands after the app's boot fetch already ran"
        )
    # Simulate a dead server at the app layer on the next load: an init
    # script (re-runs on the reload, dies with the test context) hangs
    # every /api fetch so the connect cycle keeps probing while the UI
    # must already render from IDB (spec §7: offline-first boot). NB:
    # context.set_offline + reload yields a chrome-error page — the
    # navigation never reaches the SW — so this models it instead.
    page.add_init_script(
        """(() => {
            const orig = window.fetch;
            window.fetch = (input, init) => {
                const u = typeof input === 'string' ? input : (input && input.url) || '';
                if (/(\\/api\\/|\\/thumb\\/|\\/image\\/|\\/view\\/)/.test(u)) return new Promise(() => {});
                return orig(input, init);
            };
        })()"""
    )
    page.reload(wait_until="domcontentloaded")
    # grid must paint from IDB before the connect budget finishes —
    # poll for the tile while the state is not yet online.
    page.wait_for_selector('.tile[data-path="simple_manga"]', timeout=10000)
    st = _conn_state(page)
    assert st in ("connecting", "offline"), f"boot render must precede online, state={st!r}"
    # spec §6 filter applies from the very first offline paint
    assert page.locator('.tile[data-path="group"]').count() == 0
    assert page.locator(".off-unavail").count() > 0, \
        "non-downloaded episodes must render greyed from the first offline paint"


# ── §6 browse filter ──────────────────────────────────────────────────────────

def test_offline_browse_filter_and_tap_guard(page, server):
    """spec §6: downloaded branches only + greyed episodes + tap block."""
    goto_offline(page, server)
    wait_for_sw(page)
    _wait_state(page, "online")
    page.evaluate("async () => { await window.downloadWork('simple_manga'); }")
    expect(page.locator('.tile[data-path="simple_manga"] .offline-btn.state-complete').first).to_be_visible(timeout=15000)
    _reject_all_fetch(page)
    try:
        page.evaluate("() => window.updateOnlineStatus()")
        _wait_state(page, "offline", timeout=20000)
        # downloaded work stays; level also keeps every root episode (greyed)
        expect(page.locator('.tile[data-path="simple_manga"]')).to_be_visible(timeout=5000)
        assert "off-unavail" not in (page.locator('.tile[data-path="simple_manga"]').get_attribute("class") or ""), \
            "downloaded tile must not be greyed"
        # non-downloaded root episode → greyed + aria-disabled
        long_tile = page.locator('.tile[data-path="long"]')
        expect(long_tile).to_be_visible(timeout=5000)
        cls = long_tile.get_attribute("class") or ""
        assert "off-unavail" in cls, f"non-downloaded episode must be greyed: {cls!r}"
        assert long_tile.get_attribute("aria-disabled") == "true"
        # intermediate with no download is hidden entirely
        assert page.locator('.tile[data-path="group"]').count() == 0
        assert page.locator('.tile[data-path="series"]').count() == 0
        # tapping the greyed tile is blocked — no viewer, no navigation.
        # dispatch_event bypasses Playwright's aria-disabled actionability
        # refusal so the app's own tap guard is what gets tested.
        long_tile.dispatch_event("click")
        page.wait_for_timeout(700)
        counter = page.locator("#counter").text_content() or ""
        assert " / " not in counter, f"greyed tap must not open the viewer: {counter!r}"
        # tapping the downloaded tile opens the viewer
        page.locator('.tile[data-path="simple_manga"]').click()
        expect(page.locator("#counter")).to_have_text(re.compile(r"1\s*/\s*5"), timeout=8000)
        try:
            page.screenshot(path=str(EVIDENCE_DIR / "offline-spec-browse-filter.png"))
        except Exception:
            pass
    finally:
        _restore_fetch(page)
        page.context.set_offline(False)


def test_download_start_blocked_offline(page, server):
    """spec §6 feature gate: downloadWork must refuse while offline."""
    goto_offline(page, server)
    wait_for_sw(page)
    _wait_state(page, "online")
    _reject_all_fetch(page)
    try:
        page.evaluate("() => window.updateOnlineStatus()")
        _wait_state(page, "offline", timeout=20000)
        out = page.evaluate(
            """async () => {
                try { await window.downloadWork('long'); } catch(e) { return 'threw:'+e; }
                const r = await window.OfflineStore.get('long');
                return r ? 'record-written' : 'refused';
            }"""
        )
        assert out == "refused", f"downloadWork must refuse offline, got {out!r}"
    finally:
        _restore_fetch(page)
        page.evaluate("() => window.Connectivity && window.Connectivity.connect()")
        _wait_state(page, "online")
