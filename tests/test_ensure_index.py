"""Regression tests for #113: _ensure_index TTL reuse + double-checked locking.

Single-worker hypercorn means one process-wide metadata_index_cache with a 5-minute
TTL, guarded by a single asyncio lock. These tests pin that behavior with stub
requests (no TestClient/httpx): cache hit returns the identical object, a stale
timestamp triggers exactly one rebuild, and the module-level lock object is
process-singleton (#110 removed the _get_index_lock accessor as dead code).

One asyncio.run() per test — the module-level asyncio.Lock must not be shared
across multiple event loops.
"""

import asyncio
import threading
import time
from types import SimpleNamespace

from app.config import Settings
from app.routes import INDEX_CACHE_TTL, _ensure_index, _index_lock


def _drive(coro):
    """Run *coro* to completion on a fresh event loop in a helper thread.

    Playwright's sync API (used by the browser tests in this suite) leaves a
    running event loop behind in the main thread, so plain asyncio.run() fails
    with "cannot be called from a running event loop" when this module runs
    after them. A dedicated thread always has a clean loop state. The
    module-level _index_lock is never contended here (strictly sequential
    awaits), so it never binds to any of these short-lived loops.
    """
    outcome = {}

    def target():
        outcome["value"] = asyncio.run(coro)

    worker = threading.Thread(target=target)
    worker.start()
    worker.join()
    return outcome["value"]


def _stub_request(tmp_path):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=Settings(downloads_dir=str(tmp_path)),
                metadata_index_cache=None,
            )
        )
    )


def _ensure_twice_and_report(tmp_path):
    async def main():
        request = _stub_request(tmp_path)
        first = await _ensure_index(request)
        second = await _ensure_index(request)
        return first, second

    return _drive(main())


def test_ensure_index_reuses_cached_object(tmp_path):
    first, second = _ensure_twice_and_report(tmp_path)
    assert second is first


def test_ensure_index_rebuilds_after_ttl_expiry(tmp_path):
    async def main():
        request = _stub_request(tmp_path)
        first = await _ensure_index(request)
        request.app.state.metadata_index_cache["timestamp"] = time.time() - INDEX_CACHE_TTL - 1
        rebuilt = await _ensure_index(request)
        return first, rebuilt

    first, rebuilt = _drive(main())
    assert rebuilt is not first


def test_ensure_index_hit_just_inside_ttl(tmp_path):
    # A timestamp just inside the TTL window still counts as fresh (<= comparison).
    # Exact-boundary equality is timing-racy (microseconds elapse between stamping
    # and re-check), so pin TTL-5s as the deterministic fresh side.
    async def main():
        request = _stub_request(tmp_path)
        first = await _ensure_index(request)
        request.app.state.metadata_index_cache["timestamp"] = time.time() - INDEX_CACHE_TTL + 5
        second = await _ensure_index(request)
        return first, second

    first, second = _drive(main())
    assert second is first


def test_index_lock_is_process_singleton():
    import app.routes as routes

    assert isinstance(_index_lock, asyncio.Lock)
    assert routes._index_lock is _index_lock
