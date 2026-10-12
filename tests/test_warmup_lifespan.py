"""Startup warmup: cache hand-off to the request path, and a bounded lifespan (#221).

`warmup_metadata_index` must fill the exact cache structure `_ensure_index`
reads, so the first request after a container restart is a hit instead of a
second ~9s scan, and both callers must emit the same rebuild log line so the
`grep "index rebuilt"` count cannot drift between them.

`lifespan` must additionally never make the container *unhealthy*: lifespan
completes before /api/health is served, and the compose healthcheck allows
only start_period 2s + 15 x interval 2s ~= 32s. A wedged or slow warmup
therefore has to degrade to the pre-warmup lazy path, not fail startup.

Run:
    uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_warmup_lifespan.py -q
"""

import asyncio
import logging
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

os.environ["BOOKQ_TEST_MODE"] = "true"

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from app import main as app_main
from app import routes as app_routes
from app.config import Settings
from app.routes import _ensure_index, warmup_metadata_index
from tests.helpers import drive


def _state(downloads_path):
    return SimpleNamespace(
        settings=Settings(downloads_dir=str(downloads_path)),
        metadata_index_cache=None,
    )


def _stub_request(state):
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_warmup_fills_the_cache_the_first_request_reads(tmp_path):
    state = _state(tmp_path)

    async def main():
        await warmup_metadata_index(state)
        return await _ensure_index(_stub_request(state))

    assert drive(main()) is state.metadata_index_cache["index"]


def test_warmup_and_request_paths_emit_the_same_rebuild_log_line(tmp_path, caplog):
    state = _state(tmp_path)

    async def main():
        await warmup_metadata_index(state)
        state.metadata_index_cache = None  # force the request path to rebuild
        await _ensure_index(_stub_request(state))

    with caplog.at_level(logging.INFO, logger="app.routes"):
        drive(main())

    templates = [r.msg for r in caplog.records if r.msg.startswith("metadata index rebuilt")]
    assert len(templates) == 2, templates
    assert templates[0] == templates[1]


def test_slow_warmup_is_abandoned_but_startup_continues(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(app_main, "WARMUP_TIMEOUT_SECONDS", 0.05)

    async def slow(app_state):
        await asyncio.sleep(30)

    monkeypatch.setattr(app_main, "warmup_metadata_index", slow)
    state = _state(tmp_path)

    async def main():
        async with app_main.lifespan(SimpleNamespace(state=state)):
            return state.metadata_index_cache

    with caplog.at_level(logging.INFO, logger="app.main"):
        cached = drive(main())

    assert cached is None
    assert any("内部ウォームアップタイムアウト" in r.getMessage() for r in caplog.records)


def test_failing_warmup_does_not_block_startup(tmp_path, monkeypatch, caplog):
    async def boom(app_state):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(app_main, "warmup_metadata_index", boom)
    state = _state(tmp_path)

    async def main():
        async with app_main.lifespan(SimpleNamespace(state=state)):
            return state.metadata_index_cache

    with caplog.at_level(logging.INFO, logger="app.main"):
        cached = drive(main())

    assert cached is None
    assert any("内部ウォームアップ失敗" in r.getMessage() for r in caplog.records)


def test_startup_completion_is_logged_after_the_warmup_attempt(tmp_path, caplog):
    # 起動完了 must mean "about to serve", not "lifespan entered": operators tail
    # docker logs for it as a readiness signal, so it has to follow the warmup.
    state = _state(tmp_path)

    async def main():
        async with app_main.lifespan(SimpleNamespace(state=state)):
            return state.metadata_index_cache

    with caplog.at_level(logging.INFO, logger="app.main"):
        cached = drive(main())

    assert cached is not None  # warmup actually ran, so the ordering below is real
    messages = [r.getMessage() for r in caplog.records if r.name == "app.main"]
    warmup_done = next(i for i, m in enumerate(messages) if m.startswith("内部ウォームアップ完了"))
    ready = next(i for i, m in enumerate(messages) if m.startswith("起動完了"))
    assert ready > warmup_done


def test_first_request_scans_normally_after_an_abandoned_warmup(tmp_path, monkeypatch):
    # The timeout must degrade to the pre-warmup lazy path, not to a server that
    # can no longer build an index.
    monkeypatch.setattr(app_main, "WARMUP_TIMEOUT_SECONDS", 0.05)

    async def slow(app_state):
        await asyncio.sleep(30)

    monkeypatch.setattr(app_main, "warmup_metadata_index", slow)
    state = _state(tmp_path)

    async def main():
        async with app_main.lifespan(SimpleNamespace(state=state)):
            return await _ensure_index(_stub_request(state))

    assert drive(main()) is state.metadata_index_cache["index"]


def test_abandoned_threadpool_scan_never_populates_cache(tmp_path, monkeypatch, caplog):
    # Real threadpool-abandon path. Unlike the asyncio.sleep stubs above (clean
    # coroutine cancellation, no thread involved), here a worker thread keeps
    # running after wait_for cancels the coroutine. Its late result must be
    # discarded: the cache stays empty even after the worker finishes, and the
    # first request still builds the index through the normal lazy path.
    release = threading.Event()
    finished = []

    def stuck_scan(downloads_path):
        release.wait(timeout=30)
        finished.append(True)
        return []

    monkeypatch.setattr(app_main, "WARMUP_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(app_routes, "scan_tree", stuck_scan)
    state = _state(tmp_path)

    try:
        async def main():
            async with app_main.lifespan(SimpleNamespace(state=state)):
                return state.metadata_index_cache

        with caplog.at_level(logging.INFO, logger="app.main"):
            assert drive(main()) is None
        assert any("内部ウォームアップタイムアウト" in r.getMessage() for r in caplog.records)
    finally:
        # Never leak the worker: an early assert failure above must still let
        # the blocked scan_tree return (its 30s wait cap is only the backstop).
        release.set()

    # Let the abandoned worker run to completion, then prove its result was
    # dropped instead of cached late.
    deadline = time.monotonic() + 10
    while not finished and time.monotonic() < deadline:
        time.sleep(0.05)
    assert finished, "abandoned threadpool worker never finished"
    assert state.metadata_index_cache is None

    # The lazy path still works after the abandon (unstuck scan returns []).
    assert drive(_ensure_index(_stub_request(state))) is state.metadata_index_cache["index"]
