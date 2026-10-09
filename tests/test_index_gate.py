"""Signature gate + depth=1 decouple integration tests (T4).

Pins the gate and decouple contracts at the HTTP level (plain TestClient,
no lifespan warmup — every test primes explicitly):

- gate: an expired-but-unchanged tree refreshes the timestamp in place with
  zero rebuilds and a stable ETag; only a signature mismatch rebuilds;
- decouple: ?depth=1 never populates or rebuilds metadata_index_cache — it
  serves from a fresh cache or falls back to a live-FS read (bypassing the
  body cache), so an expired cache is never served stale.

Teeth: (a) goes red under forced always-rebuild (compute_signature mismatch);
(d) goes red when the depth path serves the expired cache as fresh
(INDEX_CACHE_TTL inflated). Both restore to green unpatched.

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_index_gate.py -q
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
    """Cold full GET, expire, full GET (first TTL miss rebuilds once and stores
    the pre-lock signature), expire. Afterwards the cache carries a real
    signature and is expired, so the next full GET exercises the gate — not
    the cold-build path (whose signature is None). Mirrors the
    prime-then-gate shape of tests/test_ensure_index.py:89-107."""
    r = client.get("/api/folders")
    assert r.status_code == 200
    _expire(client)
    r = client.get("/api/folders")
    assert r.status_code == 200
    _expire(client)


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
    """(a) Expired + unchanged tree → 0 rebuilds, identical ETag, dumps delta ≤ 1.

    The dumps delta is 1, never 0: the gate refreshes the timestamp in place,
    so the body cache (keyed on that timestamp gen) re-serializes exactly once.
    """
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


def test_b_expired_new_folder_rebuilds_visible_same_response(
    client, monkeypatch, downloads_tree
):
    """(b) Expired + new folder → exactly 1 rebuild, ETag differs, and the new
    folder is already present in the SAME response that rebuilt."""
    rebuilds = _count_rebuilds(monkeypatch)
    _prime_then_gate(client)
    etag1 = client.get("/api/folders").headers["etag"]
    base = rebuilds["n"]
    new = _mkdir_with_image(downloads_tree, "zz_gate_new_b")
    try:
        _expire(client)
        r = client.get("/api/folders")
        assert r.status_code == 200
        assert rebuilds["n"] - base == 1
        assert r.headers["etag"] != etag1
        assert "zz_gate_new_b" in _names(r.json())
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


def test_e_cold_sig_none_first_miss_rebuilds(client, monkeypatch):
    """(e) A cold-built cache carries signature None, so the FIRST TTL miss
    after a cold build rebuilds once (and stores the pre-lock sig for the
    next miss to gate on)."""
    rebuilds = _count_rebuilds(monkeypatch)
    r = client.get("/api/folders")
    assert r.status_code == 200
    assert client.app.state.metadata_index_cache.get("signature") is None
    _expire(client)
    base = rebuilds["n"]
    r = client.get("/api/folders")
    assert r.status_code == 200
    assert rebuilds["n"] - base == 1
    assert client.app.state.metadata_index_cache.get("signature") is not None


def test_f_gate_refreshes_cache_dict_in_place(client):
    """(f) The gate refreshes the timestamp WITHOUT rebinding the cache dict —
    same dict, same tree list, same index object afterwards."""
    _prime_then_gate(client)
    before = client.app.state.metadata_index_cache
    before_tree, before_index, before_ts = before["tree"], before["index"], before["timestamp"]
    r = client.get("/api/folders")
    assert r.status_code == 200
    after = client.app.state.metadata_index_cache
    assert after is before
    assert after["tree"] is before_tree
    assert after["index"] is before_index
    assert after["timestamp"] >= before_ts


def test_g_metadata_toml_edit_rebuilds_visible_same_response(
    client, monkeypatch, downloads_tree
):
    """(g) In-place metadata.toml edit (no dir-mtime bump) moves the signature
    via the toml mtime+size term → exactly 1 rebuild, and the new metadata is
    visible in the SAME response that rebuilt."""
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
        assert rebuilds["n"] - base == 1
        node = next(n for n in r.json() if n.get("name") == "simple_manga")
        assert node.get("has_metadata") is True
        assert node["metadata"]["title_jp"] == "GateProbeTitle"
    finally:
        toml.unlink(missing_ok=True)


def test_h_gate_race_reread_serves_winner(client, monkeypatch):
    """(h) A concurrent rebuild may replace the cache dict while the signature
    walks — the gate re-reads and serves the winner (cur), with zero rebuilds
    of its own."""
    import app.routes as routes

    rebuilds = _count_rebuilds(monkeypatch)
    _prime_then_gate(client)
    app = client.app
    old = app.state.metadata_index_cache
    real_sig = old["signature"]
    assert real_sig is not None
    winner_tree = [{"name": "zz_race_winner", "type": "folder", "children": []}]

    def fake_sig(path):
        winner = {
            "tree": winner_tree,
            "index": old["index"],
            "timestamp": time.time(),
            "generation": old.get("generation", 1),
            "signature": real_sig,
        }
        app.state.metadata_index_cache = winner
        return real_sig

    monkeypatch.setattr(routes, "compute_signature", fake_sig)
    base = rebuilds["n"]
    r = client.get("/api/folders")
    assert r.status_code == 200
    assert rebuilds["n"] == base
    assert "zz_race_winner" in _names(r.json())
    assert app.state.metadata_index_cache["tree"] is winner_tree


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
