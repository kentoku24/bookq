"""Shallow folders listing — `GET /api/folders?path=<rel>&depth=1` (Todo 2).

`?path=&depth=1` returns only top-level folders (children == [], no
metadata/subtree_facets keys); omitting `depth` returns the legacy full tree.

Run:
    uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_folders_shallow.py -q
"""

import os
import sys
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

    return TestClient(create_app())


def _full_tree(client):
    r = client.get("/api/folders")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list) and data
    return data


def test_shallow_root_lists_only_top_level_folders(client):
    full = _full_tree(client)
    r = client.get("/api/folders", params={"path": "", "depth": 1})
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list) and data
    expected = [n["name"] for n in full if n.get("type") == "folder"]
    assert [n["name"] for n in data] == expected
    assert len(data) == len(full)  # fixture root holds folders only
    for node in data:
        assert node["type"] == "folder"
        # folderHasOnlyImages (offline-core.js) and getBrowseChildren (app.js)
        # depend on children being a list, never None.
        assert isinstance(node["children"], list)
        assert node["children"] == []


def test_shallow_carries_no_metadata_or_facets_keys(client):
    # Frontend must only OfflineTree.putTree() full-tree snapshots. The shallow
    # shape (children == [] everywhere, no metadata/subtree_facets keys, no
    # nested grandchildren) is deliberately NOT a full tree, so a shallow
    # payload must never pollute the offline snapshot.
    r = client.get("/api/folders", params={"path": "", "depth": 1})
    assert r.status_code == 200
    for node in r.json():
        assert "metadata" not in node
        assert "subtree_facets" not in node


def test_omitted_depth_returns_legacy_full_tree(client):
    full = _full_tree(client)
    by_name = {n["name"]: n for n in full}
    # Nested grandchildren survive on the legacy path (byte-compatible default).
    assert "group" in by_name
    children = by_name["group"]["children"]
    assert isinstance(children, list) and len(children) > 0
    assert any(c.get("type") == "folder" for c in children)
    # Metadata attach still runs on the legacy path (same key set as before).
    assert any("has_metadata" in n for n in full)


def test_nested_path_shallow_and_flatten_name(client):
    full = _full_tree(client)
    group = next(n for n in full if n["name"] == "group")
    r = client.get("/api/folders", params={"path": "group", "depth": 1})
    assert r.status_code == 200
    data = r.json()
    expected = [c["name"] for c in group["children"] if c.get("type") == "folder"]
    assert [n["name"] for n in data] == expected
    for node in data:
        assert isinstance(node["children"], list) and node["children"] == []
    # Flattened single-child wrapper resolves WITHOUT splitting "/" in the
    # name: "wrap/inner/vol1" is one folder node under "wrap" (segment-wise
    # descent into "inner" would miss it).
    r = client.get("/api/folders", params={"path": "wrap/inner/vol1", "depth": 1})
    assert r.status_code == 200
    assert r.json() == []  # image-only children are filtered out


def test_unknown_path_returns_404(client):
    assert client.get("/api/folders", params={"path": "no_such_folder", "depth": 1}).status_code == 404
    assert client.get("/api/folders", params={"path": "group/nope", "depth": 1}).status_code == 404


def test_outside_downloads_returns_403(client):
    assert client.get("/api/folders", params={"path": "..", "depth": 1}).status_code == 403
    assert client.get("/api/folders", params={"path": "/etc", "depth": 1}).status_code == 403


def test_depth_below_one_returns_400(client):
    assert client.get("/api/folders", params={"path": "", "depth": 0}).status_code == 400
    assert client.get("/api/folders", params={"path": "", "depth": -2}).status_code == 400


def test_non_literal_int_params_return_422(client):
    """`type: integer` in the spec means the wire value must be an integer
    literal — pydantic's lax coercion used to accept `since=0.0`/`depth=1.0`,
    which schemathesis flags as ST201 (#326)."""
    for params in (
        {"since": "0.0"}, {"since": "1.5"}, {"since": "1e2"}, {"since": "abc"},
        {"since": "１２"},  # Unicode digits: \d matches and int() parses them
        {"depth": "1.0"}, {"depth": "x"},
    ):
        assert client.get("/api/folders", params=params).status_code == 422, params
        assert client.get("/api/v1/tree", params=params).status_code == 422, params
    # Integer literals stay accepted — a non-matching `since` still serves
    # the tree, and negatives are schema-valid integers.
    assert client.get("/api/folders", params={"since": "0"}).status_code == 200
    assert client.get("/api/folders", params={"since": "-3"}).status_code == 200
    assert client.get("/api/folders", params={"depth": "1"}).status_code == 200
    assert client.get("/api/v1/tree", params={"since": "0"}).status_code == 200
    assert client.get("/api/v1/tree", params={"depth": "1"}).status_code == 200


# --- Todo 6: HTTP cache (ETag + Cache-Control: private, max-age=60) ---


def test_full_tree_carries_etag_and_private_cache_control(client):
    r = client.get("/api/folders")
    assert r.status_code == 200
    etag = r.headers.get("etag")
    assert etag, "full-tree response must carry an ETag"
    assert r.headers.get("cache-control") == "private, max-age=60"


def test_full_tree_if_none_match_returns_304_with_empty_body(client):
    r1 = client.get("/api/folders")
    assert r1.status_code == 200
    etag = r1.headers["etag"]
    body = r1.json()
    r2 = client.get("/api/folders", headers={"If-None-Match": etag})
    assert r2.status_code == 304
    assert r2.content == b""
    assert r2.headers.get("etag") == etag
    # Re-fetch without precondition: content identical to the cached one.
    r3 = client.get("/api/folders")
    assert r3.status_code == 200
    assert r3.json() == body
    assert r3.headers.get("etag") == etag  # stable within INDEX_CACHE_TTL


def test_shallow_carries_etag_and_304(client):
    r1 = client.get("/api/folders", params={"path": "", "depth": 1})
    assert r1.status_code == 200
    etag = r1.headers.get("etag")
    assert etag, "shallow response must carry an ETag"
    assert r1.headers.get("cache-control") == "private, max-age=60"
    body = r1.json()
    r2 = client.get(
        "/api/folders",
        params={"path": "", "depth": 1},
        headers={"If-None-Match": etag},
    )
    assert r2.status_code == 304
    assert r2.content == b""
    assert r2.headers.get("etag") == etag
    r3 = client.get("/api/folders", params={"path": "", "depth": 1})
    assert r3.json() == body
    assert r3.headers.get("etag") == etag


def test_etag_differs_by_path_and_depth(client):
    full_etag = client.get("/api/folders").headers["etag"]
    shallow_root_etag = client.get(
        "/api/folders", params={"path": "", "depth": 1}
    ).headers["etag"]
    shallow_group_etag = client.get(
        "/api/folders", params={"path": "group", "depth": 1}
    ).headers["etag"]
    assert full_etag != shallow_root_etag
    assert shallow_root_etag != shallow_group_etag
    assert full_etag != shallow_group_etag


# --- Task 12 (#196): ETag must not rotate with the index-cache TTL; body cached per index generation ---


def test_etag_survives_index_cache_refresh(client):
    r1 = client.get("/api/folders")
    etag1 = r1.headers["etag"]
    # index キャッシュを期限切れにして再構築させる（ツリーは不変）
    app = client.app
    app.state.metadata_index_cache["timestamp"] -= 10_000
    r2 = client.get("/api/folders")
    assert r2.status_code == 200
    assert r2.headers["etag"] == etag1
    r3 = client.get("/api/folders", headers={"If-None-Match": etag1})
    assert r3.status_code == 304


def test_etag_changes_when_tree_changes(client, downloads_tree):
    etag1 = client.get("/api/folders").headers["etag"]
    new = downloads_tree / "zz_new_folder_for_etag"
    new.mkdir()
    try:
        # 1 枚以上画像が無いと scan_tree が拾わない場合は conftest の _make_png を使う
        from tests.conftest import _make_png
        _make_png(new / "001.png")
        client.app.state.metadata_index_cache["timestamp"] -= 10_000
        # Stale-serve (plan thumb-warmup-stale-serve Todo 1): TTL 期限切れ直後の
        # read は stale tree を即返却する (ETag 不変・blocking なし)。背景 rebuild
        # は TestClient の request スコープでは応答と共に cancel されるため、ここでは
        # 同期 rebuild である手動 /api/refresh で新フォルダの ETag 変化を pin する。
        # 背景 swap 自体の契約は tests/test_stale_serve.py の unit テストで pin 済み。
        assert client.get("/api/folders").headers["etag"] == etag1
        r = client.post("/api/refresh")
        assert r.status_code == 200
        etag2 = client.get("/api/folders").headers["etag"]
        assert etag2 != etag1
    finally:
        import shutil
        shutil.rmtree(new)
        client.app.state.metadata_index_cache = None  # 後続テストに漏らさない
        client.app.state.folders_body_cache = None  # 同上（ボディ/ETagキャッシュも漏らさない）


def test_body_serialized_once_per_index_generation(client, monkeypatch):
    import app.routes as routes
    calls = {"n": 0}
    real = routes.json.dumps
    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)
    monkeypatch.setattr(routes.json, "dumps", counting)
    client.get("/api/folders")
    first = calls["n"]
    assert first >= 1
    client.get("/api/folders")
    client.get("/api/folders", headers={"If-None-Match": client.get("/api/folders").headers["etag"]})
    assert calls["n"] == first, "TTL 内の再リクエストで json.dumps が再実行されている"


def test_full_tree_body_is_compact_json_and_matches_payload(client):
    r = client.get("/api/folders")
    raw = r.content
    assert raw.startswith(b'[{"')  # separators=(",", ":") — no leading space/newline
    assert b": " not in raw and b", " not in raw  # separators=(",", ":") over the whole body
    # ensure_ascii=False: the fixture's unicode-with-spaces folder name ("夏 の 本",
    # tests/conftest.py) must appear as raw UTF-8 bytes, not a \uXXXX escape.
    assert "夏 の 本".encode("utf-8") in raw
    assert b"\\u" not in raw
    data = r.json()
    assert isinstance(data, list) and data and data[0]["type"] == "folder"
    assert any(n.get("name") == "夏 の 本" for n in data)
