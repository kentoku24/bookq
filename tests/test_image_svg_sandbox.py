"""直開き SVG の sandbox 回帰テスト。/image/* の loose file と archive 内 SVG は
image/svg+xml を active document として返すため、直接ナビゲーション時に同一
オリジンで script が走らないよう CSP sandbox + nosniff を付ける (app/thumbs.py
と同一方針)。PNG など非 SVG には付けない。

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_image_svg_sandbox.py -q
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

_SVG_DOC = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="60">'
    '<rect width="40" height="60" fill="red"/></svg>'
)

_CSP = "default-src 'none'; sandbox"


def _make_png_bytes() -> bytes:
    """最小の単色 PNG を外部依存なしで生成 (tests/test_archive.py と同形)。"""

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
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


@pytest.fixture
def svg_tree(tmp_path: Path) -> Path:
    loose = tmp_path / "svg_loose"
    loose.mkdir()
    (loose / "x.svg").write_text(_SVG_DOC, encoding="utf-8")
    (loose / "1.png").write_bytes(_make_png_bytes())
    with zipfile.ZipFile(tmp_path / "svg_book.zip", "w") as zf:
        zf.writestr("inner.svg", _SVG_DOC)
        zf.writestr("inner.png", _make_png_bytes())
    return tmp_path


@pytest.fixture
def client(svg_tree: Path, monkeypatch):
    monkeypatch.setenv("DOWNLOADS_DIR", str(svg_tree))
    from app.main import create_app

    from fastapi.testclient import TestClient

    return TestClient(create_app())


def test_loose_svg_gets_sandbox_headers(client):
    """loose file の SVG は CSP sandbox + nosniff 付きで 200 を返すこと。"""
    res = client.get("/image/svg_loose/x.svg")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("image/svg+xml")
    assert res.headers.get("content-security-policy") == _CSP
    assert res.headers.get("x-content-type-options") == "nosniff"


def test_archive_svg_gets_sandbox_headers_200_and_206(client):
    """zip 内 SVG も 200 / Range 206 の両経路で同じ 2 ヘッダを返すこと。"""
    res = client.get("/image/svg_book.zip/inner.svg")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("image/svg+xml")
    assert res.headers.get("content-security-policy") == _CSP
    assert res.headers.get("x-content-type-options") == "nosniff"

    ranged = client.get("/image/svg_book.zip/inner.svg", headers={"Range": "bytes=0-10"})
    assert ranged.status_code == 206
    assert ranged.headers.get("content-security-policy") == _CSP
    assert ranged.headers.get("x-content-type-options") == "nosniff"


def test_png_has_no_csp_header(client):
    """非 SVG (PNG) には CSP を付けないこと — loose / archive 共に回帰防止。"""
    for path in ("/image/svg_loose/1.png", "/image/svg_book.zip/inner.png"):
        res = client.get(path)
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("image/png")
        assert "content-security-policy" not in res.headers
        assert "x-content-type-options" not in res.headers
