"""Archive support over the v1 HTTP contract — ZIP/CBZ/RAR/PDF/EPUB (#483 v1 port).

Browsable-archive behavior pinned at the HTTP level against the live
``server`` fixture, so this file runs in BOTH lanes (default uvicorn +
``BOOKQ_TEST_SERVER=rs`` viewer-rs):

- tree shape (archive nodes, thumbs, nested prefixes)
- images envelopes (flat listings, pdf pages, empty/broken archives)
- entry delivery (ETag/Range/304, non-ASCII names, error codes)
- thumbnail delivery from archive entries

Python internals (scan_tree / list_archive_entries / archive_pdf /
MetadataIndex / warmup-cache truncation / tool-missing monkeypatches) stay
in ``tests/test_archive_internals.py`` and retire with ``app/`` in Phase 3.

The module fixture stages the archives in the shared ``downloads_tree``
and publishes them with a synchronous index refresh; it removes them
afterwards so the shared tree is unchanged for other modules.

Run:
    uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_archive.py -q
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
    assert body["kind"] == "tree" and isinstance(body["generation"], int)
    return body["nodes"]


def _node(nodes, name: str):
    return next(n for n in nodes if n["name"] == name)


def _make_png(path: Path) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 40, 60, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes((200, 60, 60)) * 40
    idat = zlib.compress(row * 60)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
    )


def _make_pdf(path: Path, pages: int = 2) -> None:
    objs: list[str] = ["1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"]
    kids = " ".join(f"{3 + i} 0 R" for i in range(pages))
    objs.append(f"2 0 obj\n<< /Type /Pages /Kids [{kids}] /Count {pages} >>\nendobj\n")
    for i in range(pages):
        objs.append(
            f"{3 + i} 0 obj\n<< /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 200 200] /Contents {3 + pages + i} 0 R >>\nendobj\n"
        )
    for i in range(pages):
        content = f"BT 10 90 Td (Page {i + 1}) Tj ET"
        objs.append(
            f"{3 + pages + i} 0 obj\n<< /Length {len(content)} >>\n"
            f"stream\n{content}\nendstream\nendobj\n"
        )
    path.write_bytes(
        b"%PDF-1.4\n" + "".join(objs).encode() + b"trailer\n<< /Root 1 0 R >>\n%%EOF"
    )


def _make_epub(path: Path, pages: list[tuple[str, str]], png: bytes, *, svg=False, broken=False) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        if not broken:
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
            if svg:
                body = (
                    '<svg xmlns="http://www.w3.org/2000/svg" '
                    'xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 40 60">'
                    f'<image width="40" height="60" xlink:href="{src}"/></svg>'
                )
            else:
                body = f'<img src="{src}" alt=""/>'
            zf.writestr(
                f"OEBPS/Text/{xhtml}",
                '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
                f"<head><title>p</title></head><body>{body}</body></html>",
            )
            zf.writestr(f"OEBPS/Images/{img}", png)
        if not broken:
            zf.writestr(
                "OEBPS/content.opf",
                '<?xml version="1.0"?>'
                '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="u">'
                f'<metadata/><manifest>{"".join(items)}</manifest>'
                f'<spine>{"".join(refs)}</spine></package>',
            )


_ARCHIVE_FILES = [
    "work.zip",
    "empty.zip",
    "book.cbz",
    "doc.pdf",
    "book.cbr",
    "broken.rar",
    "manga.epub",
    "svg.epub",
    "broken.epub",
]
_ARCHIVE_DIRS = ["aport_simple", "aport_sub"]


@pytest.fixture(scope="module", autouse=True)
def archive_lib(server, downloads_tree: Path):
    """Stage the archive fixtures in the shared tree and publish via refresh."""
    from tests.fixtures.rar5 import make_rar5_store

    work = downloads_tree / "_aport_work"
    work.mkdir(exist_ok=True)
    for name in ["10.png", "2.png", "1.png"]:
        _make_png(work / f"src_{name}")
    with zipfile.ZipFile(downloads_tree / "work.zip", "w") as zf:
        zf.write(work / "src_10.png", "10.png")
        zf.write(work / "src_2.png", "2.png")
        zf.write(work / "src_1.png", "ch1/1.png")
        zf.writestr("../evil.png", "x")
        zf.writestr("/abs.png", "x")
        zf.writestr("readme.txt", "hi")
    with zipfile.ZipFile(downloads_tree / "empty.zip", "w") as zf:
        zf.writestr("readme.txt", "no images here")
    with zipfile.ZipFile(downloads_tree / "book.cbz", "w") as zf:
        zf.write(work / "src_1.png", "p01.png")
        zf.write(work / "src_2.png", "p02.png")
    _make_pdf(downloads_tree / "doc.pdf", pages=2)
    make_rar5_store(
        downloads_tree / "book.cbr",
        [
            ("10.png", (work / "src_10.png").read_bytes()),
            ("2.png", (work / "src_2.png").read_bytes()),
            ("ch1/1.png", (work / "src_1.png").read_bytes()),
            ("../evil.png", b"x"),
            ("readme.txt", b"hi"),
        ],
    )
    (downloads_tree / "broken.rar").write_bytes(b"Rar!\x1a\x07\x01\x00garbage")
    png = (work / "src_1.png").read_bytes()
    _make_epub(
        downloads_tree / "manga.epub",
        [("c.xhtml", "z_cover.png"), ("b.xhtml", "a_p1.png"), ("a.xhtml", "m_p2.png")],
        png,
    )
    _make_epub(
        downloads_tree / "svg.epub",
        [("p1.xhtml", "page 1.png"), ("p2.xhtml", "page 2.png")],
        png,
        svg=True,
    )
    _make_epub(
        downloads_tree / "broken.epub",
        [("c.xhtml", "z_cover.png"), ("b.xhtml", "a_p1.png")],
        png,
        broken=True,
    )
    simple = downloads_tree / "aport_simple"
    simple.mkdir(exist_ok=True)
    _make_png(simple / "1.png")
    sub = downloads_tree / "aport_sub"
    sub.mkdir(exist_ok=True)
    with zipfile.ZipFile(sub / "nested.cbz", "w") as zf:
        zf.write(work / "src_1.png", "a.png")
        zf.write(work / "src_2.png", "b.png")
    import shutil

    shutil.rmtree(work)
    assert _post(server, "/api/v1/index/refresh") == 200
    yield
    import shutil

    for name in _ARCHIVE_FILES:
        (downloads_tree / name).unlink(missing_ok=True)
    for name in _ARCHIVE_DIRS:
        shutil.rmtree(downloads_tree / name, ignore_errors=True)
    _post(server, "/api/v1/index/refresh")


def _expected_png(tmp_path: Path) -> bytes:
    _make_png(tmp_path / "expect.png")
    return (tmp_path / "expect.png").read_bytes()


# ── tree ────────────────────────────────────────────────────────────────


def test_tree_shows_archives_as_folders(server):
    by_name = {n["name"]: n for n in _tree(server)}
    for fname in ("work.zip", "book.cbz", "doc.pdf", "empty.zip"):
        assert by_name[fname]["type"] == "folder", fname
    assert by_name["work.zip"]["thumb"] is not None
    assert by_name["doc.pdf"]["thumb"] == "p001.png"
    assert [c["name"] for c in by_name["doc.pdf"]["children"]] == ["p001.png", "p002.png"]
    assert by_name["doc.pdf"]["archive"] is True
    assert by_name["doc.pdf"]["pdf"] is True
    assert by_name["empty.zip"]["children"] == []
    assert by_name["empty.zip"]["thumb"] is None
    assert by_name["aport_simple"]["thumb"] == "1.png"


def test_tree_shows_rar_and_epub(server):
    by_name = {n["name"]: n for n in _tree(server)}
    assert by_name["book.cbr"]["thumb"] == "2.png"
    assert [c["name"] for c in by_name["book.cbr"]["children"]] == ["2.png", "10.png", "ch1"]
    ch1 = next(c for c in by_name["book.cbr"]["children"] if c["name"] == "ch1")
    assert ch1["type"] == "folder"
    assert [c["name"] for c in ch1["children"]] == ["1.png"]
    assert ch1["thumb"] == "1.png"
    assert "archive" not in ch1
    assert by_name["manga.epub"]["thumb"] == "OEBPS/Images/z_cover.png"
    assert len(by_name["manga.epub"]["children"]) == 3
    assert by_name["broken.rar"]["children"] == []
    assert by_name["broken.rar"]["thumb"] is None
    assert by_name["broken.epub"]["thumb"] == "OEBPS/Images/a_p1.png"


def test_tree_nested_archive_and_parent_thumb(server):
    by_name = {n["name"]: n for n in _tree(server)}
    sub = by_name["aport_sub"]
    nested = next(c for c in sub["children"] if c["name"] == "nested.cbz")
    assert nested["thumb"] == "a.png"
    assert sub["thumb"] == "nested.cbz/a.png"


# ── images envelopes ────────────────────────────────────────────────────


def test_images_archive_and_pdf(server):
    assert _images(server, "work.zip") == ["2.png", "10.png", "ch1/1.png"]
    assert _images(server, "doc.pdf") == ["p001.png", "p002.png"]
    assert _images(server, "empty.zip") == []


def test_images_rar_and_epub(server):
    assert _images(server, "book.cbr") == ["2.png", "10.png", "ch1/1.png"]
    assert _images(server, "manga.epub") == [
        "OEBPS/Images/z_cover.png",
        "OEBPS/Images/a_p1.png",
        "OEBPS/Images/m_p2.png",
    ]
    assert _images(server, "svg.epub") == [
        "OEBPS/Images/page 1.png",
        "OEBPS/Images/page 2.png",
    ]
    assert _images(server, "broken.epub") == [
        "OEBPS/Images/a_p1.png",
        "OEBPS/Images/z_cover.png",
    ]
    assert _images(server, "broken.rar") == []


def test_images_regular_folder_unaffected(server):
    assert _images(server, "aport_simple") == ["1.png"]


# ── entry delivery ──────────────────────────────────────────────────────


def test_serve_zip_entry_and_etag(server):
    status, raw, headers = _get(server, "/image/work.zip/2.png")
    assert status == 200
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    assert headers.get("etag")
    assert headers.get("cache-control", "").startswith("private")
    assert headers.get("accept-ranges") == "bytes"
    assert headers.get("last-modified")
    total = len(raw)
    status2, raw2, _ = _get(server, "/image/work.zip/2.png", headers={"If-None-Match": headers["etag"]})
    assert status2 == 304 and raw2 == b""
    status3, _, _ = _get(
        server, "/image/work.zip/2.png", headers={"If-Modified-Since": headers["last-modified"]}
    )
    assert status3 == 304
    status4, raw4, headers4 = _get(server, "/image/work.zip/2.png", headers={"Range": "bytes=0-9"})
    assert status4 == 206
    assert headers4["content-range"] == f"bytes 0-9/{total}"
    assert len(raw4) == 10
    assert raw4 == raw[:10]
    # Inverted range falls back to 200 full body (never empty 206).
    status5, raw5, _ = _get(server, "/image/work.zip/2.png", headers={"Range": "bytes=9-0"})
    assert status5 == 200 and raw5 == raw


def test_serve_nested_archive_entry(server):
    status, raw, headers = _get(server, "/image/aport_sub/nested.cbz/a.png")
    assert status == 200
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    assert headers.get("etag")


def test_serve_zip_entry_with_non_ascii_name(server, downloads_tree: Path, tmp_path: Path):
    """A Japanese inner entry name must not break the ETag header or delivery."""
    _make_png(tmp_path / "src.png")
    inner = "日本語フォルダ/表紙_日本語.png"
    with zipfile.ZipFile(downloads_tree / "aport_jp.zip", "w") as zf:
        zf.write(tmp_path / "src.png", inner)
    try:
        assert _post(server, "/api/v1/index/refresh") == 200
        url = "/image/" + quote("aport_jp.zip/" + inner)
        status, raw, headers = _get(server, url)
        assert status == 200, raw[:300]
        assert raw[:8] == b"\x89PNG\r\n\x1a\n"
        etag = headers.get("etag", "")
        assert etag
        etag.encode("ascii")  # must survive the header encode
        status2, _, _ = _get(server, url, headers={"If-None-Match": etag})
        assert status2 == 304
        status3, raw3, _ = _get(server, url, headers={"Range": "bytes=0-9"})
        assert status3 == 206
        assert raw3 == raw[:10]
        status4, raw4, _ = _get(server, "/thumb/" + quote("aport_jp.zip/" + inner) + "?w=200")
        assert status4 == 200
        assert raw4[:2] == b"\xff\xd8"
    finally:
        (downloads_tree / "aport_jp.zip").unlink(missing_ok=True)
        _post(server, "/api/v1/index/refresh")


def test_serve_pdf_page(server):
    status, raw, headers = _get(server, "/image/doc.pdf/p001.png")
    if status == 501:
        # No PDF renderer in this backend env (viewer-rs without libpdfium
        # fail-open proxies with no upstream here). Listing/tree above pin
        # the contract; a 500 here would still be a failure.
        return
    assert status == 200
    assert headers["content-type"] == "image/png"
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    status2, _, _ = _get(server, "/image/doc.pdf/p002.png")
    assert status2 in (200, 501)


def test_archive_errors_not_500(server):
    for path in ("work.zip/nope.png", "doc.pdf/p009.png", "work.zip/readme.txt"):
        assert _get(server, "/image/" + path)[0] == 404, path
    assert _get(server, "/image/work.zip/../secret")[0] == 404


def test_serve_rar_entry_and_errors(server, tmp_path: Path):
    status, raw, headers = _get(server, "/image/book.cbr/ch1/1.png")
    assert status == 200
    assert raw == _expected_png(tmp_path)
    assert headers.get("etag") and headers.get("accept-ranges") == "bytes"
    status2, _, _ = _get(server, "/image/book.cbr/ch1/1.png", headers={"If-None-Match": headers["etag"]})
    assert status2 == 304
    assert _get(server, "/image/book.cbr/nope.png")[0] == 404
    assert _get(server, "/image/book.cbr/readme.txt")[0] == 404
    assert _get(server, "/image/broken.rar/2.png")[0] == 404


def test_serve_epub_entry_percent_encoded(server, tmp_path: Path):
    status, raw, _ = _get(server, "/image/svg.epub/OEBPS/Images/page%201.png")
    assert status == 200
    assert raw == _expected_png(tmp_path)
    assert _get(server, "/image/manga.epub/OEBPS/Text/a.xhtml")[0] == 404


def test_thumb_from_rar_and_epub(server):
    status, raw, headers = _get(server, "/thumb/book.cbr/2.png?w=200")
    assert status == 200
    assert headers["content-type"].startswith("image/")
    assert raw[:2] == b"\xff\xd8"
    status, raw, headers = _get(server, "/thumb/manga.epub/OEBPS/Images/z_cover.png?w=200")
    assert status == 200
    assert headers["content-type"].startswith("image/")
    assert raw[:2] == b"\xff\xd8"
