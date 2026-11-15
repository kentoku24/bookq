"""Shallow folder listing — `GET /api/v1/tree?path=<rel>&depth=1` (Todo 2, #476 v1 port).

`?depth=1` returns only direct `type == "folder"` children (children == [],
metadata/subtree_facets keys only when the index actually carries them);
omitting `depth` returns the full `{"kind": "tree"}` envelope.

The v0-compat shape (the folders endpoint's bare list) is pinned in
`test_api_v0.py` and index-cache internals in `test_index_gate.py` — both
retire with `app/` in Phase 3.

Run:
    uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_folders_shallow.py -q
"""

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest


def _get(server, path, headers=None):
    req = urllib.request.Request(server + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, raw, hdrs
    except urllib.error.HTTPError as e:
        return e.code, e.read(), {k.lower(): v for k, v in (e.headers or {}).items()}


def _post(server, path):
    req = urllib.request.Request(server + path, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()
        return resp.status


def _nodes(body: bytes):
    return json.loads(body.decode("utf-8"))["nodes"]


def _full_tree(server):
    status, raw, _ = _get(server, "/api/v1/tree")
    assert status == 200
    data = json.loads(raw.decode("utf-8"))
    assert data["kind"] == "tree" and isinstance(data["nodes"], list) and data["nodes"]
    return data["nodes"]


def test_shallow_root_lists_only_top_level_folders(server):
    full = _full_tree(server)
    status, raw, _ = _get(server, "/api/v1/tree?depth=1")
    assert status == 200
    data = json.loads(raw.decode("utf-8"))
    assert data["kind"] == "children"
    nodes = data["nodes"]
    expected = [n["name"] for n in full if n.get("type") == "folder"]
    assert [n["name"] for n in nodes] == expected
    assert len(nodes) == len(full)  # fixture root holds folders only
    for node in nodes:
        assert node["type"] == "folder"
        # folderHasOnlyImages (offline-core.js) and getBrowseChildren (app.js)
        # depend on children being a list, never None.
        assert isinstance(node["children"], list)
        assert node["children"] == []


def test_shallow_carries_no_metadata_or_facets_keys(server):
    # Frontend must only OfflineTree.putTree() full-tree snapshots. The shallow
    # shape (children == [] everywhere, no metadata/subtree_facets on the
    # metadata-less fixture tree, no nested grandchildren) is deliberately NOT
    # a full tree, so a shallow payload must never pollute the offline snapshot.
    status, raw, _ = _get(server, "/api/v1/tree?depth=1")
    assert status == 200
    for node in _nodes(raw):
        assert "metadata" not in node
        assert "subtree_facets" not in node


def test_omitted_depth_returns_full_tree_envelope(server):
    full = _full_tree(server)
    by_name = {n["name"]: n for n in full}
    # Nested grandchildren survive on the full path.
    assert "group" in by_name
    children = by_name["group"]["children"]
    assert isinstance(children, list) and len(children) > 0
    assert any(c.get("type") == "folder" for c in children)
    # Metadata attach still runs on the full path (same key set as before).
    assert any("has_metadata" in n for n in full)


def test_nested_path_shallow_and_flatten_name(server):
    full = _full_tree(server)
    group = next(n for n in full if n["name"] == "group")
    status, raw, _ = _get(server, "/api/v1/tree?path=group&depth=1")
    assert status == 200
    nodes = _nodes(raw)
    expected = [c["name"] for c in group["children"] if c.get("type") == "folder"]
    assert [n["name"] for n in nodes] == expected
    for node in nodes:
        assert isinstance(node["children"], list) and node["children"] == []
    # Flattened single-child wrapper resolves WITHOUT splitting "/" in the
    # name: "wrap/inner/vol1" is one folder node under "wrap" (segment-wise
    # descent into "inner" would miss it).
    status, raw, _ = _get(server, "/api/v1/tree?path=wrap/inner/vol1&depth=1")
    assert status == 200
    assert _nodes(raw) == []  # image-only children are filtered out


def test_unknown_path_returns_404(server):
    assert _get(server, "/api/v1/tree?path=no_such_folder&depth=1")[0] == 404
    assert _get(server, "/api/v1/tree?path=group/nope&depth=1")[0] == 404


def test_outside_downloads_returns_403(server):
    assert _get(server, "/api/v1/tree?path=..&depth=1")[0] == 403
    assert _get(server, "/api/v1/tree?path=/etc&depth=1")[0] == 403


def test_depth_below_one_returns_400(server):
    assert _get(server, "/api/v1/tree?depth=0")[0] == 400
    assert _get(server, "/api/v1/tree?depth=-2")[0] == 400


def test_non_literal_int_params_return_422(server):
    """`type: integer` in the spec means the wire value must be an integer
    literal — pydantic's lax coercion used to accept `since=0.0`/`depth=1.0`,
    which schemathesis flags as ST201 (#326)."""
    import urllib.parse

    for params in (
        {"since": "0.0"}, {"since": "1.5"}, {"since": "1e2"}, {"since": "abc"},
        {"since": "１２"},  # Unicode digits: \d matches and int() parses them
        {"depth": "1.0"}, {"depth": "x"},
    ):
        q = urllib.parse.urlencode(params)
        assert _get(server, f"/api/v1/tree?{q}")[0] == 422, params
    # Integer literals stay accepted — a non-matching `since` still serves
    # the tree, and negatives are schema-valid integers.
    assert _get(server, "/api/v1/tree?since=0")[0] == 200
    assert _get(server, "/api/v1/tree?since=-3")[0] == 200
    assert _get(server, "/api/v1/tree?depth=1")[0] == 200


# --- Todo 6: HTTP cache (ETag + Cache-Control: private, max-age=60) ---


def test_full_tree_carries_etag_and_private_cache_control(server):
    status, _, headers = _get(server, "/api/v1/tree")
    assert status == 200
    assert headers.get("etag"), "full-tree response must carry an ETag"
    assert headers.get("cache-control") == "private, max-age=60"


def test_full_tree_if_none_match_returns_304_with_empty_body(server):
    status, raw, headers = _get(server, "/api/v1/tree")
    assert status == 200
    etag = headers["etag"]
    status2, raw2, headers2 = _get(server, "/api/v1/tree", headers={"If-None-Match": etag})
    assert status2 == 304
    assert raw2 == b""
    assert headers2.get("etag") == etag
    # Re-fetch without precondition: content identical to the cached one.
    status3, raw3, headers3 = _get(server, "/api/v1/tree")
    assert status3 == 200
    assert raw3 == raw
    assert headers3.get("etag") == etag  # stable within the index TTL


def test_shallow_carries_etag_and_304(server):
    status, raw, headers = _get(server, "/api/v1/tree?depth=1")
    assert status == 200
    etag = headers.get("etag")
    assert etag, "shallow response must carry an ETag"
    assert headers.get("cache-control") == "private, max-age=60"
    status2, raw2, headers2 = _get(
        server, "/api/v1/tree?depth=1", headers={"If-None-Match": etag}
    )
    assert status2 == 304
    assert raw2 == b""
    assert headers2.get("etag") == etag
    status3, raw3, headers3 = _get(server, "/api/v1/tree?depth=1")
    assert raw3 == raw
    assert headers3.get("etag") == etag


def test_etag_differs_by_path_and_depth(server):
    full_etag = _get(server, "/api/v1/tree")[2]["etag"]
    shallow_root_etag = _get(server, "/api/v1/tree?depth=1")[2]["etag"]
    shallow_group_etag = _get(server, "/api/v1/tree?path=group&depth=1")[2]["etag"]
    assert full_etag != shallow_root_etag
    assert shallow_root_etag != shallow_group_etag
    assert full_etag != shallow_group_etag


# --- Task 12 (#196): ETag tracks the tree, not the index-cache TTL ---


def test_etag_changes_when_tree_changes(server, downloads_tree):
    """A new folder bumps the ETag after a synchronous v1 index refresh
    (`POST /api/v1/index/refresh` — same contract in both backends)."""
    etag1 = _get(server, "/api/v1/tree")[2]["etag"]
    new = downloads_tree / "zz_new_folder_for_etag"
    new.mkdir()
    try:
        from tests.conftest import _make_png

        _make_png(new / "001.png")
        assert _post(server, "/api/v1/index/refresh") == 200
        etag2 = _get(server, "/api/v1/tree")[2]["etag"]
        assert etag2 != etag1
        # shallow sees the new folder once the index is refreshed
        status, raw, _ = _get(server, "/api/v1/tree?depth=1")
        assert status == 200
        assert "zz_new_folder_for_etag" in [n["name"] for n in _nodes(raw)]
    finally:
        import shutil

        shutil.rmtree(new)
        _post(server, "/api/v1/index/refresh")


def test_full_tree_body_is_compact_json_and_matches_payload(server):
    status, raw, _ = _get(server, "/api/v1/tree")
    assert status == 200
    assert raw.startswith(b'{"kind":"tree"')  # compact separators, no spaces
    assert b": " not in raw and b", " not in raw
    # ensure_ascii=False equivalent: the fixture's unicode-with-spaces folder
    # name ("夏 の 本", tests/conftest.py) must appear as raw UTF-8 bytes,
    # not a \uXXXX escape.
    assert "夏 の 本".encode("utf-8") in raw
    assert b"\\u" not in raw
    data = json.loads(raw.decode("utf-8"))
    assert isinstance(data["nodes"], list) and data["nodes"][0]["type"] == "folder"
    assert any(n.get("name") == "夏 の 本" for n in data["nodes"])
