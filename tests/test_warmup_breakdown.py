"""Warmup breakdown log fields: scan_ms/rebuild_ms + import_ms (failing-first, T1).

`_scan_and_rebuild` (app/routes.py:78-104) already emits
`metadata index rebuilt ... scan_ms=... rebuild_ms=...`; the lifespan
`内部ウォームアップ完了` line (app/main.py:65-114) does NOT yet carry an
`import_ms` field, so the second test below is RED by design (T2 turns it GREEN).

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_warmup_breakdown.py -q
"""

import asyncio
import logging
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

os.environ["BOOKQ_TEST_MODE"] = "true"

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from app import main as app_main
from app.config import Settings
from app.routes import warmup_metadata_index


def _drive(coro):
    """Run *coro* on a fresh event loop in a helper thread.

    Same constraint as test_ensure_index: Playwright's sync API leaves a
    running loop in the main thread, so asyncio.run() there fails. The
    module-level _index_lock is never contended (strictly sequential awaits),
    so it never binds to these short-lived loops.
    """
    outcome = {}

    def target():
        outcome["value"] = asyncio.run(coro)

    worker = threading.Thread(target=target)
    worker.start()
    worker.join()
    return outcome["value"]


def _state(downloads_path):
    return SimpleNamespace(
        settings=Settings(downloads_dir=str(downloads_path)),
        metadata_index_cache=None,
    )


def test_rebuild_log_contains_scan_and_rebuild_ms(tmp_path, caplog):
    state = _state(tmp_path)

    async def main():
        await warmup_metadata_index(state)

    with caplog.at_level(logging.INFO, logger="app.routes"):
        _drive(main())

    records = [r for r in caplog.records if r.msg.startswith("metadata index rebuilt")]
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    message = records[0].getMessage()
    assert "scan_ms=" in message, message
    assert "rebuild_ms=" in message, message


def test_lifespan_completion_log_contains_import_ms(tmp_path, caplog):
    state = _state(tmp_path)

    async def main():
        async with app_main.lifespan(SimpleNamespace(state=state)):
            return state.metadata_index_cache

    with caplog.at_level(logging.INFO, logger="app.main"):
        _drive(main())

    records = [r for r in caplog.records if r.msg.startswith("内部ウォームアップ完了")]
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    message = records[0].getMessage()
    assert "import_ms=" in message, message
