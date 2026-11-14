"""v0 compat-surface tests — `/api/folders`, `/api/reading-position`, and the
v0↔v1 store interplay that dies with `app/` in Phase 3.

Under `BOOKQ_TEST_SERVER=rs` (no-upstream) every v0 URL answers 501 — this file
is quarantined there by the collection hook and only runs in the Python lane.
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


def _v1_positions(server, folder=None):
    q = f"?folder={folder}" if folder is not None else ""
    _, body, _ = _req(server, "GET", "/api/v1/reading-positions" + q)
    return (body or {}).get("positions")


# ── ETag namespacing ─────────────────────────────────────────────────

def test_tree_etag_namespaced_from_v0(server):
    """v0 and v1 must not share ETags/bodies — the cache key is versioned."""
    _, _, v0h = _get(server, "/api/folders")
    _, _, v1h = _get(server, "/api/v1/tree")
    assert v0h["etag"] != v1h["etag"]
    # Cross-applying the v1 ETag to v0 must not produce a 304.
    status, _, _ = _get(server, "/api/folders", headers={"If-None-Match": v1h["etag"]})
    assert status == 200

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

def test_pair_report_gate_blocks_non_test_mode(monkeypatch):
    """Outside test mode the endpoint must not exist (404)."""
    from fastapi import HTTPException

    from app import auth
    from app.api_v1 import _pair_report_gate

    monkeypatch.setattr(auth, "_TEST_MODE", False)
    with pytest.raises(HTTPException) as ei:
        _pair_report_gate()
    assert ei.value.status_code == 404
