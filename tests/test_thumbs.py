"""Resized thumbnail endpoint — GET /thumb/<path>?w= (Todo 3, top-page display speed).

Shrink-only JPEG delivery with disk cache; /image/* untouched.

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_thumbs.py -q
"""

import io
import os
import zipfile
from pathlib import Path

import pytest

os.environ["BOOKQ_TEST_MODE"] = "true"

from PIL import Image  # noqa: E402

from tests.helpers import make_pdf  # noqa: E402


def _make_noise_png(path: Path, w: int = 800, h: int = 600) -> None:
    """Write a large noisy PNG (incompressible → thumb bytes << original bytes)."""
    Image.frombytes("RGB", (w, h), os.urandom(w * h * 3)).save(path, format="PNG")


@pytest.fixture
def thumb_tree(tmp_path: Path, tmp_path_factory) -> Path:
    plain = tmp_path / "plain"
    plain.mkdir()
    _make_noise_png(plain / "big.png")
    with zipfile.ZipFile(tmp_path / "work.zip", "w") as zf:
        zf.write(plain / "big.png", "2.png")
        zf.writestr("readme.txt", "hi")
    make_pdf(tmp_path / "doc.pdf", pages=2)
    # Symlink escaping DOWNLOADS_DIR → 403 (guard resolves symlinks).
    outside = tmp_path_factory.mktemp("outside") / "secret.png"
    _make_noise_png(outside, w=100, h=100)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "link.png").symlink_to(outside)
    return tmp_path


@pytest.fixture
def thumb_cache(tmp_path: Path, monkeypatch) -> Path:
    cache = tmp_path / "thumbcache"
    monkeypatch.setenv("BOOKQ_THUMB_CACHE_DIR", str(cache))
    return cache


@pytest.fixture
def client(thumb_tree: Path, thumb_cache: Path, monkeypatch):
    monkeypatch.setenv("DOWNLOADS_DIR", str(thumb_tree))
    from app.main import create_app

    from fastapi.testclient import TestClient

    return TestClient(create_app())


def _jpeg_size(body: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(body)) as im:
        return im.size


@pytest.mark.parametrize(
    "w, expected", [("400", (400, 300)), ("800", (800, 600))]
)
def test_thumb_widths(client, w, expected):
    res = client.get(f"/thumb/plain/big.png?w={w}")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"
    assert _jpeg_size(res.content) == expected


def test_thumb_default_width_is_400(client):
    res = client.get("/thumb/plain/big.png")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"
    assert _jpeg_size(res.content) == (400, 300)


@pytest.mark.parametrize("w", ["abc", "0"])
def test_thumb_invalid_w_falls_back_to_400(client, w):
    res = client.get(f"/thumb/plain/big.png?w={w}")
    assert res.status_code == 200
    assert _jpeg_size(res.content) == (400, 300)


def test_thumb_unknown_returns_404(client):
    assert client.get("/thumb/nope/missing.png").status_code == 404
    assert client.get("/thumb/work.zip/nope.png").status_code == 404


def test_thumb_escape_returns_403(client):
    assert client.get("/thumb/sub/link.png").status_code == 403


def test_thumb_etag_304_and_cache_control(client, thumb_cache: Path):
    res = client.get("/thumb/plain/big.png?w=200")
    assert res.status_code == 200
    assert res.headers.get("ETag")
    assert res.headers.get("Cache-Control") == "private, max-age=86400"
    assert res.headers.get("Last-Modified")
    assert res.headers.get("X-Content-Type-Options") == "nosniff"
    # Only the SVG branch carries the sandbox CSP; a rasterized JPEG must not.
    assert "Content-Security-Policy" not in res.headers
    # Disk cache written on first generation (cache hit proof).
    assert list((thumb_cache / "thumbs" / "200").glob("*.jpg"))
    res2 = client.get("/thumb/plain/big.png?w=200", headers={"If-None-Match": res.headers["ETag"]})
    assert res2.status_code == 304


def test_thumb_archive_entry(client):
    res = client.get("/thumb/work.zip/2.png?w=200")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"


def test_thumb_pdf_page(client):
    pypdfium2 = pytest.importorskip("pypdfium2")
    assert pypdfium2 is not None
    res = client.get("/thumb/doc.pdf/p001.png?w=200")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"


# NOTE (test-prune-audit todo 2): test_thumb_smaller_than_original was deleted here.
# It asserted a urandom-noise Pillow size property (noisy PNG bytes > JPEG bytes),
# not app logic. Do not re-add as a regression test.


def test_thumb_cache_eviction_caps_dir(client, thumb_cache: Path, monkeypatch):
    """Prefilled thumbs/<w>/ beyond cap is pruned back under cap on next write.

    Cap is monkeypatched to 3 (prod N=5000 in app/thumbs.py, matching the
    warmup cache bound); 5 stale files are dropped directly, then one real
    thumb write must prune the dir back under cap with the newest surviving.
    """
    import time

    import app.thumbs as thumbs_mod

    monkeypatch.setattr(thumbs_mod, "THUMB_CACHE_MAX_FILES", 3)
    ns = thumb_cache / "thumbs" / "200"
    ns.mkdir(parents=True, exist_ok=True)
    now = time.time()
    stale_names = set()
    for i in range(5):
        name = f"stale-{i}.jpg"
        stale_names.add(name)
        p = ns / name
        p.write_bytes(b"stale")
        os.utime(p, (now - 1000 + i, now - 1000 + i))
    res = client.get("/thumb/plain/big.png?w=200")
    assert res.status_code == 200
    files = list(ns.glob("*.jpg"))
    assert len(files) <= 3
    newest = max(files, key=lambda p: p.stat().st_mtime)
    assert newest.name not in stale_names


def test_thumb_eviction_failure_is_silent(client, thumb_cache: Path, monkeypatch):
    """Eviction delete failures (unwritable dir) never break thumb serving.

    Path.unlink is forced to raise (as an unwritable cache dir would), the
    dir is prefilled beyond a monkeypatched cap so prune attempts deletes,
    and the thumb request must still serve 200.
    """
    from pathlib import Path as _Path

    import app.thumbs as thumbs_mod

    monkeypatch.setattr(thumbs_mod, "THUMB_CACHE_MAX_FILES", 3)

    def _boom(self, *args, **kwargs):
        raise OSError("unwritable dir")

    monkeypatch.setattr(_Path, "unlink", _boom)
    ns = thumb_cache / "thumbs" / "200"
    ns.mkdir(parents=True, exist_ok=True)
    for i in range(5):
        (ns / f"stale-{i}.jpg").write_bytes(b"stale")
    res = client.get("/thumb/plain/big.png?w=200")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"
