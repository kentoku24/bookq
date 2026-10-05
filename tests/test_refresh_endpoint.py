"""Manual refresh endpoint — `POST /api/refresh` (plan: metadata-cache-refresh §2).

TDD red: endpoint does not exist yet, so the refresh tests fail with 404/405
until app/routes.py gains the endpoint.

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright \
      --with-requirements requirements-dev.txt pytest tests/test_refresh_endpoint.py -q
"""

import os
import shutil
import sys
import time
from pathlib import Path

import pytest

os.environ["BOOKQ_TEST_MODE"] = "true"

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))


@pytest.fixture
def client(downloads_tree: Path, monkeypatch):
    """TestClient serving the shared conftest fixture tree (no browser needed)."""
    monkeypatch.setenv("DOWNLOADS_DIR", str(downloads_tree))
    from app.main import create_app

    from fastapi.testclient import TestClient

    app_client = TestClient(create_app())
    yield app_client
    app_client.app.dependency_overrides.clear()


def _folder_names(client):
    r = client.get("/api/folders")
    assert r.status_code == 200
    return [n["name"] for n in r.json()]


def test_refresh_reflects_new_folder_immediately(client, downloads_tree):
    """New folder invisible under TTL becomes visible right after POST /api/refresh."""
    from tests.conftest import _make_png

    assert "zz_refresh_probe" not in _folder_names(client)
    new = downloads_tree / "zz_refresh_probe"
    new.mkdir()
    try:
        _make_png(new / "001.png")
        # TTL cache still hides the new folder before refresh.
        assert "zz_refresh_probe" not in _folder_names(client)
        r = client.post("/api/refresh")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert isinstance(body["folders"], int) and body["folders"] >= 0
        # Refresh rebuilds synchronously: visible on the very next read.
        assert "zz_refresh_probe" in _folder_names(client)
    finally:
        shutil.rmtree(new, ignore_errors=True)
        client.app.state.metadata_index_cache = None
        client.app.state.folders_body_cache = None


def test_refresh_requires_auth(client):
    """POST /api/refresh is wired through require_auth — denied callers get 401."""
    from fastapi import HTTPException, Request

    from app.auth import require_auth

    async def _deny(request: Request):
        raise HTTPException(status_code=401, detail="Not authenticated")

    client.app.dependency_overrides[require_auth] = _deny
    try:
        r = client.post("/api/refresh")
        assert r.status_code == 401
    finally:
        client.app.dependency_overrides.clear()


def test_refresh_does_not_conflict_with_ttl_cache(client):
    """Refresh stamps a fresh timestamp; later reads hit cache, ETag stays stable."""
    from app.routes import INDEX_CACHE_TTL

    r = client.post("/api/refresh")
    assert r.status_code == 200
    stamped = client.app.state.metadata_index_cache["timestamp"]
    assert time.time() - stamped < INDEX_CACHE_TTL

    etag1 = client.get("/api/folders").headers["etag"]
    # Force TTL expiry — reads must still work (auto-rebuild path intact).
    client.app.state.metadata_index_cache["timestamp"] -= INDEX_CACHE_TTL + 10_000
    try:
        r2 = client.get("/api/folders")
        assert r2.status_code == 200
        # Tree unchanged → ETag stable across the TTL rebuild (no conflict).
        assert r2.headers["etag"] == etag1
        # A second refresh on a stale cache still succeeds and re-freshens.
        r3 = client.post("/api/refresh")
        assert r3.status_code == 200
        assert r3.json()["status"] == "ok"
        assert time.time() - client.app.state.metadata_index_cache["timestamp"] < INDEX_CACHE_TTL
    finally:
        client.app.state.metadata_index_cache = None
        client.app.state.folders_body_cache = None
