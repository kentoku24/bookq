"""Regression tests for #113: _ensure_index TTL reuse + double-checked locking.

Single-worker hypercorn means one process-wide metadata_index_cache with a 5-minute
TTL, guarded by a single asyncio lock. These tests pin that behavior with stub
requests (no TestClient/httpx): cache hit returns the identical object, an expired
cache is served immediately with the refresh moving to a single-flight background
task that gates on the filesystem signature, and the module-level lock object is
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


def _ensure_twice_and_report(tmp_path):
    async def main():
        request = _stub_request(tmp_path)
        first = await _ensure_index(request)
        second = await _ensure_index(request)
        return first, second

    return drive(main())


def test_ensure_index_reuses_cached_object(tmp_path):
    first, second = _ensure_twice_and_report(tmp_path)
    assert second is first


def test_ensure_index_stale_serve_and_signature_gate(tmp_path, monkeypatch):
    # Unified contract (merge of PR thumb-warmup-stale-serve's stale-serve
    # and bookq/main's filesystem signature gate): a TTL-expired cache is
    # returned IMMEDIATELY (the stale object), and the single-flight
    # background refresh gates on the filesystem signature — unchanged tree
    # → in-place timestamp refresh with zero scans; changed tree → full
    # rebuild + swap storing the walked signature. A null-signature cache
    # (cold build / warmup failure) always rebuilds once, self-healing the
    # stored signature for later expiries.
    import app.routes as routes

    calls = {"n": 0}
    real = routes._scan_and_rebuild

    async def counting(*a, **kw):
        calls["n"] += 1
        return await real(*a, **kw)

    monkeypatch.setattr(routes, "_scan_and_rebuild", counting)

    async def main():
        request = _stub_request(tmp_path)
        first = await _ensure_index(request)
        assert calls["n"] == 1
        # A cold-built cache starts with signature None (only lifespan
        # warmup pre-populates a walked one in production).
        cache = request.app.state.metadata_index_cache
        assert cache.get("signature") is None

        # 1) Expiry over a null-signature cache: stale object returned
        #    immediately, background refresh rebuilds once and stores a
        #    walked signature for later expiries.
        cache["timestamp"] = time.time() - INDEX_CACHE_TTL - 1
        stale = await _ensure_index(request)
        assert stale is first
        task = getattr(request.app.state, "index_refresh_task", None)
        assert task is not None
        await asyncio.wait_for(asyncio.ensure_future(task), 30)
        cache = request.app.state.metadata_index_cache
        assert calls["n"] == 2
        assert cache.get("signature") is not None
        assert cache["index"] is not first

        # 2) Expiry over an unchanged tree: stale object returned, gate
        #    refreshes the timestamp IN PLACE — same dict, zero scans.
        before_dict = cache
        before_ts = cache["timestamp"]
        before_tree, before_index = cache["tree"], cache["index"]
        before_gen = cache["generation"]
        cache["timestamp"] = time.time() - INDEX_CACHE_TTL - 1
        stale2 = await _ensure_index(request)
        assert stale2 is before_index
        task2 = getattr(request.app.state, "index_refresh_task", None)
        assert task2 is not None
        await asyncio.wait_for(asyncio.ensure_future(task2), 30)
        assert request.app.state.metadata_index_cache is before_dict, \
            "gate must refresh in place, never rebind"
        assert cache["tree"] is before_tree
        assert cache["index"] is before_index
        assert cache["timestamp"] > before_ts
        assert cache["generation"] == before_gen, \
            "an unchanged tree must not advance the generation"
        assert calls["n"] == 2, "unchanged tree must gate with zero rebuilds"

        # 3) Expiry over a changed tree: stale object returned, background
        #    rebuild swaps in a fresh cache dict with one more scan, and the
        #    generation ADVANCES so ``?since=`` pollers re-fetch the new tree.
        gen_before = cache["generation"]
        (tmp_path / "zz_new_folder_sig_gate").mkdir()
        (tmp_path / "zz_new_folder_sig_gate" / "001.png").write_bytes(b"x")
        cache["timestamp"] = time.time() - INDEX_CACHE_TTL - 1
        stale3 = await _ensure_index(request)
        assert stale3 is before_index
        task3 = getattr(request.app.state, "index_refresh_task", None)
        assert task3 is not None
        await asyncio.wait_for(asyncio.ensure_future(task3), 30)
        cache = request.app.state.metadata_index_cache
        assert cache is not before_dict, "rebuild must swap in a fresh dict"
        assert calls["n"] == 3
        assert cache.get("signature") is not None
        assert cache["generation"] == gen_before + 1, \
            "a proven content change must advance the generation"

    drive(main())


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

    first, second = drive(main())
    assert second is first


def test_index_lock_is_process_singleton():
    import app.routes as routes

    assert isinstance(_index_lock, asyncio.Lock)
    assert routes._index_lock is _index_lock
