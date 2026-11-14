"""Recursive nested-archive folders over the v1 HTTP contract (#483 v1 port).

ZIP + RAR5-store archives with a ``root cover + vol01(+sub) + vol02`` shape
must scan into a recursive tree, serve descendant-relative prefixes, and keep
all existing guards (junk exclusion, EPUB-flat, 404s) — pinned at the HTTP
level against the live ``server`` fixture, so this file runs in BOTH lanes
(default uvicorn + ``BOOKQ_TEST_SERVER=rs`` viewer-rs).

Python internals (scan_tree / build_archive_tree / list_archive_entries /
_find_tree_node units, entry-cap and deep-nesting collapse, rar-vol2
lock-in) stay in ``tests/test_archive_nested_internals.py`` and retire
with ``app/`` in Phase 3.

Run:
    uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_archive_nested.py -q
"""

import json
import struct
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from pathlib import Path
from urllib.parse import quote

import pytest

ARCHIVES = ["nested.zip", "nested.cbr"]


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


def _make_epub(path: Path, pages: list[tuple[str, str]], png: bytes) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?>'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        items: list[str] = []
        refs: list[str] = []
        for i, (xhtml, img) in enumerate(pages):
            items.append(f'<item id="pg{i}" href="Text/{xhtml}" media-type="application/xhtml+xml"/>')
            items.append(f'<item id="im{i}" href="Images/{quote(img)}" media-type="image/png"/>')
            refs.append(f'<itemref idref="pg{i}"/>')
            src = "../Images/" + quote(img)
            body = f'<img src="{src}" alt=""/>'
            zf.writestr(f"OEBPS/Text/{xhtml}", f"<html><body>{body}</body></html>")
            zf.writestr(f"OEBPS/Images/{img}", png)
        opf = (
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            "<metadata/>" + f"<manifest>{''.join(items)}</manifest>"
            + f"<spine>{''.join(refs)}</spine></package>"
        )
        zf.writestr("OEBPS/content.opf", opf)


# Logical nested content shared by the ZIP and the RAR5-store fixture.
NESTED_ENTRIES = [
    "cover.jpg",
    "vol01/01.jpg",
    "vol01/02.jpg",
    "vol01/sub/03.jpg",
    "vol02/01.jpg",
]
JUNK_ENTRIES = [
    "vol01/__MACOSX/x.jpg",
    "vol01/.dot.jpg",
    "../evil.png",
    "/abs.png",
    "vol01/sub/../../evil2.png",
    "readme.txt",
]


@pytest.fixture(scope="module", autouse=True)
def nested_lib(server, downloads_tree: Path):
    """Stage the nested-archive fixtures in the shared tree and publish."""
    from tests.fixtures.rar5 import make_rar5_store

    work = downloads_tree / "_aport_nested_work"
    work.mkdir(exist_ok=True)
    _make_png(work / "src.png")
    png = (work / "src.png").read_bytes()
    payloads = [(name, png) for name in NESTED_ENTRIES + JUNK_ENTRIES]
    with zipfile.ZipFile(downloads_tree / "nested.zip", "w") as zf:
        for name, data in payloads:
            zf.writestr(name, data)
    make_rar5_store(downloads_tree / "nested.cbr", payloads)
    jp_payloads = [("vol01/\u7b2c01\u8a71/01.jpg", png), ("vol01/01.jpg", png)]
    with zipfile.ZipFile(downloads_tree / "aport_jp.zip", "w") as zf:
        for name, data in jp_payloads:
            zf.writestr(name, data)
    make_rar5_store(downloads_tree / "aport_jp.cbr", jp_payloads)
    _make_epub(
        downloads_tree / "aport_manga.epub",
        [("c.xhtml", "z_cover.png"), ("b.xhtml", "a_p1.png"), ("a.xhtml", "m_p2.png")],
        png,
    )
    (downloads_tree / "aport_broken.rar").write_bytes(b"Rar!\x1a\x07\x01\x00garbage")
    import shutil

    shutil.rmtree(work)
    assert _post(server, "/api/v1/index/refresh") == 200
    yield
    import shutil

    for name in ("nested.zip", "nested.cbr", "aport_jp.zip", "aport_jp.cbr",
                 "aport_manga.epub", "aport_broken.rar"):
        (downloads_tree / name).unlink(missing_ok=True)
    _post(server, "/api/v1/index/refresh")


def _expected_png(tmp_path: Path) -> bytes:
    _make_png(tmp_path / "expect.png")
    return (tmp_path / "expect.png").read_bytes()


# ── recursive tree shape ────────────────────────────────────────────────


@pytest.mark.parametrize("archive", ARCHIVES)
def test_tree_recursive_shape(server, archive: str):
    node = _node(_tree(server), archive)
    assert node["type"] == "folder"
    assert node["archive"] is True
    # Root loose-first: cover leads, then sub-folders.
    assert [c["name"] for c in node["children"]] == ["cover.jpg", "vol01", "vol02"]
    vol01 = _node(node["children"], "vol01")
    assert [c["name"] for c in vol01["children"]] == ["01.jpg", "02.jpg", "sub"]
    sub = _node(vol01["children"], "sub")
    assert [c["name"] for c in sub["children"]] == ["03.jpg"]
    assert [c["name"] for c in _node(node["children"], "vol02")["children"]] == ["01.jpg"]


@pytest.mark.parametrize("archive", ARCHIVES)
def test_tree_nested_thumbs(server, archive: str):
    node = _node(_tree(server), archive)
    assert node["thumb"] == "cover.jpg"
    assert node["thumbs"] == ["vol01/01.jpg", "vol02/01.jpg"]
    vol01 = _node(node["children"], "vol01")
    assert vol01["thumb"] == "01.jpg"
    assert "archive" not in vol01
    sub = _node(vol01["children"], "sub")
    assert sub["thumb"] == "03.jpg"
    assert "archive" not in sub
    assert _node(node["children"], "vol02")["thumb"] == "01.jpg"


# ── prefix + delivery ───────────────────────────────────────────────────


@pytest.mark.parametrize("archive", ARCHIVES)
def test_images_nested_prefix_is_descendant_relative(server, archive: str):
    assert _images(server, f"{archive}/vol01") == ["01.jpg", "02.jpg", "sub/03.jpg"]
    assert _images(server, f"{archive}/vol01/sub") == ["03.jpg"]


@pytest.mark.parametrize("archive", ARCHIVES)
def test_tree_addresses_nested_prefix(server, archive: str):
    nodes = _tree(server)
    found = _find(nodes, f"{archive}/vol01")
    assert found is not None and found["type"] == "folder"
    assert found["thumb"] == "01.jpg"
    assert _find(nodes, f"{archive}/vol01/sub")["thumb"] == "03.jpg"
    # Shallow subtree listing resolves the archive prefix too.
    status, raw, _ = _get(
        server, "/api/v1/tree?path=" + urllib.parse.quote(f"{archive}/vol01", safe="") + "&depth=1"
    )
    assert status == 200
    body = _json(raw)
    assert body["kind"] == "children"
    assert [c["name"] for c in body["nodes"]] == ["sub"]


@pytest.mark.parametrize("archive", ARCHIVES)
def test_image_delivery_nested_inner(server, tmp_path: Path, archive: str):
    status, raw, _ = _get(server, f"/image/{archive}/vol01/sub/03.jpg")
    assert status == 200
    assert raw == _expected_png(tmp_path)


@pytest.mark.parametrize("archive", ARCHIVES)
def test_thumb_delivery_multi_grid_path(server, archive: str):
    node = _node(_tree(server), archive)
    assert node["thumbs"] == ["vol01/01.jpg", "vol02/01.jpg"]
    for rel in node["thumbs"]:
        status, raw, headers = _get(server, f"/thumb/{archive}/{rel}?w=200")
        assert status == 200, rel
        assert headers["content-type"].startswith("image/")
        assert raw[:2] == b"\xff\xd8"


# ── non-ASCII / EPUB-flat / errors ──────────────────────────────────────


@pytest.mark.parametrize("archive", ["aport_jp.zip", "aport_jp.cbr"])
def test_non_ascii_nested_inner_listed_and_servable(server, tmp_path: Path, archive: str):
    inner = "vol01/\u7b2c01\u8a71/01.jpg"
    assert inner in _images(server, archive)
    vol01 = _node(_node(_tree(server), archive)["children"], "vol01")
    assert "\u7b2c01\u8a71" in [c["name"] for c in vol01["children"]]
    status, raw, _ = _get(server, "/image/" + quote(f"{archive}/{inner}"))
    assert status == 200
    assert raw == _expected_png(tmp_path)


def test_epub_stays_flat_spine_order(server):
    node = _node(_tree(server), "aport_manga.epub")
    assert node["archive"] is True
    assert all(c["type"] == "image" for c in node["children"])
    assert [c["name"] for c in node["children"]] == [
        "OEBPS/Images/z_cover.png",
        "OEBPS/Images/a_p1.png",
        "OEBPS/Images/m_p2.png",
    ]
    assert node["thumb"] == "OEBPS/Images/z_cover.png"


def test_unknown_prefix_404_and_broken_rar_empty(server):
    for archive in ARCHIVES:
        assert _get(server, "/api/v1/images?folder=" + urllib.parse.quote(f"{archive}/nope", safe=""))[0] == 404
        assert _get(server, f"/image/{archive}/nope.png")[0] == 404
    node = _node(_tree(server), "aport_broken.rar")
    assert node["children"] == []
    assert node["thumb"] is None


# ── rar vol2 prefix + delivery ──────────────────────────────────────────

VOL2_LOOSE = [f"top/v02/p{i:02d}.jpg" for i in range(1, 7)]
VOL2_SUB = "top/v02/\u5358\u30da\u30fc\u30b8"
VOL2_CASE_NAMES = ["cover.jpg", "top/v01/cover.jpg"] + VOL2_LOOSE + [
    f"{VOL2_SUB}/s{i:02d}.jpg" for i in range(1, 3)
]


def test_rar_vol2_prefix_and_delivery(server, downloads_tree: Path, tmp_path: Path):
    from tests.fixtures.rar5 import make_rar5_store

    _make_png(tmp_path / "src.png")
    png = (tmp_path / "src.png").read_bytes()
    make_rar5_store(downloads_tree / "aport_vol2.cbr", [(name, png) for name in VOL2_CASE_NAMES])
    try:
        assert _post(server, "/api/v1/index/refresh") == 200
        assert _images(server, "aport_vol2.cbr/top/v02") == [f"p{i:02d}.jpg" for i in range(1, 7)] + [
            f"\u5358\u30da\u30fc\u30b8/s01.jpg",
            f"\u5358\u30da\u30fc\u30b8/s02.jpg",
        ]
        status, raw, _ = _get(server, "/image/aport_vol2.cbr/top/v02/p01.jpg")
        assert status == 200
        assert raw == png
        status, raw, _ = _get(
            server, "/image/" + quote("aport_vol2.cbr/top/v02/\u5358\u30da\u30fc\u30b8/s02.jpg")
        )
        assert status == 200
        assert raw == png
        assert _get(
            server, "/api/v1/images?folder=" + urllib.parse.quote("aport_vol2.cbr/top/nope", safe="")
        )[0] == 404
    finally:
        (downloads_tree / "aport_vol2.cbr").unlink(missing_ok=True)
        _post(server, "/api/v1/index/refresh")
