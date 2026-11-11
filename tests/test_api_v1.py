"""API v1 endpoint contract tests (api/openapi.v1.yaml shapes).

Covers the normalized envelopes ({kind, nodes, generation} for tree,
{folder, images} for images), per-folder reading-position LWW semantics,
and v0↔v1 store interplay (v0 sees the global newest; its single-slot
stale/tombstone contract is preserved).
"""

import json
import urllib.error
import urllib.request

import pytest


def _req(server: str, method: str, path: str, body=None, headers=None):
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(server + path, data=data, headers=hdrs, method=method)

    def _headers(resp):
        return {k.lower(): v for k, v in resp.headers.items()}

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None), _headers(resp)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null"), _headers(e)
        except Exception:
            return e.code, None, _headers(e)


def _get(server, path, **kw):
    status, body, headers = _req(server, "GET", path, **kw)
    if status == 202:  # cold index build: retry once after Retry-After
        import time

        time.sleep(2.5)
        status, body, headers = _req(server, "GET", path, **kw)
    return status, body, headers


# ── meta ─────────────────────────────────────────────────────────────


def test_health(server):
    status, body, _ = _get(server, "/api/v1/health")
    assert status == 200
    assert body == {"status": "ok"}


def test_version(server):
    status, body, headers = _get(server, "/api/v1/version")
    assert status == 200
    assert set(body) == {"version", "sha", "build_date"}
    assert "no-store" in headers.get("cache-control", "")


# ── tree envelope ────────────────────────────────────────────────────


def test_tree_envelope(server):
    status, body, headers = _get(server, "/api/v1/tree")
    assert status == 200
    assert body["kind"] == "tree"
    assert isinstance(body["generation"], int) and body["generation"] >= 1
    names = [n["name"] for n in body["nodes"]]
    assert "long" in names and "series" in names
    assert headers.get("etag")
    assert "private" in headers.get("cache-control", "")


def test_tree_children_kind(server):
    status, body, _ = _get(server, "/api/v1/tree?depth=1")
    assert status == 200
    assert body["kind"] == "children"
    for node in body["nodes"]:
        assert node["type"] == "folder"
        assert node["children"] == []
        assert "metadata" not in node and "subtree_facets" not in node


def test_tree_since_304_and_stale_full(server):
    _, body, _ = _get(server, "/api/v1/tree")
    gen = body["generation"]
    status, _, _ = _get(server, f"/api/v1/tree?since={gen}")
    assert status == 304
    status, body2, _ = _get(server, f"/api/v1/tree?since={gen + 1000}")
    assert status == 200 and body2["kind"] == "tree"


def test_tree_etag_revalidate(server):
    _, _, headers = _get(server, "/api/v1/tree")
    etag = headers["etag"]
    status, _, _ = _get(server, "/api/v1/tree", headers={"If-None-Match": etag})
    assert status == 304


def test_tree_etag_namespaced_from_v0(server):
    """v0 and v1 must not share ETags/bodies — the cache key is versioned."""
    _, _, v0h = _get(server, "/api/folders")
    _, _, v1h = _get(server, "/api/v1/tree")
    assert v0h["etag"] != v1h["etag"]
    # Cross-applying the v1 ETag to v0 must not produce a 304.
    status, _, _ = _get(server, "/api/folders", headers={"If-None-Match": v1h["etag"]})
    assert status == 200


def test_tree_depth_validation(server):
    status, _, _ = _get(server, "/api/v1/tree?depth=0")
    assert status == 400
    status, _, _ = _get(server, "/api/v1/tree?path=nope&depth=1")
    assert status == 404
    status, _, _ = _get(server, "/api/v1/tree?path=../../etc&depth=1")
    assert status == 403


# ── images / search / facets ─────────────────────────────────────────


def test_images_envelope(server):
    status, body, _ = _get(server, "/api/v1/images?folder=long")
    assert status == 200
    assert body["folder"] == "long"
    assert len(body["images"]) == 30
    assert body["images"][0] == "p001.png" and body["images"][-1] == "p030.png"


def test_images_404(server):
    status, _, _ = _get(server, "/api/v1/images?folder=nope")
    assert status == 404


def test_search_envelope(server):
    status, body, _ = _get(server, "/api/v1/search")
    assert status == 200
    assert set(body) == {"query", "total", "results"}
    assert isinstance(body["results"], list)


def test_facets_envelope(server):
    status, body, _ = _get(server, "/api/v1/facets")
    assert status == 200
    assert set(body) == {"artists", "tags"}


# ── reading positions (per-folder LWW) ───────────────────────────────


def _v1_positions(server, folder=None):
    q = f"?folder={folder}" if folder is not None else ""
    _, body, _ = _req(server, "GET", "/api/v1/reading-positions" + q)
    return (body or {}).get("positions")


def test_positions_per_folder_lww(server):
    _, body, _ = _req(server, "PUT", "/api/v1/reading-positions?folder=long",
                      {"page": 4, "total": 30, "ts": 100})
    assert body["status"] == "ok"
    _, body, _ = _req(server, "PUT", "/api/v1/reading-positions?folder=series/vol1",
                      {"page": 0, "total": 2, "ts": 90})
    assert body["status"] == "ok"
    # Older ts for the same folder → stale; other folder untouched.
    _, body, _ = _req(server, "PUT", "/api/v1/reading-positions?folder=long",
                      {"page": 9, "total": 30, "ts": 70})
    assert body["status"] == "stale"
    positions = _v1_positions(server)
    by_folder = {p["folder"]: p for p in positions}
    assert by_folder["long"]["page"] == 4 and by_folder["long"]["ts"] == 100
    assert by_folder["series/vol1"]["ts"] == 90


def test_positions_newest_first_order(server):
    _req(server, "PUT", "/api/v1/reading-positions?folder=a", {"page": 0, "total": 1, "ts": 10})
    _req(server, "PUT", "/api/v1/reading-positions?folder=b", {"page": 0, "total": 1, "ts": 30})
    _req(server, "PUT", "/api/v1/reading-positions?folder=c", {"page": 0, "total": 1, "ts": 20})
    positions = _v1_positions(server)
    assert [p["folder"] for p in positions] == ["b", "c", "a"]


def test_positions_folder_scoped_get(server):
    _req(server, "PUT", "/api/v1/reading-positions?folder=long", {"page": 1, "total": 30, "ts": 100})
    _req(server, "PUT", "/api/v1/reading-positions?folder=long/x", {"page": 0, "total": 1, "ts": 50})
    positions = _v1_positions(server, folder="long")
    assert [p["folder"] for p in positions] == ["long"]


def test_positions_folder_delete_tombstone(server):
    _req(server, "PUT", "/api/v1/reading-positions?folder=long", {"page": 1, "total": 30, "ts": 100})
    status, _, _ = _req(server, "DELETE", "/api/v1/reading-positions?folder=long")
    assert status == 200
    assert _v1_positions(server, folder="long") == []
    # Resurrect at the wiped ts → stale; strictly newer → ok.
    _, body, _ = _req(server, "PUT", "/api/v1/reading-positions?folder=long",
                      {"page": 2, "total": 30, "ts": 100})
    assert body["status"] == "stale"
    _, body, _ = _req(server, "PUT", "/api/v1/reading-positions?folder=long",
                      {"page": 2, "total": 30, "ts": 101})
    assert body["status"] == "ok"


def test_positions_delete_all_tombstone(server):
    _req(server, "PUT", "/api/v1/reading-positions?folder=long", {"page": 1, "total": 30, "ts": 200})
    status, _, _ = _req(server, "DELETE", "/api/v1/reading-positions")
    assert status == 200
    assert _v1_positions(server) == []
    _, body, _ = _req(server, "PUT", "/api/v1/reading-positions?folder=other",
                      {"page": 0, "total": 9, "ts": 200})
    assert body["status"] == "stale"
    _, body, _ = _req(server, "PUT", "/api/v1/reading-positions?folder=other",
                      {"page": 0, "total": 9, "ts": 201})
    assert body["status"] == "ok"


def test_positions_put_validation(server):
    status, _, _ = _req(server, "PUT", "/api/v1/reading-positions?folder=long",
                        {"page": 0, "total": 5})
    assert status == 400  # ts required
    status, _, _ = _req(server, "PUT", "/api/v1/reading-positions?folder=long",
                        {"page": 0, "total": 5, "ts": 1.5})
    assert status == 400  # ts must be int
    status, _, _ = _req(server, "PUT", "/api/v1/reading-positions?folder=/abs",
                        {"page": 0, "total": 5, "ts": 1})
    assert status == 400  # leading slash rejected
    # ints beyond SQLite i64 must 400 (Rust as_int parity), not 503
    for field, bad in (("page", 1 << 63), ("total", 1 << 63), ("ts", 1 << 63)):
        body = {"page": 0, "total": 5, "ts": 1, field: bad}
        status, _, _ = _req(server, "PUT",
                            "/api/v1/reading-positions?folder=long", body)
        assert status == 400, field


# ── v0 ↔ v1 store interplay ──────────────────────────────────────────


def test_v0_get_reads_global_newest(server):
    _req(server, "PUT", "/api/v1/reading-positions?folder=long", {"page": 1, "total": 30, "ts": 100})
    _req(server, "PUT", "/api/v1/reading-positions?folder=series/vol1", {"page": 0, "total": 2, "ts": 300})
    _, body, _ = _req(server, "GET", "/api/reading-position")
    assert body["position"]["folder"] == "series/vol1"
    assert body["position"]["ts"] == 300


def test_v0_put_global_newest_wins(server):
    _req(server, "PUT", "/api/v1/reading-positions?folder=long", {"page": 1, "total": 30, "ts": 100})
    # ts below the global live newest is stale, and must NOT write the folder slot.
    _, body, _ = _req(server, "PUT", "/api/reading-position",
                      {"folder": "other", "page": 0, "total": 5, "ts": 50})
    assert body["status"] == "stale"
    assert _v1_positions(server, folder="other") == []
    _, body, _ = _req(server, "PUT", "/api/reading-position",
                      {"folder": "other", "page": 0, "total": 5, "ts": 200})
    assert body["status"] == "ok"
    assert _v1_positions(server, folder="other")[0]["ts"] == 200


def test_v0_delete_wipes_all_folders(server):
    _req(server, "PUT", "/api/v1/reading-positions?folder=long", {"page": 1, "total": 30, "ts": 200})
    _req(server, "PUT", "/api/v1/reading-positions?folder=series/vol1", {"page": 0, "total": 2, "ts": 100})
    status, _, _ = _req(server, "DELETE", "/api/reading-position")
    assert status == 200
    assert _v1_positions(server) == []
    _, body, _ = _req(server, "GET", "/api/reading-position")
    assert body["position"] is None


def test_v1_folder_tombstone_blocks_v0_put(server):
    """A v1 folder delete must beat an older v0 write for that folder."""
    _req(server, "PUT", "/api/v1/reading-positions?folder=long", {"page": 1, "total": 30, "ts": 100})
    _req(server, "DELETE", "/api/v1/reading-positions?folder=long")
    _, body, _ = _req(server, "PUT", "/api/reading-position",
                      {"folder": "long", "page": 2, "total": 30, "ts": 90})
    assert body["status"] == "stale"


# ── folder-visibility ────────────────────────────────────────────────


def test_folder_visibility_v1_shape(server):
    status, body, _ = _get(server, "/api/v1/folder-visibility")
    assert status == 200
    assert set(body) == {"owner", "revision", "hiddenRoots"}
    assert isinstance(body["hiddenRoots"], list)


def test_folder_visibility_v1_put_roundtrip(server):
    _, snap, _ = _get(server, "/api/v1/folder-visibility")
    owner = snap["owner"]
    status, body, _ = _req(
        server, "PUT", "/api/v1/folder-visibility",
        {"root": "long", "hidden": True},
        headers={"x-bookq-visibility-owner": owner},
    )
    assert status == 200
    assert "long" in body["hiddenRoots"]
    # unhide to leave the shared state clean
    status, body, _ = _req(
        server, "PUT", "/api/v1/folder-visibility",
        {"root": "long", "hidden": False},
        headers={"x-bookq-visibility-owner": owner},
    )
    assert status == 200
    assert "long" not in body["hiddenRoots"]


# ── pair-report (test-only #142 bridge) ──────────────────────────────


def test_pair_report_post_get_roundtrip(server):
    payload = {"id": "pytest-bridge", "pc": {"phase": "reading"}, "pv": {"left": "1"}}
    status, body, _ = _req(server, "POST", "/api/v1/pair-report", payload)
    assert status == 200
    assert body == {"ok": True}

    status, body, _ = _get(server, "/api/v1/pair-report?id=pytest-bridge")
    assert status == 200
    state = body["state"]
    assert state["pc"] == {"phase": "reading"}
    assert state["pv"] == {"left": "1"}
    assert "id" not in state            # id is the key, not part of the state
    assert state["received_at"] > 0     # stamped by the endpoint


def test_pair_report_unknown_id_returns_null_state(server):
    status, body, _ = _get(server, "/api/v1/pair-report?id=never-posted")
    assert status == 200
    assert body == {"state": None}


def test_pair_report_post_requires_id(server):
    status, _, _ = _req(server, "POST", "/api/v1/pair-report", {"pc": {}})
    assert status == 400


def test_pair_report_gate_blocks_non_test_mode(monkeypatch):
    """Outside test mode the endpoint must not exist (404)."""
    from fastapi import HTTPException

    from app import auth
    from app.api_v1 import _pair_report_gate

    monkeypatch.setattr(auth, "_TEST_MODE", False)
    with pytest.raises(HTTPException) as ei:
        _pair_report_gate()
    assert ei.value.status_code == 404
