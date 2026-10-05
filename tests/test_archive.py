"""Archive support — ZIP/CBZ/PDF as browsable works (Roadmap P1-8, #104).

Unit level (scan_tree / list_archive_entries / archive_pdf) plus API level
(/api/folders, /api/images, /image/<archive>/<entry>) via TestClient.

Run:
    BOOKQ_TEST_MODE=true DOWNLOADS_DIR=<tmp> uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_archive.py -q
"""

import os
import struct
import sys
import zipfile
import zlib
from pathlib import Path
from urllib.parse import quote

import pytest

os.environ["BOOKQ_TEST_MODE"] = "true"

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from app.archive import list_archive_entries  # noqa: E402
from app.archive_pdf import parse_pdf_page_name, pdf_page_count, render_pdf_page  # noqa: E402
from app.metadata import MetadataIndex  # noqa: E402
from app.routes import _resolve_archive  # noqa: E402
from app.scanner import scan_tree  # noqa: E402
from tests.fixtures.rar5 import make_rar5_store  # noqa: E402


def _require_rar_tool() -> None:
    """Gate for tests that force rarfile's external extractor (production path for compressed entries).

    Store-method entries are read in pure Python, so most RAR tests need no tool.
    rarfile 4.5's bsdtar backend returns corrupt data (its ``-f -- <archive>`` argv
    is mis-parsed), so bsdtar-only hosts skip instead of failing: ``brew install sevenzip``.
    """
    rarfile = pytest.importorskip("rarfile")
    try:
        setup = rarfile.tool_setup()
    except rarfile.RarCannotExec as exc:  # pragma: no cover - env dependent
        pytest.skip(f"no RAR extractor available: {exc}")
    if setup.setup["open_cmd"][0] == "BSDTAR_TOOL":  # pragma: no cover - env dependent
        pytest.skip("rarfile's bsdtar backend is broken in 4.5; install 7-Zip (brew install sevenzip)")


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
    """Write a minimal multi-page PDF (content stream per page)."""
    objs: list[str] = ["1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"]
    kids = " ".join(f"{3 + i} 0 R" for i in range(pages))
    objs.append(f"2 0 obj\n<< /Type /Pages /Kids [{kids}] /Count {pages} >>\nendobj\n")
    for i in range(pages):
        content = f"BT 10 90 Td (Page {i + 1}) Tj ET"
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


def _make_epub(
    path: Path,
    pages: list[tuple[str, str]],
    png: bytes,
    *,
    svg: bool = False,
    broken: bool = False,
) -> None:
    """Write a fixed-layout EPUB. *pages* is ``[(xhtml_name, image_name)]`` in spine order.

    XHTML lives in ``OEBPS/Text/``, images in ``OEBPS/Images/`` so every ``src``
    is a ``../`` relative path. Image names are URL-encoded in the markup so
    ``%20`` decoding is exercised. ``svg=True`` wraps the image in an SVG
    ``<image xlink:href>`` (the common fixed-layout manga pattern).
    ``broken=True`` omits container.xml and the OPF so the reader must fall
    back to natural order.
    """
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


@pytest.fixture
def archive_tree(tmp_path: Path) -> Path:
    for name in ["10.png", "2.png", "1.png"]:
        _make_png(tmp_path / f"src_{name}")
    with zipfile.ZipFile(tmp_path / "work.zip", "w") as zf:
        zf.write(tmp_path / "src_10.png", "10.png")
        zf.write(tmp_path / "src_2.png", "2.png")
        zf.write(tmp_path / "src_1.png", "ch1/1.png")
        zf.writestr("../evil.png", "x")
        zf.writestr("/abs.png", "x")
        zf.writestr("readme.txt", "hi")
    with zipfile.ZipFile(tmp_path / "empty.zip", "w") as zf:
        zf.writestr("readme.txt", "no images here")
    with zipfile.ZipFile(tmp_path / "book.cbz", "w") as zf:
        zf.write(tmp_path / "src_1.png", "p01.png")
        zf.write(tmp_path / "src_2.png", "p02.png")
    _make_pdf(tmp_path / "doc.pdf", pages=2)
    make_rar5_store(
        tmp_path / "book.cbr",
        [
            ("10.png", (tmp_path / "src_10.png").read_bytes()),
            ("2.png", (tmp_path / "src_2.png").read_bytes()),
            ("ch1/1.png", (tmp_path / "src_1.png").read_bytes()),
            ("../evil.png", b"x"),
            ("readme.txt", b"hi"),
        ],
    )
    (tmp_path / "broken.rar").write_bytes(b"Rar!\x1a\x07\x01\x00garbage")
    png = (tmp_path / "src_1.png").read_bytes()
    # Spine order (cover, p1, p2) deliberately disagrees with filename order.
    _make_epub(
        tmp_path / "manga.epub",
        [("c.xhtml", "z_cover.png"), ("b.xhtml", "a_p1.png"), ("a.xhtml", "m_p2.png")],
        png,
    )
    _make_epub(
        tmp_path / "svg.epub",
        [("p1.xhtml", "page 1.png"), ("p2.xhtml", "page 2.png")],
        png,
        svg=True,
    )
    _make_epub(
        tmp_path / "broken.epub",
        [("c.xhtml", "z_cover.png"), ("b.xhtml", "a_p1.png")],
        png,
        broken=True,
    )
    simple = tmp_path / "simple"
    simple.mkdir()
    _make_png(simple / "1.png")
    sub = tmp_path / "sub"
    sub.mkdir()
    with zipfile.ZipFile(sub / "nested.cbz", "w") as zf:
        zf.write(tmp_path / "src_1.png", "a.png")
        zf.write(tmp_path / "src_2.png", "b.png")
    return tmp_path


# ── scanner unit ──────────────────────────────────────────────────────────


def test_list_archive_entries_sorted_and_safe(archive_tree: Path):
    entries = list_archive_entries(archive_tree / "work.zip")
    assert entries == ["2.png", "10.png", "ch1/1.png"]
    assert "readme.txt" not in entries


def test_list_archive_entries_empty(archive_tree: Path):
    assert list_archive_entries(archive_tree / "empty.zip") == []


def test_scan_tree_lists_archives_as_works(archive_tree: Path):
    tree = scan_tree(archive_tree)
    by_name = {n["name"]: n for n in tree}
    for fname in ("work.zip", "book.cbz", "doc.pdf", "empty.zip"):
        assert fname in by_name, f"{fname} missing from {sorted(by_name)}"
        assert by_name[fname]["type"] == "folder"
    assert by_name["work.zip"]["thumb"] is not None
    assert by_name["book.cbz"]["thumb"] == "p01.png"
    assert by_name["doc.pdf"]["thumb"] == "p001.png"
    assert by_name["doc.pdf"]["children"] is not None
    assert [c["name"] for c in by_name["doc.pdf"]["children"]] == ["p001.png", "p002.png"]
    # Empty archive: no crash, empty children, no thumb (empty message downstream).
    assert by_name["empty.zip"]["children"] == []
    assert by_name["empty.zip"]["thumb"] is None
    # Regression: plain image folder untouched.
    assert by_name["simple"]["thumb"] == "1.png"


def test_pdf_helpers(archive_tree: Path):
    pdf = archive_tree / "doc.pdf"
    assert pdf_page_count(pdf) == 2
    data = render_pdf_page(pdf, 0)
    assert data is not None and data[:8] == b"\x89PNG\r\n\x1a\n"
    assert render_pdf_page(pdf, 5) is None
    assert parse_pdf_page_name("p001.png", 2) == 0
    assert parse_pdf_page_name("p003.png", 2) is None
    assert parse_pdf_page_name("cover.png", 2) is None


def test_scan_tree_nested_archive_and_parent_thumb(archive_tree: Path):
    tree = scan_tree(archive_tree)
    by_name = {n["name"]: n for n in tree}
    sub = by_name["sub"]
    nested = next(c for c in sub["children"] if c["name"] == "nested.cbz")
    assert nested["thumb"] == "a.png"
    assert sub["thumb"] == "nested.cbz/a.png"


def test_resolve_archive(archive_tree: Path):
    archive, inner = _resolve_archive(archive_tree, "work.zip/2.png") or (None, None)
    assert archive is not None and archive.name == "work.zip" and inner == "2.png"
    assert _resolve_archive(archive_tree, "simple/1.png") is None
    assert _resolve_archive(archive_tree, "work.zip/../../secret") is None


def test_read_archive_entry_zip(archive_tree: Path):
    from app.archive import read_archive_entry

    data = read_archive_entry(archive_tree / "work.zip", "2.png")
    assert data is not None and data[:8] == b"\x89PNG\r\n\x1a\n"
    assert read_archive_entry(archive_tree / "work.zip", "nope.png") is None
    assert read_archive_entry(archive_tree / "work.zip", "readme.txt") is None
    assert read_archive_entry(archive_tree / "work.zip", "../evil.png") is None
    assert read_archive_entry(archive_tree / "missing.zip", "1.png") is None


def test_list_rar_entries_sorted_and_safe(archive_tree: Path):
    entries = list_archive_entries(archive_tree / "book.cbr")
    assert entries == ["2.png", "10.png", "ch1/1.png"]


def test_list_rar_entries_broken_is_empty(archive_tree: Path):
    assert list_archive_entries(archive_tree / "broken.rar") == []


def test_read_rar_entry(archive_tree: Path):
    from app.archive import read_archive_entry

    data = read_archive_entry(archive_tree / "book.cbr", "2.png")
    assert data == (archive_tree / "src_2.png").read_bytes()
    assert read_archive_entry(archive_tree / "book.cbr", "nope.png") is None
    assert read_archive_entry(archive_tree / "book.cbr", "readme.txt") is None
    assert read_archive_entry(archive_tree / "broken.rar", "2.png") is None


def test_read_rar_entry_via_external_tool(archive_tree: Path, monkeypatch):
    """Force rarfile to shell out (the production path for compressed entries)."""
    _require_rar_tool()
    import rarfile

    from app.archive import read_archive_entry

    monkeypatch.setattr(rarfile, "FORCE_TOOL", True)
    data = read_archive_entry(archive_tree / "book.cbr", "2.png")
    assert data == (archive_tree / "src_2.png").read_bytes()


def test_read_rar_entry_tool_missing_raises(archive_tree: Path, monkeypatch):
    rarfile = pytest.importorskip("rarfile")
    from app.archive import ArchiveToolMissing, read_archive_entry

    def _boom(self, *_a, **_k):
        raise rarfile.RarCannotExec("no tool")

    monkeypatch.setattr(rarfile.RarFile, "read", _boom)
    with pytest.raises(ArchiveToolMissing):
        read_archive_entry(archive_tree / "book.cbr", "2.png")


def test_read_rar_extraction_failure_logs_warning(archive_tree: Path, monkeypatch, caplog):
    import logging

    rarfile = pytest.importorskip("rarfile")
    from app.archive import read_archive_entry

    def _boom(self, *_a, **_k):
        raise rarfile.BadRarFile("Failed the read enough data: req=365249 got=54")

    monkeypatch.setattr(rarfile.RarFile, "read", _boom)
    with caplog.at_level(logging.WARNING, logger="app.archive"):
        assert read_archive_entry(archive_tree / "book.cbr", "2.png") is None
    assert any(
        r.levelno >= logging.WARNING and "2.png" in r.getMessage() for r in caplog.records
    )


def test_scan_tree_lists_rar_as_work(archive_tree: Path):
    tree = scan_tree(archive_tree)
    by_name = {n["name"]: n for n in tree}
    assert by_name["book.cbr"]["type"] == "folder"
    assert by_name["book.cbr"]["thumb"] == "2.png"
    assert [c["name"] for c in by_name["book.cbr"]["children"]] == ["2.png", "10.png", "ch1"]
    ch1 = next(c for c in by_name["book.cbr"]["children"] if c["name"] == "ch1")
    assert ch1["type"] == "folder"
    assert [c["name"] for c in ch1["children"]] == ["1.png"]
    assert ch1["thumb"] == "1.png"
    assert "archive" not in ch1
    assert by_name["broken.rar"]["children"] == []
    assert by_name["broken.rar"]["thumb"] is None


def test_epub_entries_follow_spine_order(archive_tree: Path):
    entries = list_archive_entries(archive_tree / "manga.epub")
    assert entries == [
        "OEBPS/Images/z_cover.png",
        "OEBPS/Images/a_p1.png",
        "OEBPS/Images/m_p2.png",
    ]


def test_epub_entries_svg_wrapper_and_percent_encoding(archive_tree: Path):
    entries = list_archive_entries(archive_tree / "svg.epub")
    assert entries == ["OEBPS/Images/page 1.png", "OEBPS/Images/page 2.png"]


def test_epub_without_opf_falls_back_to_natural_order(archive_tree: Path):
    entries = list_archive_entries(archive_tree / "broken.epub")
    assert entries == ["OEBPS/Images/a_p1.png", "OEBPS/Images/z_cover.png"]


def test_read_epub_entry(archive_tree: Path):
    from app.archive import read_archive_entry

    data = read_archive_entry(archive_tree / "manga.epub", "OEBPS/Images/a_p1.png")
    assert data == (archive_tree / "src_1.png").read_bytes()
    assert read_archive_entry(archive_tree / "manga.epub", "OEBPS/Text/a.xhtml") is None


def test_scan_tree_lists_epub_with_cover_thumb(archive_tree: Path):
    tree = scan_tree(archive_tree)
    by_name = {n["name"]: n for n in tree}
    assert by_name["manga.epub"]["type"] == "folder"
    assert by_name["manga.epub"]["thumb"] == "OEBPS/Images/z_cover.png"
    assert len(by_name["manga.epub"]["children"]) == 3


def test_epub_skips_unreadable_page_document(tmp_path: Path):
    """One malformed XHTML in the spine is skipped; the other pages still follow spine order."""
    _make_png(tmp_path / "src.png")
    png = (tmp_path / "src.png").read_bytes()
    _make_epub(
        tmp_path / "partial.epub",
        [("c.xhtml", "z_cover.png"), ("b.xhtml", "a_p1.png"), ("a.xhtml", "m_p2.png")],
        png,
    )
    # Corrupt the middle page document in place (zip append with a duplicate
    # name is fine for this test: zipfile.read returns the last entry written).
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(tmp_path / "partial.epub", "a") as zf:
            zf.writestr("OEBPS/Text/b.xhtml", "<html><body><img src='../Images/a_p1.png'")
    entries = list_archive_entries(tmp_path / "partial.epub")
    assert entries == ["OEBPS/Images/z_cover.png", "OEBPS/Images/m_p2.png"]


def test_epub_spine_scan_stops_at_entry_cap(tmp_path: Path, monkeypatch):
    import app.archive as archive_mod

    _make_png(tmp_path / "src.png")
    png = (tmp_path / "src.png").read_bytes()
    pages = [(f"p{i}.xhtml", f"img{i}.png") for i in range(6)]
    _make_epub(tmp_path / "big.epub", pages, png)
    monkeypatch.setattr(archive_mod, "MAX_ARCHIVE_ENTRIES", 3)
    entries = list_archive_entries(tmp_path / "big.epub")
    assert entries == [f"OEBPS/Images/img{i}.png" for i in range(3)]


# ── API level ─────────────────────────────────────────────────────────────


@pytest.fixture
def client(archive_tree: Path, monkeypatch):
    monkeypatch.setenv("DOWNLOADS_DIR", str(archive_tree))
    from app.main import create_app

    from fastapi.testclient import TestClient

    return TestClient(create_app())


def test_api_folders_shows_archives(client):
    res = client.get("/api/folders")
    assert res.status_code == 200
    by_name = {n["name"]: n for n in res.json()}
    assert by_name["work.zip"]["thumb"] is not None
    assert by_name["doc.pdf"]["thumb"] == "p001.png"
    assert by_name["empty.zip"]["thumb"] is None


def test_api_images_archive_and_pdf(client):
    res = client.get("/api/images", params={"folder": "work.zip"})
    assert res.status_code == 200
    assert len(res.json()) == 3
    res = client.get("/api/images", params={"folder": "doc.pdf"})
    assert res.status_code == 200
    assert res.json() == ["p001.png", "p002.png"]
    res = client.get("/api/images", params={"folder": "empty.zip"})
    assert res.status_code == 200
    assert res.json() == []


def test_serve_zip_entry_and_etag(client):
    res = client.get("/image/work.zip/2.png")
    assert res.status_code == 200
    assert res.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert res.headers.get("ETag")
    assert res.headers.get("Cache-Control", "").startswith("public")
    assert res.headers.get("Accept-Ranges") == "bytes"
    assert res.headers.get("Last-Modified")
    res2 = client.get("/image/work.zip/2.png", headers={"If-None-Match": res.headers["ETag"]})
    assert res2.status_code == 304
    # If-Modified-Since (exact Last-Modified) also revalidates to 304.
    res3 = client.get(
        "/image/work.zip/2.png", headers={"If-Modified-Since": res.headers["Last-Modified"]}
    )
    assert res3.status_code == 304
    # Range: first 10 bytes as 206 with Content-Range.
    total = len(res.content)
    res4 = client.get("/image/work.zip/2.png", headers={"Range": "bytes=0-9"})
    assert res4.status_code == 206
    assert res4.headers["Content-Range"] == f"bytes 0-9/{total}"
    assert len(res4.content) == 10
    assert res4.content == res.content[:10]
    # Inverted range falls back to 200 full body (never empty 206).
    res5 = client.get("/image/work.zip/2.png", headers={"Range": "bytes=9-0"})
    assert res5.status_code == 200 and res5.content == res.content


def test_serve_nested_archive_entry(client):
    res = client.get("/image/sub/nested.cbz/a.png")
    assert res.status_code == 200
    assert res.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert res.headers.get("ETag")


def test_serve_zip_entry_with_non_ascii_name(tmp_path: Path, monkeypatch):
    """#222: a Japanese inner entry name must not 500 on the ETag header.

    Starlette encodes response headers as latin-1, so an ETag carrying the
    raw inner name raises UnicodeEncodeError -> HTTP 500.
    """
    _make_png(tmp_path / "src.png")
    inner = "日本語フォルダ/表紙_日本語.png"
    with zipfile.ZipFile(tmp_path / "jp.zip", "w") as zf:
        zf.write(tmp_path / "src.png", inner)
    monkeypatch.setenv("DOWNLOADS_DIR", str(tmp_path))
    from app.main import create_app

    from fastapi.testclient import TestClient

    jp_client = TestClient(create_app(), raise_server_exceptions=False)
    url = "/image/" + quote("jp.zip/" + inner)
    res = jp_client.get(url)
    assert res.status_code == 200, res.text[:300]
    assert res.content[:8] == b"\x89PNG\r\n\x1a\n"
    etag = res.headers.get("ETag", "")
    assert etag
    etag.encode("ascii")  # must survive Starlette's latin-1 header encode
    # Conditional revalidation still works with the ASCII-safe ETag.
    res2 = jp_client.get(url, headers={"If-None-Match": etag})
    assert res2.status_code == 304
    # Range requests still work.
    res3 = jp_client.get(url, headers={"Range": "bytes=0-9"})
    assert res3.status_code == 206
    assert res3.content == res.content[:10]
    # Thumb path serves the same entry too.
    res4 = jp_client.get("/thumb/" + quote("jp.zip/" + inner) + "?w=200")
    assert res4.status_code == 200


def test_metadata_indexes_archives_searchable(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    _make_png(plain / "1.png")
    with zipfile.ZipFile(tmp_path / "book.zip", "w") as zf:
        zf.writestr("1.png", b"fake-png-bytes")
    _make_pdf(tmp_path / "doc.pdf", pages=1)
    idx = MetadataIndex()
    idx.rebuild(tmp_path)
    paths = {c.folder_path for c in idx.comics}
    assert "book.zip" in paths and "doc.pdf" in paths and "plain" in paths
    # Empty query returns everything (archives included).
    assert {c["folder_path"] for c in idx.search(q="")} == paths


def test_serve_pdf_page_as_png(client):
    res = client.get("/image/doc.pdf/p001.png")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/png"
    assert res.content[:8] == b"\x89PNG\r\n\x1a\n"
    res = client.get("/image/doc.pdf/p002.png")
    assert res.status_code == 200


def test_archive_errors_not_500(client):
    assert client.get("/image/work.zip/nope.png").status_code == 404
    assert client.get("/image/doc.pdf/p009.png").status_code == 404
    assert client.get("/image/work.zip/readme.txt").status_code == 404
    r = client.get("/image/work.zip/../secret")
    assert r.status_code in (400, 404, 422)


def test_regular_folder_unaffected(client):
    res = client.get("/api/images", params={"folder": "simple"})
    assert res.status_code == 200
    assert res.json() == ["1.png"]
    res = client.get("/image/simple/1.png")
    assert res.status_code == 200


def test_api_folders_shows_rar_and_epub(client):
    by_name = {n["name"]: n for n in client.get("/api/folders").json()}
    assert by_name["book.cbr"]["thumb"] == "2.png"
    assert by_name["manga.epub"]["thumb"] == "OEBPS/Images/z_cover.png"
    assert by_name["broken.rar"]["thumb"] is None
    assert by_name["broken.epub"]["thumb"] == "OEBPS/Images/a_p1.png"


def test_api_images_rar_and_epub(client):
    res = client.get("/api/images", params={"folder": "book.cbr"})
    assert res.status_code == 200 and res.json() == ["2.png", "10.png", "ch1/1.png"]
    res = client.get("/api/images", params={"folder": "manga.epub"})
    assert res.status_code == 200
    assert res.json() == [
        "OEBPS/Images/z_cover.png",
        "OEBPS/Images/a_p1.png",
        "OEBPS/Images/m_p2.png",
    ]
    res = client.get("/api/images", params={"folder": "broken.rar"})
    assert res.status_code == 200 and res.json() == []


def test_serve_rar_entry_and_errors(client, archive_tree: Path):
    res = client.get("/image/book.cbr/ch1/1.png")
    assert res.status_code == 200
    assert res.content == (archive_tree / "src_1.png").read_bytes()
    assert res.headers.get("ETag") and res.headers.get("Accept-Ranges") == "bytes"
    res2 = client.get("/image/book.cbr/ch1/1.png", headers={"If-None-Match": res.headers["ETag"]})
    assert res2.status_code == 304
    assert client.get("/image/book.cbr/nope.png").status_code == 404
    assert client.get("/image/book.cbr/readme.txt").status_code == 404
    assert client.get("/image/broken.rar/2.png").status_code == 404


def test_serve_epub_entry_percent_encoded(client, archive_tree: Path):
    res = client.get("/image/svg.epub/OEBPS/Images/page%201.png")
    assert res.status_code == 200
    assert res.content == (archive_tree / "src_1.png").read_bytes()
    assert client.get("/image/manga.epub/OEBPS/Text/a.xhtml").status_code == 404


def test_serve_rar_entry_tool_missing_is_501(client, monkeypatch):
    rarfile = pytest.importorskip("rarfile")

    def _boom(self, *_a, **_k):
        raise rarfile.RarCannotExec("no tool")

    monkeypatch.setattr(rarfile.RarFile, "read", _boom)
    res = client.get("/image/book.cbr/2.png")
    assert res.status_code == 501
    assert "7-Zip" in res.json()["detail"]


def test_thumb_from_rar_and_epub(client):
    res = client.get("/thumb/book.cbr/2.png?w=200")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("image/")
    res = client.get("/thumb/manga.epub/OEBPS/Images/z_cover.png?w=200")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("image/")


def test_metadata_indexes_rar_and_epub(tmp_path: Path):
    make_rar5_store(tmp_path / "book.cbr", [("1.png", b"fake")])
    _make_epub(tmp_path / "m.epub", [("a.xhtml", "1.png")], b"fake")
    idx = MetadataIndex()
    idx.rebuild(tmp_path)
    paths = {c.folder_path for c in idx.comics}
    assert {"book.cbr", "m.epub"} <= paths


# ── #260: oversized archives stay cached as truncated ─────────────────────


def _make_big_zip(path: Path, count: int) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for i in range(count):
            zf.writestr(f"p{i:05d}.png", b"x")


def _isolate_cache(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("BOOKQ_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("DOWNLOADS_DIR", str(tmp_path / "downloads"))
    downloads = tmp_path / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    return downloads


def _counting_zipfile(monkeypatch):
    real = zipfile.ZipFile
    calls: list = []

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(zipfile, "ZipFile", spy)
    return calls


def test_oversized_archive_cached_truncated_and_served_identically(tmp_path: Path, monkeypatch):
    """#260: 2001 entries are stored capped with truncated=True; the
    restart-equivalent second lookup serves bytes identical to the live
    capped read with zero ZipFile re-opens."""
    import app.archive as archive_mod
    from app import warmup_cache

    downloads = _isolate_cache(tmp_path, monkeypatch)
    zpath = downloads / "big.zip"
    _make_big_zip(zpath, archive_mod.MAX_ARCHIVE_ENTRIES + 1)

    live = list_archive_entries(zpath)
    assert len(live) == archive_mod.MAX_ARCHIVE_ENTRIES

    key = warmup_cache.cache_key_for_path(zpath)
    assert warmup_cache.is_archive_truncated(key) is True

    calls = _counting_zipfile(monkeypatch)
    cached = list_archive_entries(zpath)
    assert cached == live
    assert len(calls) == 0, "oversized archive must answer from disk cache"


def test_oversized_archive_cap_never_exceeded(tmp_path: Path, monkeypatch):
    """#260: small-cap simulation — 8 entries with cap 5 serve exactly the
    first 5 in served order, flagged truncated, with zero re-opens."""
    import app.archive as archive_mod
    from app import warmup_cache

    downloads = _isolate_cache(tmp_path, monkeypatch)
    zpath = downloads / "capped.zip"
    _make_big_zip(zpath, 8)
    monkeypatch.setattr(archive_mod, "MAX_ARCHIVE_ENTRIES", 5)

    live = list_archive_entries(zpath)
    assert live == [f"p{i:05d}.png" for i in range(5)]

    key = warmup_cache.cache_key_for_path(zpath)
    assert warmup_cache.is_archive_truncated(key) is True

    calls = _counting_zipfile(monkeypatch)
    assert list_archive_entries(zpath) == live
    assert len(calls) == 0


def test_small_archive_cached_without_truncated_flag(tmp_path: Path, monkeypatch):
    """#260: below-cap listings keep truncated=False and stay cache hits."""
    from app import warmup_cache

    downloads = _isolate_cache(tmp_path, monkeypatch)
    zpath = downloads / "small.zip"
    _make_big_zip(zpath, 3)

    live = list_archive_entries(zpath)
    assert live == [f"p{i:05d}.png" for i in range(3)]

    key = warmup_cache.cache_key_for_path(zpath)
    assert warmup_cache.is_archive_truncated(key) is False

    calls = _counting_zipfile(monkeypatch)
    assert list_archive_entries(zpath) == live
    assert len(calls) == 0
