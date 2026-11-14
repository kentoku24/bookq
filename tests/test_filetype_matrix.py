"""Filetype readability matrix — 14 types via the v1 contract (Phase 2c port, #487).

Covers all 14 file types the scanner knows (正: app/scanner.py:14 IMAGE_EXTENSIONS,
app/archive.py:20-22 ARCHIVE_EXTENSIONS, app/scanner.py:19 PDF_EXTENSIONS):

- images (8): .avif / .webp / .jpg / .jpeg / .png / .gif / .bmp / .svg
- archives (5): .zip / .cbz / .rar / .cbr / .epub
- pdf (1): .pdf

Each type x (v1 enumeration ``{folder, images}``, ``/image/*`` 200,
``/thumb/*`` 200) is asserted strictly per backend lane. The two lanes
disagree exactly where the backends genuinely differ:

- ``/thumb`` for genuine AVIF: Pillow decodes (py 200 JPEG) but the
  ``image`` crate cannot — rs proxies, and with no upstream that is 501.
- PDF pages need libpdfium: py renders them (200), rs proxies them
  (no-upstream 501) for both ``/image`` and ``/thumb``.
- RAR/CBR members read natively on both (bundled unrar + Pillow/``image``).

AVIF honesty rule: generated only via Pillow. If Pillow cannot encode AVIF in
this environment, the AVIF cells are reported as UNMEASURED via
``pytest.skip`` with an explicit reason — never a renamed PNG forgery.

The matrix boots its own backend (rs_spawn, both lanes) over a synthetic
tree so the shared session ``server`` tree stays untouched.

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_filetype_matrix.py -q
    BOOKQ_TEST_SERVER=rs uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_filetype_matrix.py -q
"""

import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import pytest

_RS = os.environ.get("BOOKQ_TEST_SERVER") == "rs"

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from PIL import Image  # noqa: E402

from tests.fixtures.rar5 import make_rar5_store  # noqa: E402

# ── genuine fixture builders (no extension forgery) ─────────────────────────

_PIL_FORMAT = {
    "avif": "AVIF",
    "webp": "WEBP",
    "jpg": "JPEG",
    "jpeg": "JPEG",
    "png": "PNG",
    "gif": "GIF",
    "bmp": "BMP",
}

_SVG_DOC = """<svg xmlns="http://www.w3.org/2000/svg" width="40" height="60">""" \
    """<rect width="40" height="60" fill="red"/></svg>"""


def _raster_bytes(fmt: str) -> bytes:
    """Encode a genuine 40x60 red raster via Pillow (raises if unsupported)."""
    buf = io.BytesIO()
    Image.new("RGB", (40, 60), (200, 60, 60)).save(buf, format=fmt)
    return buf.getvalue()


def _make_pdf(path: Path, pages: int = 1) -> None:
    """Minimal multi-page PDF (same shape as tests/test_archive.py::_make_pdf)."""
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


def _make_epub(path: Path, png: bytes) -> None:
    """Minimal valid EPUB with a single spine page (cf. test_archive._make_epub)."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?>'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        zf.writestr(
            "OEBPS/Text/p1.xhtml",
            '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
            '<head><title>p</title></head><body>'
            '<img src="../Images/p01.png" alt=""/></body></html>',
        )
        zf.writestr("OEBPS/Images/p01.png", png)
        zf.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="u">'
            '<metadata/><manifest>'
            '<item id="pg0" href="Text/p1.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="im0" href="Images/p01.png" media-type="image/png"/>'
            '</manifest><spine><itemref idref="pg0"/></spine></package>',
        )


# ── matrix definition ───────────────────────────────────────────────────────
# (filetype, kind, folder, inner): folder is the v1 images folder param,
# inner is the single expected entry served under /image/<folder>/<inner>.

MATRIX: list[tuple[str, str, str, str]] = [
    ("avif", "image", "mtx_avif", "p.avif"),
    ("webp", "image", "mtx_webp", "p.webp"),
    ("jpg", "image", "mtx_jpg", "p.jpg"),
    ("jpeg", "image", "mtx_jpeg", "p.jpeg"),
    ("png", "image", "mtx_png", "p.png"),
    ("gif", "image", "mtx_gif", "p.gif"),
    ("bmp", "image", "mtx_bmp", "p.bmp"),
    ("svg", "image", "mtx_svg", "p.svg"),
    ("zip", "archive", "mtx_book.zip", "p01.png"),
    ("cbz", "archive", "mtx_book.cbz", "p01.png"),
    ("rar", "archive", "mtx_book.rar", "p01.png"),
    ("cbr", "archive", "mtx_book.cbr", "p01.png"),
    ("epub", "archive", "mtx_book.epub", "OEBPS/Images/p01.png"),
    ("pdf", "pdf", "mtx_doc.pdf", "p001.png"),
]

#: filetypes Pillow could not encode in this env → cells reported UNMEASURED.
UNMEASURABLE: set[str] = set()
UNMEASURABLE_REASON: dict[str, str] = {}


def _get(server: str, path: str):
    req = urllib.request.Request(server + path)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return (
                resp.status,
                {k.lower(): v for k, v in resp.headers.items()},
                resp.read(),
            )
    except urllib.error.HTTPError as e:
        return (
            e.code,
            {k.lower(): v for k, v in (e.headers or {}).items()},
            e.read(),
        )


@pytest.fixture(scope="module")
def matrix_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("matrix")
    png = _raster_bytes("PNG")
    for ext, kind, folder, inner in MATRIX:
        if kind != "image":
            continue
        d = root / folder
        d.mkdir()
        if ext == "svg":
            (d / inner).write_text(_SVG_DOC, encoding="utf-8")
            continue
        try:
            (d / inner).write_bytes(_raster_bytes(_PIL_FORMAT[ext]))
        except Exception as exc:  # env lacks the encoder (e.g. AVIF build)
            UNMEASURABLE.add(ext)
            UNMEASURABLE_REASON[ext] = (
                f"Pillow cannot encode .{ext} in this env ({exc!r}); "
                "cell UNMEASURED — no renamed-PNG forgery used"
            )
    for name in ("mtx_book.zip", "mtx_book.cbz"):
        with zipfile.ZipFile(root / name, "w") as zf:
            zf.writestr("p01.png", png)
    for name in ("mtx_book.rar", "mtx_book.cbr"):
        make_rar5_store(root / name, [("p01.png", png)])
    _make_epub(root / "mtx_book.epub", png)
    _make_pdf(root / "mtx_doc.pdf", pages=1)
    return root


@pytest.fixture(scope="module")
def matrix_server(matrix_tree: Path):
    """Dedicated backend over the matrix tree (both lanes via rs_spawn)."""
    from rs_spawn import spawn_backend

    proc, base_url = spawn_backend(
        downloads_dir=matrix_tree, state_dir=os.environ["BOOKQ_STATE_DIR"]
    )
    try:
        yield base_url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _skip_if_unmeasurable(filetype: str) -> None:
    if filetype in UNMEASURABLE:
        pytest.skip(UNMEASURABLE_REASON[filetype])


def _v1_images(server: str, folder: str):
    status, _, raw = _get(
        server, "/api/v1/images?folder=" + urllib.parse.quote(folder, safe="")
    )
    body = json.loads(raw.decode("utf-8")) if raw else None
    return status, body


# ── the 3 cells × 14 types ──────────────────────────────────────────────────


@pytest.mark.parametrize("filetype,kind,folder,inner", MATRIX, ids=[m[0] for m in MATRIX])
def test_matrix_enumeration(matrix_server, filetype, kind, folder, inner):
    """Cell 1: v1 enumeration ``{folder, images}`` lists exactly the expected entry."""
    _skip_if_unmeasurable(filetype)
    status, body = _v1_images(matrix_server, folder)
    assert status == 200
    assert body == {"folder": folder, "images": [inner]}


@pytest.mark.parametrize("filetype,kind,folder,inner", MATRIX, ids=[m[0] for m in MATRIX])
def test_matrix_image(matrix_server, filetype, kind, folder, inner):
    """Cell 2: /image/<folder>/<inner> serves the page.

    PDF pages need libpdfium: py renders them, rs (no-upstream Proxy) 501s.
    """
    _skip_if_unmeasurable(filetype)
    path = "/image/{}/{}".format(
        urllib.parse.quote(folder, safe=""),
        "/".join(urllib.parse.quote(p, safe="") for p in inner.split("/")),
    )
    status, headers, raw = _get(matrix_server, path)
    if _RS and filetype == "pdf":
        assert status == 501, path
        assert headers["content-type"] == "application/json"
        return
    assert status == 200, path
    assert headers["content-type"].startswith("image/")
    assert len(raw) > 0


@pytest.mark.parametrize("filetype,kind,folder,inner", MATRIX, ids=[m[0] for m in MATRIX])
def test_matrix_thumb(matrix_server, filetype, kind, folder, inner):
    """Cell 3: /thumb/<folder>/<inner>?w=200 renders a thumbnail.

    AVIF genuine-decode is Pillow-only (py 200, rs no-upstream Proxy 501);
    PDF pages likewise need libpdfium (py 200, rs 501).
    """
    _skip_if_unmeasurable(filetype)
    path = "/thumb/{}/{}?w=200".format(
        urllib.parse.quote(folder, safe=""),
        "/".join(urllib.parse.quote(p, safe="") for p in inner.split("/")),
    )
    status, headers, raw = _get(matrix_server, path)
    if _RS and filetype in ("avif", "pdf"):
        assert status == 501, path
        assert headers["content-type"] == "application/json"
        return
    assert status == 200, path
    assert len(raw) > 0
    assert headers.get("x-content-type-options") == "nosniff"
    if filetype == "svg":
        # SVG is served as an active document, so a top-level navigation to it
        # must land in an opaque origin. Pinned per-filetype because the sandbox
        # is applied on the SVG branch only — every other type is rasterized.
        assert headers["content-type"] == "image/svg+xml"
        assert headers.get("content-security-policy") == "default-src 'none'; sandbox"
    else:
        assert headers["content-type"].startswith("image/")
        assert "content-security-policy" not in headers
