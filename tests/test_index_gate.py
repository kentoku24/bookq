"""Signature gate + depth=1 decouple integration tests (T4), merged with
stale-serve (thumb-warmup-stale-serve).

Pins the request-path contracts at the HTTP level (plain TestClient, no
lifespan warmup — every test primes explicitly):

- gate: expired+unchanged serves the identical payload with a stable ETag
  and zero request-path rebuilds; the signature walk lives entirely in the
  single-flight background refresh, which this suite stubs out
  (_schedule_index_refresh → no-op) to keep HTTP tests deterministic — the
  background contract (in-place refresh / rebuild swap / race re-read) is
  pinned at unit level in tests/test_ensure_index.py and tests/test_stale_serve.py;
- decouple: ?depth=1 never populates or rebuilds metadata_index_cache — it
  serves from a fresh cache or falls back to a live-FS read (bypassing the
  body cache), so an expired cache is never served stale.

Teeth: (a) goes red under a forced synchronous rebuild on the expired
path (request-path rebuilds > 0); (d) goes red when the depth path serves
the expired cache as fresh (INDEX_CACHE_TTL inflated). Both restore to
green unpatched.

NOTE: TestClient portals run per request, so background tasks created inside
a handler are cancelled with the response — HTTP tests must never depend on,
or observe, background refresh effects (see the PR/test-plan notes in
tests/test_ensure_index.py).

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_index_gate.py -q
"""

import os
import shutil
import sys
from pathlib import Path

import pytest

os.environ["BOOKQ_TEST_MODE"] = "true"

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from tests.conftest import _make_png  # noqa: E402


@pytest.fixture
def client(downloads_tree: Path, monkeypatch):
    """TestClient serving the shared conftest fixture tree (no browser needed).

    Plain TestClient (no context manager) does NOT run lifespan warmup, so the
    cache starts cold (absent) — every test primes explicitly before asserting
    gate behavior.
    """
    monkeypatch.setenv("DOWNLOADS_DIR", str(downloads_tree))
    from app.main import create_app

    from fastapi.testclient import TestClient

    return TestClient(create_app())


def _expire(client):
    """Backdate the index-cache timestamp past the TTL (fixed offset, no clock)."""
    client.app.state.metadata_index_cache["timestamp"] -= 10_000


def _prime_then_gate(client):
    """Cold full GET, expire, full GET (stale-serve: served immediately with
    the refresh deferred to a background task this suite stubs out), expire.
    After the second expiry the cache is expired again, so the next full GET
    exercises the stale-serve path (never a request-path rebuild). Mirrors the
    prime-then-gate shape of tests/test_ensure_index.py."""
    r = client.get("/api/folders")
    assert r.status_code == 200
    _expire(client)
    r = client.get("/api/folders")
    assert r.status_code == 200
    _expire(client)


def _stub_background_refresh(monkeypatch):
    """No-op the background refresh scheduling.

    TestClient portals are per request, so a real background task would be
    cancelled mid-flight nondeterministically; HTTP-level tests here pin the
    request path only. The background contract is pinned at unit level
    (tests/test_ensure_index.py, tests/test_stale_serve.py).
    """
    import app.routes as routes

    monkeypatch.setattr(routes, "_schedule_index_refresh", lambda app: None)


def _count_rebuilds(monkeypatch):
    """Monkeypatch counter for _scan_and_rebuild (the locked rebuild path)."""
    import app.routes as routes

    calls = {"n": 0}
    real = routes._scan_and_rebuild

    async def counting(*a, **kw):
        calls["n"] += 1
        return await real(*a, **kw)

    monkeypatch.setattr(routes, "_scan_and_rebuild", counting)
    return calls


def _count_dumps(monkeypatch):
    """Monkeypatch counter for json.dumps (body serialization)."""
    import app.routes as routes

    calls = {"n": 0}
    real = routes.json.dumps

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(routes.json, "dumps", counting)
    return calls


def _names(payload):
    return [n.get("name") for n in payload]


def _mkdir_with_image(root: Path, name: str) -> Path:
    """New folder scan_tree will pick up (an image-less dir may be skipped)."""
    new = root / name
    new.mkdir()
    _make_png(new / "001.png")
    return new


def test_a_expired_unchanged_gates_with_zero_rebuilds(client, monkeypatch):
    """(a) Expired + unchanged tree → 0 request-path rebuilds, identical ETag,
    dumps delta ≤ 1.

    Stale-serve contract: the expired fetch is served IMMEDIATELY from cache
    with the refresh deferred to the background (stubbed out here), so the
    request path performs no signature walk and no rebuild. The dumps delta
    is ≤ 1 (the body-cache gen key may roll once if a concurrent refresh
    bumped the timestamp; the ETag — payload digest + fill generation — is
    unchanged because the tree did not change).
    """
    _stub_background_refresh(monkeypatch)
    rebuilds = _count_rebuilds(monkeypatch)
    dumps = _count_dumps(monkeypatch)
    _prime_then_gate(client)
    r1 = client.get("/api/folders")
    assert r1.status_code == 200
    etag1, base_rebuilds, base_dumps = r1.headers["etag"], rebuilds["n"], dumps["n"]
    _expire(client)
    r2 = client.get("/api/folders")
    assert r2.status_code == 200
    assert rebuilds["n"] - base_rebuilds == 0
    assert r2.headers["etag"] == etag1
    assert dumps["n"] - base_dumps <= 1


def test_b_expired_new_folder_serves_stale_without_rebuild(
    client, monkeypatch, downloads_tree
):
    """(b) Expired + new folder → the request path serves the STALE payload
    immediately (no rebuild, stable ETag); the change becomes visible after
    the background refresh swaps in a fresh index (pinned at unit level in
    tests/test_ensure_index.py — TestClient cannot observe background swaps).
    """
    _stub_background_refresh(monkeypatch)
    rebuilds = _count_rebuilds(monkeypatch)
    _prime_then_gate(client)
    etag1 = client.get("/api/folders").headers["etag"]
    base = rebuilds["n"]
    new = _mkdir_with_image(downloads_tree, "zz_gate_new_b")
    try:
        _expire(client)
        r = client.get("/api/folders")
        assert r.status_code == 200
        assert rebuilds["n"] - base == 0
        assert r.headers["etag"] == etag1  # stale serve, signature unchanged
        assert "zz_gate_new_b" not in _names(r.json())
    finally:
        shutil.rmtree(new, ignore_errors=True)


def test_c_depth1_never_rebuilds_warm_expired_or_absent(client, monkeypatch):
    """(c) ?depth=1 never populates or rebuilds metadata_index_cache — whether
    the cache is warm, expired, or absent entirely."""
    rebuilds = _count_rebuilds(monkeypatch)
    r = client.get("/api/folders")
    assert r.status_code == 200
    base = rebuilds["n"]
    # Warm cache: served from the tree, no rebuild.
    r = client.get("/api/folders", params={"path": "", "depth": 1})
    assert r.status_code == 200
    assert rebuilds["n"] == base
    # Expired cache: live-FS fallback, no rebuild (never served stale).
    _expire(client)
    r = client.get("/api/folders", params={"path": "", "depth": 1})
    assert r.status_code == 200
    assert rebuilds["n"] == base
    # Absent cache: live-FS fallback, no rebuild.
    client.app.state.metadata_index_cache = None
    r = client.get("/api/folders", params={"path": "", "depth": 1})
    assert r.status_code == 200
    assert rebuilds["n"] == base


def test_d_expired_new_folder_depth1_fallback_shows_immediately(
    client, monkeypatch, downloads_tree
):
    """(d) Expired + new folder → ?depth=1 falls back to the live FS and shows
    the folder immediately, with zero rebuilds. TEETH: serving the expired
    cache from the depth path (inflated TTL) misses the folder → red."""
    rebuilds = _count_rebuilds(monkeypatch)
    _prime_then_gate(client)
    base = rebuilds["n"]
    new = _mkdir_with_image(downloads_tree, "zz_gate_new_d")
    try:
        _expire(client)
        r = client.get("/api/folders", params={"path": "", "depth": 1})
        assert r.status_code == 200
        assert rebuilds["n"] - base == 0
        assert "zz_gate_new_d" in _names(r.json())
    finally:
        shutil.rmtree(new, ignore_errors=True)


def test_e_cold_sig_none_first_refresh_self_heals(client, monkeypatch):
    """(e) A cold-built cache carries signature None, so the first TTL
    expiry cannot gate — the background refresh rebuilds once and stores
    the walked signature (unit-level pin in tests/test_ensure_index.py;
    TestClient cannot observe background effects). The request path itself
    performs zero scans in either case."""
    _stub_background_refresh(monkeypatch)
    rebuilds = _count_rebuilds(monkeypatch)
    r = client.get("/api/folders")
    assert r.status_code == 200
    assert client.app.state.metadata_index_cache.get("signature") is None
    _expire(client)
    base = rebuilds["n"]
    r = client.get("/api/folders")
    assert r.status_code == 200
    assert rebuilds["n"] - base == 0
    assert client.app.state.metadata_index_cache.get("signature") is None


# (f) and (h) are re-derived at unit level where the background task can be
# awaited deterministically: (f) in-place timestamp refresh —
# tests/test_ensure_index.py step 2; (h) concurrent-replacement winner —
# tests/test_stale_serve.py::test_background_gate_race_serves_winner.


def test_g_metadata_toml_edit_serves_stale_without_rebuild(
    client, monkeypatch, downloads_tree
):
    """(g) In-place metadata.toml edit (no dir-mtime bump) moves the
    signature; the request path serves the STALE payload immediately (old
    metadata, zero rebuilds, stable ETag) — the rebuild is pinned at unit
    level in tests/test_ensure_index.py."""
    _stub_background_refresh(monkeypatch)
    rebuilds = _count_rebuilds(monkeypatch)
    _prime_then_gate(client)
    base = rebuilds["n"]
    toml = downloads_tree / "simple_manga" / "metadata.toml"
    assert not toml.exists()
    try:
        toml.write_text('[comic]\ntitle_jp = "GateProbeTitle"\n')
        _expire(client)
        r = client.get("/api/folders")
        assert r.status_code == 200
        assert rebuilds["n"] - base == 0
        node = next(n for n in r.json() if n.get("name") == "simple_manga")
        assert node.get("has_metadata") is False  # stale payload served
    finally:
        toml.unlink(missing_ok=True)


def test_i_depth1_fresh_then_expire_shows_new_folder_without_full_fetch(
    client, monkeypatch, downloads_tree
):
    """(i) Body-cache back door: ?depth=1 while fresh, mkdir, expire WITHOUT a
    full fetch, ?depth=1 again → the live-FS fallback (which deliberately
    bypasses folders_body_cache) shows the new folder with zero rebuilds."""
    rebuilds = _count_rebuilds(monkeypatch)
    r = client.get("/api/folders")
    assert r.status_code == 200
    d1 = client.get("/api/folders", params={"path": "", "depth": 1})
    assert d1.status_code == 200
    assert "zz_gate_new_i" not in _names(d1.json())
    new = _mkdir_with_image(downloads_tree, "zz_gate_new_i")
    try:
        _expire(client)
        d2 = client.get("/api/folders", params={"path": "", "depth": 1})
        assert d2.status_code == 200
        assert "zz_gate_new_i" in _names(d2.json())
        assert d2.headers["etag"] != d1.headers["etag"]  # fresh -g suffix vs fallback
        assert rebuilds["n"] == 1  # only the cold build; nothing rebuilt since
    finally:
        shutil.rmtree(new, ignore_errors=True)
