"""Reader-page normalization endpoint — GET /view/<path> (android-viewer-perf).

jpeg/png/gif pass through byte-identical; other Pillow-decodable formats
(webp/avif/…) are re-encoded once to JPEG q85 and disk-cached under view/.
Run:

    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_view_endpoint.py -q
"""

import io
import os
import zipfile
from pathlib import Path

import pytest

os.environ["BOOKQ_TEST_MODE"] = "true"

from PIL import Image  # noqa: E402


def _make_noise_png(path: Path, w: int = 800, h: int = 600) -> None:
    Image.frombytes("RGB", (w, h), os.urandom(w * h * 3)).save(path, format="PNG")


def _make_jpeg(path: Path, w: int = 640, h: int = 480) -> None:
    Image.frombytes("RGB", (w, h), os.urandom(w * h * 3)).save(path, format="JPEG", quality=90)


def _make_webp(path: Path, w: int = 900, h: int = 1280) -> None:
    im = Image.frombytes("RGB", (w, h), os.urandom(w * h * 3))
    im.save(path, format="WEBP", quality=80)


@pytest.fixture
def view_tree(tmp_path: Path, tmp_path_factory) -> Path:
    plain = tmp_path / "plain"
    plain.mkdir()
    _make_noise_png(plain / "big.png")
    _make_jpeg(plain / "photo.jpg")
    _make_webp(plain / "page.webp")
    with zipfile.ZipFile(tmp_path / "work.zip", "w") as zf:
        zf.write(plain / "page.webp", "2.webp")
        zf.write(plain / "photo.jpg", "3.jpg")
    outside = tmp_path_factory.mktemp("outside") / "secret.webp"
    _make_webp(outside, w=100, h=100)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "link.webp").symlink_to(outside)
    return tmp_path


@pytest.fixture
def view_cache(tmp_path: Path, monkeypatch) -> Path:
    cache = tmp_path / "viewcache"
    monkeypatch.setenv("BOOKQ_THUMB_CACHE_DIR", str(cache))
    return cache


@pytest.fixture
def client(view_tree: Path, view_cache: Path, monkeypatch):
    monkeypatch.setenv("DOWNLOADS_DIR", str(view_tree))
    from app.main import create_app

    from fastapi.testclient import TestClient

    return TestClient(create_app())


def test_view_passthrough_png_identical_bytes(client, view_tree: Path):
    res = client.get("/view/plain/big.png")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/png"
    assert res.content == (view_tree / "plain" / "big.png").read_bytes()


def test_view_passthrough_jpeg_identical_bytes(client, view_tree: Path):
    res = client.get("/view/plain/photo.jpg")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"
    assert res.content == (view_tree / "plain" / "photo.jpg").read_bytes()


def test_view_webp_normalized_to_jpeg(client):
    res = client.get("/view/plain/page.webp")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"
    with Image.open(io.BytesIO(res.content)) as im:
        assert im.format == "JPEG"
        assert im.size == (900, 1280)


def test_view_archive_webp_entry(client):
    res = client.get("/view/work.zip/2.webp")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"


def test_view_archive_jpeg_entry_passthrough(client):
    res = client.get("/view/work.zip/3.jpg")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"


def test_view_unknown_returns_404(client):
    assert client.get("/view/nope/missing.webp").status_code == 404
    assert client.get("/view/work.zip/nope.webp").status_code == 404


def test_view_escape_returns_403(client):
    assert client.get("/view/sub/link.webp").status_code == 403


def test_view_etag_304_and_disk_cache(client, view_cache: Path):
    res = client.get("/view/plain/page.webp")
    assert res.status_code == 200
    assert res.headers.get("ETag")
    assert res.headers.get("Cache-Control") == "private, max-age=86400"
    # Normalized output landed in the view/ namespace.
    assert list((view_cache / "view").glob("*.jpg"))
    res2 = client.get("/view/plain/page.webp", headers={"If-None-Match": res.headers["ETag"]})
    assert res2.status_code == 304


def test_view_avif_normalized_to_jpeg(client, view_tree: Path):
    try:
        Image.new("RGB", (64, 64)).save(view_tree / "plain" / "page.avif", format="AVIF")
    except Exception:
        pytest.skip("Pillow AVIF encoder unavailable in this environment")
    res = client.get("/view/plain/page.avif")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"


def test_view_w_downsizes_passthrough_source(client):
    """?w= opts a jpeg source out of passthrough into the normalized
    branch — this is the iOS flip-through fast tier."""
    res = client.get("/view/plain/photo.jpg?w=240")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"
    with Image.open(io.BytesIO(res.content)) as im:
        assert im.format == "JPEG"
        assert im.size == (240, 180)


def test_view_w_webp_normalized_at_width(client):
    res = client.get("/view/plain/page.webp?w=400")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"
    with Image.open(io.BytesIO(res.content)) as im:
        assert im.size[0] <= 400
        assert im.size[1] > im.size[0]  # portrait aspect preserved


def test_view_w_invalid_falls_back_to_default(client, view_tree: Path):
    # Non-numeric w is ignored → jpeg stays passthrough byte-identical.
    res = client.get("/view/plain/photo.jpg?w=abc")
    assert res.status_code == 200
    assert res.content == (view_tree / "plain" / "photo.jpg").read_bytes()


def test_view_w_clamps_to_bounds(client):
    # w=99999 clamps to VIEW_WIDTH_MAX — a 640px source can't grow, so
    # the rendition equals the source dimensions (thumbnail never
    # upscales) but is still re-encoded, not passthrough bytes.
    res = client.get("/view/plain/photo.jpg?w=99999")
    assert res.status_code == 200
    with Image.open(io.BytesIO(res.content)) as im:
        assert im.size == (640, 480)
    # w below the floor clamps up to 240.
    res2 = client.get("/view/plain/photo.jpg?w=1")
    with Image.open(io.BytesIO(res2.content)) as im:
        assert im.size == (240, 180)


def test_view_w_separate_cache_and_etag(client, view_cache: Path):
    full = client.get("/view/plain/page.webp")
    small = client.get("/view/plain/page.webp?w=200")
    assert full.headers["ETag"] != small.headers["ETag"]
    # Two renditions coexist in the view/ namespace.
    assert len(list((view_cache / "view").glob("*.jpg"))) >= 2
    # Conditional request honours the width-specific etag.
    res2 = client.get("/view/plain/page.webp?w=200",
                      headers={"If-None-Match": small.headers["ETag"]})
    assert res2.status_code == 304


def test_view_w_archive_entry(client):
    res = client.get("/view/work.zip/2.webp?w=300")
    assert res.status_code == 200
    with Image.open(io.BytesIO(res.content)) as im:
        assert im.size[0] <= 300
