"""#226: archives with 2+ top-level internal folders split into subfolder nodes.

Regression first: a multi-folder ZIP must appear as multiple folders
(each an independent work at ``<archive>/<innerdir>``), not one flat chapter.
Single-internal-dir archives nest under one folder node; EPUB/PDF never split.
"""

import os
import struct
import sys
import zipfile
import zlib
from pathlib import Path

import pytest

os.environ["BOOKQ_TEST_MODE"] = "true"

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from app.scanner import scan_tree  # noqa: E402


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


@pytest.fixture
def split_tree(tmp_path: Path) -> Path:
    _make_png(tmp_path / "src.png")
    with zipfile.ZipFile(tmp_path / "multi.zip", "w") as zf:
        zf.write(tmp_path / "src.png", "v01/a.png")
        zf.write(tmp_path / "src.png", "v01/b.png")
        zf.write(tmp_path / "src.png", "v02/c.png")
        zf.write(tmp_path / "src.png", "v02/d.png")
    with zipfile.ZipFile(tmp_path / "single.zip", "w") as zf:
        zf.write(tmp_path / "src.png", "ch1/1.png")
        zf.write(tmp_path / "src.png", "ch1/2.png")
    with zipfile.ZipFile(tmp_path / "mixed.zip", "w") as zf:
        zf.write(tmp_path / "src.png", "loose.png")
        zf.write(tmp_path / "src.png", "v01/a.png")
        zf.write(tmp_path / "src.png", "v02/b.png")
    return tmp_path


def test_multi_folder_archive_splits_into_subfolders(split_tree: Path):
    tree = scan_tree(split_tree)
    node = next(n for n in tree if n["name"] == "multi.zip")
    subs = {c["name"]: c for c in node["children"] if c["type"] == "folder"}
    assert set(subs) == {"v01", "v02"}
    assert [c["name"] for c in subs["v01"]["children"]] == ["a.png", "b.png"]
    assert [c["name"] for c in subs["v02"]["children"]] == ["c.png", "d.png"]
    assert all(c["type"] == "image" for c in subs["v01"]["children"])
    assert subs["v01"]["thumb"] == "a.png"
    assert node["thumb"] == "v01/a.png"


def test_single_inner_dir_nests_under_one_folder(split_tree: Path):
    tree = scan_tree(split_tree)
    node = next(n for n in tree if n["name"] == "single.zip")
    assert [c["name"] for c in node["children"]] == ["ch1"]
    ch1 = node["children"][0]
    assert ch1["type"] == "folder"
    assert [c["name"] for c in ch1["children"]] == ["1.png", "2.png"]
    assert all(c["type"] == "image" for c in ch1["children"])
    assert "archive" not in ch1
    assert node["thumb"] == "ch1/1.png"


def test_mixed_loose_images_and_dirs(split_tree: Path):
    tree = scan_tree(split_tree)
    node = next(n for n in tree if n["name"] == "mixed.zip")
    by_name = {c["name"]: c for c in node["children"]}
    assert by_name["loose.png"]["type"] == "image"
    assert by_name["v01"]["type"] == "folder"
    assert [c["name"] for c in by_name["v01"]["children"]] == ["a.png"]
    assert [c["name"] for c in by_name["v02"]["children"]] == ["b.png"]


@pytest.fixture
def split_client(split_tree: Path, monkeypatch):
    monkeypatch.setenv("DOWNLOADS_DIR", str(split_tree))
    from app.main import create_app

    from fastapi.testclient import TestClient

    return TestClient(create_app())


def test_api_images_scoped_to_inner_dir(split_client):
    # Names are relative to the inner dir (same contract as plain folders):
    # the viewer/offline code builds /image/<folder>/<name> with
    # folder="multi.zip/v01", so full inner paths here would double the prefix.
    res = split_client.get("/api/images", params={"folder": "multi.zip/v01"})
    assert res.status_code == 200
    assert res.json() == ["a.png", "b.png"]
    res = split_client.get("/api/images", params={"folder": "multi.zip/v02"})
    assert res.status_code == 200
    assert res.json() == ["c.png", "d.png"]
    # Whole-archive listing still returns everything.
    res = split_client.get("/api/images", params={"folder": "multi.zip"})
    assert res.status_code == 200
    assert res.json() == ["v01/a.png", "v01/b.png", "v02/c.png", "v02/d.png"]


def test_api_folders_inner_dir_is_viewer_addressable(split_client):
    res = split_client.get("/api/folders")
    assert res.status_code == 200
    tree = res.json()
    multi = next(n for n in tree if n["name"] == "multi.zip")
    v01 = next(c for c in multi["children"] if c["name"] == "v01")
    # Viewer composes _fullPath as parent + "/" + name.
    assert multi["name"] + "/" + v01["name"] == "multi.zip/v01"
    # The inner-dir node is findable via the same tree walk the server uses.
    from app.routes import _find_tree_node

    found = _find_tree_node(tree, "multi.zip/v01")
    assert found is not None and found["name"] == "v01"
    assert [c["name"] for c in found["children"]] == ["a.png", "b.png"]
    # Entry images still serve under the full inner path.
    img = split_client.get("/image/multi.zip/v01/a.png")
    assert img.status_code == 200
    assert img.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_api_images_unknown_inner_dir_is_404(split_client):
    res = split_client.get("/api/images", params={"folder": "multi.zip/nope"})
    assert res.status_code == 404
