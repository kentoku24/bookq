"""Multi-top-dir archive split over the v1 HTTP contract (#483 v1 port).

Regression first, at the HTTP level against the live ``server`` fixture,
so this file runs in BOTH lanes (default uvicorn + ``BOOKQ_TEST_SERVER=rs``
viewer-rs): a multi-folder ZIP appears as multiple folders (each an
independent work at ``<archive>/<innerdir>``), not one flat chapter.
Single-internal-dir archives nest under one folder node; mixed loose
images stay images.

Python internals (scan_tree split units) stay in
``tests/test_archive_split_internals.py`` and retire with ``app/`` in
Phase 3.

Run:
    uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_archive_split.py -q
"""

import json
import struct
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from pathlib import Path

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
            return resp.status, raw, _headers(resp)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), {k.lower(): v for k, v in (e.headers or {}).items()}


def _get(server, path, **kw):
    status, raw, headers = _req(server, "GET", path, **kw)
    if status == 202:  # cold index build: retry once after Retry-After
        import time

        time.sleep(2.5)
        status, raw, headers = _req(server, "GET", path, **kw)
    return status, raw, headers


def _post(server, path):
    req = urllib.request.Request(server + path, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status


def _json(raw: bytes):
    return json.loads(raw.decode("utf-8"))


def _images(server, folder: str):
    status, raw, _ = _get(server, "/api/v1/images?folder=" + urllib.parse.quote(folder, safe=""))
    assert status == 200, folder
    body = _json(raw)
    assert body["folder"] == folder
    return body["images"]


def _tree(server):
    status, raw, _ = _get(server, "/api/v1/tree")
    assert status == 200
    body = _json(raw)
    assert body["kind"] == "tree"
    return body["nodes"]


def _node(nodes, name: str):
    return next(n for n in nodes if n["name"] == name)


def _find(nodes, target: str, parent: str = ""):
    """Tree walk mirroring the server lookup — never split names on "/".

    Flattened folder names contain "/" (e.g. a single-child wrapper), so
    match on the composed full path instead of segment-wise descent.
    """
    for n in nodes:
        full = f"{parent}/{n['name']}" if parent else n["name"]
        if full == target:
            return n
        kids = n.get("children")
        if kids and target.startswith(full + "/"):
            hit = _find(kids, target, full)
            if hit is not None:
                return hit
    return None


def _make_png(path: Path) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 40, 60, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes((60, 200, 60)) * 40
    idat = zlib.compress(row * 60)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
    )


@pytest.fixture(scope="module", autouse=True)
def split_lib(server, downloads_tree: Path):
    """Stage the split-shape archives in the shared tree and publish."""
    work = downloads_tree / "_aport_split_work"
    work.mkdir(exist_ok=True)
    _make_png(work / "src.png")
    with zipfile.ZipFile(downloads_tree / "aport_multi.zip", "w") as zf:
        zf.write(work / "src.png", "v01/a.png")
        zf.write(work / "src.png", "v01/b.png")
        zf.write(work / "src.png", "v02/c.png")
        zf.write(work / "src.png", "v02/d.png")
    with zipfile.ZipFile(downloads_tree / "aport_single.zip", "w") as zf:
        zf.write(work / "src.png", "ch1/1.png")
        zf.write(work / "src.png", "ch1/2.png")
    with zipfile.ZipFile(downloads_tree / "aport_mixed.zip", "w") as zf:
        zf.write(work / "src.png", "loose.png")
        zf.write(work / "src.png", "v01/a.png")
        zf.write(work / "src.png", "v02/b.png")
    import shutil

    shutil.rmtree(work)
    assert _post(server, "/api/v1/index/refresh") == 200
    yield
    import shutil

    for name in ("aport_multi.zip", "aport_single.zip", "aport_mixed.zip"):
        (downloads_tree / name).unlink(missing_ok=True)
    _post(server, "/api/v1/index/refresh")


def test_tree_multi_folder_archive_splits_into_subfolders(server):
    node = _node(_tree(server), "aport_multi.zip")
    subs = {c["name"]: c for c in node["children"] if c["type"] == "folder"}
    assert set(subs) == {"v01", "v02"}
    assert [c["name"] for c in subs["v01"]["children"]] == ["a.png", "b.png"]
    assert [c["name"] for c in subs["v02"]["children"]] == ["c.png", "d.png"]
    assert all(c["type"] == "image" for c in subs["v01"]["children"])
    assert subs["v01"]["thumb"] == "a.png"
    assert node["thumb"] == "v01/a.png"


def test_tree_single_inner_dir_nests_under_one_folder(server):
    node = _node(_tree(server), "aport_single.zip")
    assert [c["name"] for c in node["children"]] == ["ch1"]
    ch1 = node["children"][0]
    assert ch1["type"] == "folder"
    assert [c["name"] for c in ch1["children"]] == ["1.png", "2.png"]
    assert all(c["type"] == "image" for c in ch1["children"])
    assert "archive" not in ch1
    assert node["thumb"] == "ch1/1.png"


def test_tree_mixed_loose_images_and_dirs(server):
    node = _node(_tree(server), "aport_mixed.zip")
    by_name = {c["name"]: c for c in node["children"]}
    assert by_name["loose.png"]["type"] == "image"
    assert by_name["v01"]["type"] == "folder"
    assert [c["name"] for c in by_name["v01"]["children"]] == ["a.png"]
    assert [c["name"] for c in by_name["v02"]["children"]] == ["b.png"]


def test_images_scoped_to_inner_dir(server):
    # Names are relative to the inner dir (same contract as plain folders):
    # the viewer/offline code builds /image/<folder>/<name> with
    # folder="aport_multi.zip/v01", so full inner paths would double the prefix.
    assert _images(server, "aport_multi.zip/v01") == ["a.png", "b.png"]
    assert _images(server, "aport_multi.zip/v02") == ["c.png", "d.png"]
    # Whole-archive listing still returns everything.
    assert _images(server, "aport_multi.zip") == ["v01/a.png", "v01/b.png", "v02/c.png", "v02/d.png"]


def test_tree_inner_dir_is_viewer_addressable(server):
    nodes = _tree(server)
    multi = _node(nodes, "aport_multi.zip")
    v01 = _node(multi["children"], "v01")
    # Viewer composes _fullPath as parent + "/" + name.
    assert multi["name"] + "/" + v01["name"] == "aport_multi.zip/v01"
    # The inner-dir node is findable via the same tree walk the server uses.
    found = _find(nodes, "aport_multi.zip/v01")
    assert found is not None and found["name"] == "v01"
    assert [c["name"] for c in found["children"]] == ["a.png", "b.png"]
    # Entry images still serve under the full inner path.
    status, raw, _ = _get(server, "/image/aport_multi.zip/v01/a.png")
    assert status == 200
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"


def test_images_unknown_inner_dir_is_404(server):
    status, _, _ = _get(
        server, "/api/v1/images?folder=" + urllib.parse.quote("aport_multi.zip/nope", safe="")
    )
    assert status == 404
