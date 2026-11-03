"""BOOKQ_DISABLE_INDEX (#320): py must not build or hold tree+index in rs-native mode.

Once every tree/index consumer is served natively upstream (BOOKQ_RS_NATIVE
covering tree/search/facets/visibility/refresh/reader-sync), the Python viewer
drops its largest heap block by never constructing metadata_index_cache.
The flag fails fast (503 _IndexDisabled) on any index-needing path instead of
lazily rebuilding — a silent lazy build would regrow the heap on
misconfiguration. Index-free paths (shallow /api/folders live-FS fallback,
/api/images, /image, /thumb, health) keep working.
"""

import os
from types import SimpleNamespace

import pytest

# app.auth reads BOOKQ_TEST_MODE once at import — set it before importing
# app modules so collection order cannot poison sibling HTTP tests.
os.environ["BOOKQ_TEST_MODE"] = "true"

import app.routes as routes
from app.config import Settings
from app.routes import (
    _IndexDisabled,
    _ensure_tree_and_index,
    _index_disabled,
    warmup_metadata_index,
)
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


def test_index_disabled_env_parsing(monkeypatch):
    for value in (None, "", "0", "false", "no", "off"):
        if value is None:
            monkeypatch.delenv("BOOKQ_DISABLE_INDEX", raising=False)
        else:
            monkeypatch.setenv("BOOKQ_DISABLE_INDEX", value)
        assert _index_disabled() is False
    for value in ("1", "true", "yes", "TRUE", " yes "):
        monkeypatch.setenv("BOOKQ_DISABLE_INDEX", value)
        assert _index_disabled() is True


def test_ensure_raises_503_under_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOKQ_DISABLE_INDEX", "1")
    request = _stub_request(tmp_path)

    async def main():
        with pytest.raises(_IndexDisabled) as excinfo:
            await _ensure_tree_and_index(request)
        return excinfo.value

    exc = drive(main())
    assert exc.status_code == 503
    # Even the wait=True path (WS/callers that cannot take 202) fails fast —
    # it must not park on a build that will never run.
    async def wait_main():
        with pytest.raises(_IndexDisabled):
            await _ensure_tree_and_index(request, wait=True)

    drive(wait_main())


def test_warmup_is_noop_under_flag(tmp_path, monkeypatch):
    """No tree is built at all — not even once (a one-shot build would still
    leave it in the cache for its lifetime)."""
    monkeypatch.setenv("BOOKQ_DISABLE_INDEX", "1")
    state = SimpleNamespace(
        settings=Settings(downloads_dir=str(tmp_path)),
        metadata_index_cache=None,
    )
    folders, images, elapsed = drive(warmup_metadata_index(state))
    assert (folders, images, elapsed) == (0, 0, 0.0)
    assert state.metadata_index_cache is None


def test_refresh_fails_fast_under_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOKQ_DISABLE_INDEX", "1")
    request = _stub_request(tmp_path)

    async def main():
        with pytest.raises(_IndexDisabled) as excinfo:
            await routes.refresh_index(request, {})
        return excinfo.value

    assert drive(main()).status_code == 503


def test_disabled_flag_off_builds_normally(tmp_path, monkeypatch):
    """Flag off: the normal build path is unchanged (synthetic tree)."""
    monkeypatch.delenv("BOOKQ_DISABLE_INDEX", raising=False)
    request = _stub_request(tmp_path)
    tree, _index = drive(_ensure_tree_and_index(request))
    assert isinstance(tree, list)
