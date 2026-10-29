"""Cold-build 202 contract: a missing index cache must never park a request.

When ``metadata_index_cache`` is empty (warmup not landed / warmup failed),
``_ensure_tree_and_index`` spawns the shared ``app.state.index_build_task``
(_rebuild_into_cache) and awaits it only ``_INDEX_BUILD_AWAIT_SEC`` before
raising ``_IndexBuilding`` — handlers translate that into ``202 +
Retry-After`` so a 10s+ real-data scan can't stall a page load. Builds that
finish inside the budget keep the synchronous 200 contract, which is why
the small synthetic fixtures used by the rest of the suite see no change.
"""

import asyncio
import os
import time
from types import SimpleNamespace

import pytest

# app.auth reads BOOKQ_TEST_MODE once at import — set it before importing
# app modules so collection order cannot poison sibling HTTP tests.
os.environ["BOOKQ_TEST_MODE"] = "true"

import app.routes as routes
from app.config import Settings
from app.routes import _INDEX_BUILD_AWAIT_SEC, _IndexBuilding, _ensure_tree_and_index
from tests.helpers import drive


def _stub_request(tmp_path):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=Settings(downloads_dir=str(tmp_path)),
                metadata_index_cache=None,
                index_build_task=None,
            )
        )
    )


def test_cold_build_bounded_wait_then_lands(tmp_path, monkeypatch):
    """A build slower than the budget raises _IndexBuilding, then caches."""
    gate = asyncio.Event()
    calls = []

    async def slow_rebuild(downloads_path):
        calls.append(downloads_path)
        await gate.wait()
        return [], SimpleNamespace(), 0, 0, 0, 0, 0, 0

    monkeypatch.setattr(routes, "_scan_and_rebuild", slow_rebuild)

    async def main():
        request = _stub_request(tmp_path)
        t0 = time.monotonic()
        with pytest.raises(_IndexBuilding):
            await _ensure_tree_and_index(request)
        waited = time.monotonic() - t0
        # A second caller while the build is in flight shares the same task —
        # also _IndexBuilding, and crucially no second scan is spawned.
        with pytest.raises(_IndexBuilding):
            await _ensure_tree_and_index(request)
        gate.set()
        tree, index = await _ensure_tree_and_index(request)
        return waited, len(calls), tree, index

    waited, n_calls, tree, index = drive(main())
    assert waited < _INDEX_BUILD_AWAIT_SEC + 1.0
    assert n_calls == 1
    assert tree == []


def test_api_folders_returns_202_while_building(tmp_path, monkeypatch):
    """Handler-level contract: 202 + Retry-After, not a parked request."""
    gate = asyncio.Event()

    async def slow_rebuild(downloads_path):
        await gate.wait()
        return [], SimpleNamespace(), 0, 0, 0, 0, 0, 0

    monkeypatch.setattr(routes, "_scan_and_rebuild", slow_rebuild)

    async def main():
        request = _stub_request(tmp_path)
        return await routes.api_folders(request, SimpleNamespace(), "", None, None, {})

    out = drive(main())
    gate.set()
    assert getattr(out, "status_code", None) == 202
    assert out.headers.get("Retry-After") == "2"


def test_cold_build_inside_budget_keeps_sync_contract(tmp_path):
    """Fast cold build (small library) still returns the pair synchronously."""
    async def main():
        return await _ensure_tree_and_index(_stub_request(tmp_path))

    tree, index = drive(main())
    assert isinstance(tree, list)


def test_wait_true_blocks_until_build_lands(tmp_path, monkeypatch):
    """wait=True (the WS path, which cannot answer 202) keeps the old
    block-until-built semantics past the request budget."""
    gate = asyncio.Event()
    calls = []

    async def slow_rebuild(downloads_path):
        calls.append(downloads_path)
        await gate.wait()
        return [], SimpleNamespace(), 0, 0, 0, 0, 0, 0

    monkeypatch.setattr(routes, "_scan_and_rebuild", slow_rebuild)

    async def main():
        request = _stub_request(tmp_path)
        pending = asyncio.create_task(_ensure_tree_and_index(request, wait=True))
        # Past the HTTP budget the wait=True caller is still parked — it has
        # no 202 to fall back on, so it must ride the build to completion.
        await asyncio.sleep(_INDEX_BUILD_AWAIT_SEC + 0.2)
        assert not pending.done()
        gate.set()
        return await pending

    tree, index = drive(main())
    assert tree == []
    assert len(calls) == 1
