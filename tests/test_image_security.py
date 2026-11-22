"""#62: `/image/` loose-file allowlist — image extensions only.

Archive entries were already restricted by `is_image_entry`; the plain
(loose) file branch served ANY library file with a guessed MIME, so an
`.html`/`.xml` in the library came back as a same-origin active
document. The fix unifies both branches on `IMAGE_EXTENSIONS`, checked
against the RESOLVED path (a `*.png` symlink to an `.html` target must
not launder the type).

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_image_security.py -q
"""

import io
import os
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest


def _get(server: str, path: str, headers: dict | None = None):
    req = urllib.request.Request(server + path, headers=headers or {})
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


def _png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(scope="module")
def sec_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("imgsec")
    plain = root / "plain"
    plain.mkdir()
    (plain / "ok.png").write_bytes(_png())
    (plain / "ok.JPG").write_bytes(_png())  # case-insensitive suffix
    (plain / "ok.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><rect width="4" height="4"/></svg>',
        encoding="utf-8",
    )
    # Active-content / non-image loose files — all must 404.
    (plain / "evil.html").write_text("<script>alert(1)</script>", encoding="utf-8")
    (plain / "page.xhtml").write_text("<html/>", encoding="utf-8")
    (plain / "feed.xml").write_text("<a/>", encoding="utf-8")
    (plain / "notes.txt").write_text("hi", encoding="utf-8")
    (plain / "noext").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    # Symlink laundering: request name is an image, resolved target is not.
    try:
        (plain / "link.png").symlink_to(plain / "evil.html")
    except OSError:
        pass  # filesystems without symlink support skip via test below
    # Archive members: image ok, active content rejected (pre-existing
    # is_image_entry policy — kept here as the unified-contract proof).
    with zipfile.ZipFile(root / "pics.zip", "w") as zf:
        zf.writestr("ok.png", _png())
        zf.writestr("evil.html", "<script>alert(1)</script>")
        zf.writestr("readme.txt", "hi")
    with zipfile.ZipFile(root / "scan.cbz", "w") as zf:
        zf.writestr("p1.jpg", _png())
        zf.writestr("page.xhtml", "<html/>")
    return root


@pytest.fixture(scope="module")
def sec_server(sec_tree: Path):
    from rs_spawn import spawn_backend

    proc, base_url = spawn_backend(
        library_dir=sec_tree, state_dir=os.environ["BOOKQ_STATE_DIR"]
    )
    try:
        yield base_url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# ── loose files: non-images rejected ─────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    ["evil.html", "page.xhtml", "feed.xml", "notes.txt", "noext"],
)
def test_loose_non_image_404(sec_server: str, name: str):
    status, headers, _ = _get(sec_server, f"/image/plain/{name}")
    assert status == 404
    assert headers["content-type"] == "application/json"


def test_loose_symlink_to_html_404(sec_server: str, sec_tree: Path):
    """`link.png` resolves to `evil.html` — the allowlist must inspect
    the resolved path, not the request suffix."""
    if not (sec_tree / "plain" / "link.png").is_symlink():
        pytest.skip("symlinks unsupported on this filesystem")
    status, _, _ = _get(sec_server, "/image/plain/link.png")
    assert status == 404


# ── loose files: images still served with security headers ───────────────


def test_loose_png_200_nosniff(sec_server: str):
    status, headers, body = _get(sec_server, "/image/plain/ok.png")
    assert status == 200
    assert headers["content-type"] == "image/png"
    assert headers.get("x-content-type-options") == "nosniff"
    assert body.startswith(b"\x89PNG")


def test_loose_jpg_uppercase_ext(sec_server: str):
    status, headers, _ = _get(sec_server, "/image/plain/ok.JPG")
    assert status == 200
    assert headers["content-type"] == "image/jpeg"


def test_loose_svg_sandbox_csp(sec_server: str):
    status, headers, _ = _get(sec_server, "/image/plain/ok.svg")
    assert status == 200
    assert headers["content-type"] == "image/svg+xml"
    assert headers.get("x-content-type-options") == "nosniff"
    assert headers.get("content-security-policy") == "default-src 'none'; sandbox"


def test_loose_png_range_206_nosniff(sec_server: str):
    status, headers, body = _get(
        sec_server, "/image/plain/ok.png", {"Range": "bytes=0-9"}
    )
    assert status == 206
    assert headers.get("x-content-type-options") == "nosniff"
    assert headers.get("content-range", "").startswith("bytes 0-9/")
    assert len(body) == 10


def test_loose_png_304_keeps_nosniff(sec_server: str):
    status, headers, _ = _get(sec_server, "/image/plain/ok.png")
    etag = headers.get("etag")
    assert etag
    status, headers, _ = _get(
        sec_server, "/image/plain/ok.png", {"If-None-Match": etag}
    )
    assert status == 304
    assert headers.get("x-content-type-options") == "nosniff"


# ── archive entries: same policy both formats ────────────────────────────


@pytest.mark.parametrize("archive", ["pics.zip", "scan.cbz"])
def test_archive_non_image_entries_404(sec_server: str, archive: str):
    for inner in ("evil.html", "readme.txt", "page.xhtml"):
        status, _, _ = _get(sec_server, f"/image/{archive}/{inner}")
        assert status == 404, f"{archive}/{inner}"


@pytest.mark.parametrize(
    "archive,inner",
    [("pics.zip", "ok.png"), ("scan.cbz", "p1.jpg")],
)
def test_archive_image_entries_200(sec_server: str, archive: str, inner: str):
    status, headers, _ = _get(sec_server, f"/image/{archive}/{inner}")
    assert status == 200
    assert headers.get("x-content-type-options") == "nosniff"
    assert headers["content-type"].startswith("image/")
