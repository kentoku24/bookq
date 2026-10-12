"""Stale-serve + background refresh (plan thumb-warmup-stale-serve Todo 1).

TTL expiry of ``metadata_index_cache`` must no longer block the first
request: a stale cache is returned IMMEDIATELY while a single-flight
background task rebuilds and swaps the cache. No cache at all keeps the
synchronous build path.

One asyncio.run() per test in a helper thread — Playwright's sync API
leaves a running loop behind in the main thread, and the module-level
_index_lock must not be shared across loops (same constraint as
tests/test_ensure_tree_and_index.py).

Stale-path awaits are wrapped in ``asyncio.wait_for`` so the pre-fix code
fails fast (TimeoutError) instead of hanging the suite forever.

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_stale_serve.py -q
"""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.routes import INDEX_CACHE_TTL, _ensure_tree_and_index
from tests.helpers import drive


def _stub_request(tmp_path):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=Settings(downloads_dir=str(tmp_path)),
                metadata_index_cache=None,
            )
        )
    )


def _age_cache(request, generation=7):
    """Force the request's cache into the stale window, keeping its objects."""
    cache = request.app.state.metadata_index_cache
    cache["timestamp"] = time.time() - INDEX_CACHE_TTL - 1
    cache["generation"] = generation
    return cache["tree"], cache["index"]


async def _await_refresh(state, timeout=10.0):
    """Await the scheduled background refresh task to completion.

    Tolerant of the fast-failure race: a scan that raises immediately may
    let the background task settle (and clear its handle) before this
    runs — one loop tick then re-reads the handle, and a still-absent
    handle means already settled. Callers pin "a refresh was attempted"
    with their own scan-invocation counter, not with this helper.
    """
    task = getattr(state, "index_refresh_task", None)
    if task is None:
        await asyncio.sleep(0)
        task = getattr(state, "index_refresh_task", None)
    if task is not None:
        await asyncio.wait_for(asyncio.ensure_future(task), timeout)
    # The task handle is cleared once the refresh settles.
    assert getattr(state, "index_refresh_task", None) is None


async def _ensure_stale_now(request):
    """Await one stale-path ensure, failing fast if it blocks on the scan."""
    try:
        return await asyncio.wait_for(_ensure_tree_and_index(request), 5)
    except asyncio.TimeoutError:
        pytest.fail("stale ensure blocked waiting for the scan (no stale-serve)")


def test_stale_returns_immediately_without_waiting_for_scan(tmp_path, monkeypatch):
    """The stale call resolves while the rebuild scan is still blocked."""
    import app.routes as routes

    started = threading.Event()
    new_tree = [{"name": "fresh", "type": "folder"}]
    new_index = object()

    async def main():
        release = asyncio.Event()
        finished = []

        async def blocked_scan(downloads_path, list_archives=True):
            started.set()
            await release.wait()
            finished.append(True)
            return new_tree, new_index, 0, 0, 0, 0, 0, 0

        request = _stub_request(tmp_path)
        first_tree, first_index = await _ensure_tree_and_index(request)
        monkeypatch.setattr(routes, "_scan_and_rebuild", blocked_scan)
        stale_tree, stale_index = _age_cache(request)
        assert stale_tree is first_tree and stale_index is first_index
        got_tree, got_index = await _ensure_stale_now(request)
        # Immediate: the stale objects come back, scan still blocked.
        assert got_tree is stale_tree and got_index is stale_index
        deadline = time.monotonic() + 5
        while not started.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert started.is_set(), "background refresh must have started the scan"
        assert not finished, "ensure must not wait for the scan"
        release.set()
        await _await_refresh(request.app.state)
        cache = request.app.state.metadata_index_cache
        assert cache["tree"] is new_tree and cache["index"] is new_index
        # Swap stamps fresh and preserves the fill generation (ETag stability).
        assert time.time() - cache["timestamp"] <= INDEX_CACHE_TTL
        assert cache["generation"] == 7
        return True

    assert drive(main())


def test_concurrent_stale_requests_trigger_single_rebuild(tmp_path, monkeypatch):
    """N concurrent stale requests schedule exactly one background rebuild."""
    import app.routes as routes

    calls = []
    release = threading.Event()
    new_tree = [{"name": "fresh", "type": "folder"}]
    new_index = object()

    async def counting_scan(downloads_path, list_archives=True):
        calls.append(1)
        await asyncio.to_thread(release.wait, 10)
        return new_tree, new_index, 0, 0, 0, 0, 0, 0

    async def main():
        request = _stub_request(tmp_path)
        await _ensure_tree_and_index(request)
        monkeypatch.setattr(routes, "_scan_and_rebuild", counting_scan)
        stale_tree, stale_index = _age_cache(request)
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*(_ensure_tree_and_index(request) for _ in range(5))), 15
            )
        finally:
            release.set()
        assert all(t is stale_tree and i is stale_index for t, i in results)
        await _await_refresh(request.app.state)
        assert len(calls) == 1, f"single-flight violated: {len(calls)} scans"
        assert request.app.state.metadata_index_cache["tree"] is new_tree
        return True

    assert drive(main())


def test_background_failure_keeps_old_cache(tmp_path, monkeypatch):
    """A failing background rebuild never breaks serving: stale keeps flowing."""
    import app.routes as routes

    calls = []

    async def failing_scan(downloads_path, list_archives=True):
        calls.append(1)
        raise RuntimeError("scan exploded")

    async def main():
        request = _stub_request(tmp_path)
        await _ensure_tree_and_index(request)
        monkeypatch.setattr(routes, "_scan_and_rebuild", failing_scan)
        stale_tree, stale_index = _age_cache(request)
        got_tree, got_index = await _ensure_stale_now(request)
        assert got_tree is stale_tree and got_index is stale_index
        await _await_refresh(request.app.state)  # must not raise
        assert len(calls) == 1, "stale serve must attempt exactly one background refresh"
        cache = request.app.state.metadata_index_cache
        assert cache["tree"] is stale_tree and cache["index"] is stale_index
        return True

    assert drive(main())


def test_manual_refresh_wins_over_late_background(tmp_path, monkeypatch):
    """A late-finishing background task must not clobber a fresh manual refresh."""
    import app.routes as routes

    bg_tree = [{"name": "bg", "type": "folder"}]
    bg_index = object()

    async def main():
        release = asyncio.Event()

        async def slow_scan(downloads_path, list_archives=True):
            await release.wait()
            return bg_tree, bg_index, 0, 0, 0, 0, 0, 0

        request = _stub_request(tmp_path)
        await _ensure_tree_and_index(request)
        monkeypatch.setattr(routes, "_scan_and_rebuild", slow_scan)
        _age_cache(request)
        await _ensure_stale_now(request)  # schedules the slow background rebuild
        # Manual /api/refresh lands while the background scan is still running:
        # synchronous rebuild under the lock, stamping a fresh timestamp.
        manual_tree = [{"name": "manual", "type": "folder"}]
        manual_index = object()
        request.app.state.metadata_index_cache = {
            "tree": manual_tree,
            "index": manual_index,
            "timestamp": time.time(),
            "generation": 7,
        }
        release.set()
        await _await_refresh(request.app.state)
        cache = request.app.state.metadata_index_cache
        assert cache["tree"] is manual_tree and cache["index"] is manual_index
        assert cache["generation"] == 7
        return True

    assert drive(main())


def test_cold_cache_still_builds_synchronously(tmp_path, monkeypatch):
    """No cache at all keeps today's synchronous build path (no background)."""
    import app.routes as routes

    calls = []

    real_scan = routes._scan_and_rebuild

    async def counting_scan(downloads_path, list_archives=True):
        calls.append(1)
        return await real_scan(downloads_path, list_archives=list_archives)

    monkeypatch.setattr(routes, "_scan_and_rebuild", counting_scan)

    async def main():
        request = _stub_request(tmp_path)
        tree, index = await _ensure_tree_and_index(request)
        assert tree is request.app.state.metadata_index_cache["tree"]
        assert index is request.app.state.metadata_index_cache["index"]
        assert len(calls) == 1
        assert getattr(request.app.state, "index_refresh_task", None) is None
        return True

    assert drive(main())


def test_background_gate_race_serves_winner(tmp_path, monkeypatch):
    """(h) re-derived at unit level (from tests/test_index_gate.py, deleted at
    HTTP level): the gate's post-walk re-read must serve the winner when a
    concurrent rebuild replaced the cache dict while the signature walked —
    no rebuild of its own, no clobber, winner tree intact."""
    import app.routes as routes

    rebuild_calls = []
    real_scan = routes._scan_and_rebuild

    async def counting_scan(downloads_path, list_archives=True):
        rebuild_calls.append(1)
        return await real_scan(downloads_path, list_archives=list_archives)

    monkeypatch.setattr(routes, "_scan_and_rebuild", counting_scan)

    async def main():
        request = _stub_request(tmp_path)
        await _ensure_tree_and_index(request)
        old = request.app.state.metadata_index_cache
        real_sig = "gatesig"
        winner_tree = [{"name": "zz_race_winner", "type": "folder", "children": []}]

        def fake_sig(path):
            walk_calls["n"] += 1
            winner = {
                "tree": winner_tree,
                "index": old["index"],
                "timestamp": time.time(),
                "generation": old.get("generation", 1),
                "signature": real_sig,
            }
            request.app.state.metadata_index_cache = winner
            return real_sig

        walk_calls = {"n": 0}
        monkeypatch.setattr(routes, "compute_signature", fake_sig)
        # Age the cache so the ensure takes the stale path, and give the
        # current cache the matching signature the fake walk will return.
        old["signature"] = real_sig
        _age_cache(request)
        got_tree, _ = await _ensure_stale_now(request)
        # The fake walk installed the winner before the re-read: the gate
        # must re-check and leave the winner untouched.
        await _await_refresh(request.app.state)
        assert got_tree is old["tree"]
        cache = request.app.state.metadata_index_cache
        assert cache["tree"] is winner_tree, "must serve the racing winner"
        assert len(rebuild_calls) == 1, "only the cold build may scan"
        return True

    assert drive(main())
