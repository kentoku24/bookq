"""Contact-sheet sprite endpoint — GET /api/thumb-sprite (T3 of thumb-serving-speedup).

One request opens the archive ONCE, thumbnails up to 9 entries to w=200
(JPEG q70, same make_thumbnail), and returns a horizontal contact sheet.
Per-entry failures paint a gray cell (fail-open); all-fail → 404;
non-archive non-directory folder → 400. ``folder`` may also be a
DIRECTORY: names resolve relative to it via the shared stat/load helpers
(archive children inside it work). ``rows=a,b,c`` composes a 2D sheet
matching the DOM multi-thumb grid (one img per tile); the sheet size
follows ``w`` via SPRITE_GRID_DIMS (200 → 240x360, 400 → 480x720,
800 → 960x1440).

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_thumb_sprite.py -q
"""

import io
import asyncio
import os
import sys
import threading
import zipfile
from pathlib import Path
from urllib.parse import quote

import pytest

os.environ["BOOKQ_TEST_MODE"] = "true"

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from PIL import Image  # noqa: E402

from tests.helpers import drive  # noqa: E402

GRAY = (128, 128, 128)


def _solid_png(path: Path, rgb: tuple, w: int = 400, h: int = 300) -> None:
    Image.new("RGB", (w, h), rgb).save(path, format="PNG")


@pytest.fixture
def sprite_tree(tmp_path: Path) -> Path:
    plain = tmp_path / "plain"
    plain.mkdir()
    _solid_png(plain / "big.png", (9, 9, 9))
    _solid_png(tmp_path / "r.png", (255, 0, 0))
    _solid_png(tmp_path / "g.png", (0, 255, 0))
    _solid_png(tmp_path / "b.png", (0, 0, 255))
    _solid_png(tmp_path / "comma,name.png", (255, 255, 0))
    _solid_png(tmp_path / "p.png", (255, 0, 255))
    _solid_png(tmp_path / "q.png", (0, 255, 255))
    _solid_png(tmp_path / "pq.png", (255, 255, 0))
    with zipfile.ZipFile(tmp_path / "work.zip", "w") as zf:
        for name in ["r.png", "g.png", "b.png", "comma,name.png", "p.png", "q.png"]:
            zf.write(tmp_path / name, name)
        # Commas are legal in zip entry names: single entry "p.png,q.png"
        # collides with the pair "p.png"+"q.png" under a ","-joined key.
        zf.write(tmp_path / "pq.png", "p.png,q.png")
    return tmp_path


@pytest.fixture
def sprite_cache(tmp_path: Path, monkeypatch) -> Path:
    cache = tmp_path / "thumbcache"
    monkeypatch.setenv("BOOKQ_THUMB_CACHE_DIR", str(cache))
    return cache


@pytest.fixture
def client(sprite_tree: Path, sprite_cache: Path, monkeypatch):
    monkeypatch.setenv("DOWNLOADS_DIR", str(sprite_tree))
    from app.main import create_app

    from fastapi.testclient import TestClient

    return TestClient(create_app())


def _size(body: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(body)) as im:
        return im.size


def _pixel(body: bytes, x: int, y: int) -> tuple:
    with Image.open(io.BytesIO(body)) as im:
        return im.convert("RGB").getpixel((x, y))


def _near(pixel: tuple, target: tuple, tol: int = 25) -> bool:
    return all(abs(a - b) <= tol for a, b in zip(pixel, target))


def test_sprite_three_entries_width_600(client) -> None:
    res = client.get("/api/thumb-sprite?folder=work.zip&names=r.png,g.png,b.png&w=200")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"
    assert _size(res.content) == (600, 150)


def test_sprite_opens_archive_exactly_once(client, monkeypatch) -> None:
    opens: list[str] = []
    real_zipfile = zipfile.ZipFile

    def counting(*args, **kwargs):
        opens.append(str(args[0]) if args else "")
        return real_zipfile(*args, **kwargs)

    # app/thumbs.py does `import zipfile` + `zipfile.ZipFile(archive)`.
    monkeypatch.setattr(zipfile, "ZipFile", counting)
    url = "/api/thumb-sprite?folder=work.zip&names=r.png,g.png,b.png&w=200"
    res = client.get(url)
    assert res.status_code == 200
    assert len(opens) == 1, f"sprite must open the archive once, got: {opens}"
    del opens[:]
    res2 = client.get(url)
    assert res2.status_code == 200
    assert res2.content == res.content
    assert len(opens) == 0, f"cached sprite must open zero archives: {opens}"


def test_sprite_non_archive_folder_is_400(client) -> None:
    # plain is a directory now (dir sprites are supported) — see dir tests.
    assert client.get("/api/thumb-sprite?folder=nope.zip&names=a.png&w=200").status_code == 400
    assert client.get("/api/thumb-sprite?folder=r.png&names=a.png&w=200").status_code == 400
    assert client.get("/api/thumb-sprite?folder=work.zip&w=200").status_code == 400


def test_sprite_directory_entries(client) -> None:
    res = client.get("/api/thumb-sprite?folder=plain&names=big.png&w=200")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"
    assert _size(res.content) == (200, 150)


def test_sprite_directory_nested_and_root_names(client) -> None:
    # Directory names are folder-relative paths; ../ escapes stay blocked.
    res = client.get("/api/thumb-sprite?folder=.&names=plain%2Fbig.png,r.png&w=200")
    assert res.status_code == 200
    assert _size(res.content) == (400, 150)
    res = client.get("/api/thumb-sprite?folder=plain&names=..%2Fbig.png&w=200")
    assert res.status_code == 404  # traversal name == all-fail
    res = client.get("/api/thumb-sprite?folder=plain&names=big.png,..%2Fr.png&w=200")
    assert res.status_code == 200
    assert _near(_pixel(res.content, 300, 75), GRAY)


def test_sprite_directory_grid_rows(client) -> None:
    res = client.get(
        "/api/thumb-sprite?folder=.&names=r.png,g.png,b.png,p.png&rows=2,2&w=400"
    )
    assert res.status_code == 200
    assert _size(res.content) == (480, 720)
    # cells: r top-left, g top-right, b bottom-left, p bottom-right
    assert _near(_pixel(res.content, 120, 180), (255, 0, 0))
    assert _near(_pixel(res.content, 360, 180), (0, 255, 0))
    assert _near(_pixel(res.content, 120, 540), (0, 0, 255))
    assert _near(_pixel(res.content, 360, 540), (255, 0, 255))


def test_sprite_grid_rows_w200_small_sheet(client) -> None:
    """w=200 → the low-dpr sheet class: 240x360, same 2:3 grid layout."""
    res = client.get(
        "/api/thumb-sprite?folder=.&names=r.png,g.png,b.png,p.png&rows=2,2&w=200"
    )
    assert res.status_code == 200
    assert _size(res.content) == (240, 360)
    assert _near(_pixel(res.content, 60, 90), (255, 0, 0))
    assert _near(_pixel(res.content, 180, 90), (0, 255, 0))
    assert _near(_pixel(res.content, 60, 270), (0, 0, 255))
    assert _near(_pixel(res.content, 180, 270), (255, 0, 255))


def test_sprite_archive_grid_rows(client) -> None:
    res = client.get(
        "/api/thumb-sprite?folder=work.zip&names=r.png,g.png,b.png,p.png&rows=2,2&w=400"
    )
    assert res.status_code == 200
    assert _size(res.content) == (480, 720)
    assert _near(_pixel(res.content, 360, 540), (255, 0, 255))


def test_sprite_grid_all_bad_is_404_and_invalid_rows_fall_back(client) -> None:
    assert (
        client.get("/api/thumb-sprite?folder=plain&names=nope.png&rows=1&w=200").status_code
        == 404
    )
    # invalid rows → horizontal strip fallback (existing behavior)
    res = client.get("/api/thumb-sprite?folder=work.zip&names=r.png,g.png&rows=x&w=200")
    assert res.status_code == 200
    assert _size(res.content) == (400, 150)


def test_sprite_one_bad_entry_gray_others_intact(client) -> None:
    res = client.get("/api/thumb-sprite?folder=work.zip&names=r.png,nope.png,b.png&w=200")
    assert res.status_code == 200
    assert _size(res.content) == (600, 150)
    assert _near(_pixel(res.content, 100, 75), (255, 0, 0))
    assert _near(_pixel(res.content, 300, 75), GRAY)
    assert _near(_pixel(res.content, 500, 75), (0, 0, 255))


def test_sprite_all_bad_entries_is_404(client) -> None:
    res = client.get("/api/thumb-sprite?folder=work.zip&names=nope.png,nada.png&w=200")
    assert res.status_code == 404


def test_sprite_traversal_entries_fail_open(client) -> None:
    # Traversal / absolute names are rejected per-entry (gray cell), never 500.
    res = client.get("/api/thumb-sprite?folder=work.zip&names=r.png,../evil.png,g.png&w=200")
    assert res.status_code == 200
    assert _near(_pixel(res.content, 300, 75), GRAY)
    res = client.get("/api/thumb-sprite?folder=work.zip&names=/etc/passwd&w=200")
    assert res.status_code == 404  # single bad entry == all-fail
    res = client.get("/api/thumb-sprite?folder=../..&names=r.png&w=200")
    assert res.status_code in (400, 403)


def test_sprite_caps_at_nine_entries(client) -> None:
    names = ",".join(["r.png", "g.png", "b.png"] * 4)  # 12 names
    res = client.get(f"/api/thumb-sprite?folder=work.zip&names={names}&w=200")
    assert res.status_code == 200
    assert _size(res.content) == (1800, 150)


def test_sprite_comma_in_entry_name(client) -> None:
    names = quote("comma,name.png", safe="") + ",r.png"
    res = client.get(f"/api/thumb-sprite?folder=work.zip&names={names}&w=200")
    assert res.status_code == 200
    assert _size(res.content) == (400, 150)
    assert _near(_pixel(res.content, 100, 75), (255, 255, 0))


def test_sprite_comma_collision_distinct_sheets(client) -> None:
    """Two entries [p.png, q.png] vs one entry literally named "p.png,q.png".

    Under a ","-joined cache key both requests map to the same key/etag,
    so the second request is served the first request's sheet (wrong).
    The key joins names with \\x00, so the sheets must differ — asserted
    on response BYTES (a 2-cell sheet vs a 1-cell sheet).
    """
    two = client.get("/api/thumb-sprite?folder=work.zip&names=p.png,q.png&w=200")
    assert two.status_code == 200
    assert _size(two.content) == (400, 150)
    one = client.get(
        f"/api/thumb-sprite?folder=work.zip&names={quote('p.png,q.png', safe='')}&w=200"
    )
    assert one.status_code == 200
    assert _size(one.content) == (200, 150)
    assert two.content != one.content


def test_stat_helpers_reject_null_byte_without_500(tmp_path: Path) -> None:
    """Null-byte paths map to 400/404, never a leaked ValueError (HTTP 500).

    Simulated at the unit level: HTTP servers/clients normalize or reject
    embedded null bytes before the app sees them, so the adversarial path is
    fed directly to the stat helpers. ``Path.stat()`` / ``Path.is_file()``
    raise ``ValueError`` (not ``OSError``) on embedded null bytes; both
    helpers must convert that to "not found" instead of letting it escape.
    """
    from fastapi import HTTPException

    from app.routes import _resolve_archive
    from app.thumbs import _stat_archive_source

    downloads = tmp_path / "downloads"
    downloads.mkdir()

    # _resolve_archive probes candidates with is_file(); a null byte must
    # be skipped like a missing file (None), not raise.
    assert _resolve_archive(downloads, "a\x00b/c.png") is None

    # _stat_archive_source stats the archive directly; a null byte must
    # become 404, not a leaked ValueError.
    try:
        _stat_archive_source(downloads / "a\x00b.zip", "1.png")
    except HTTPException as exc:
        assert exc.status_code == 404
    else:  # pragma: no cover - ValueError leaking would fail here first
        raise AssertionError("null-byte archive path must raise HTTPException")


def test_stat_siblings_reject_null_byte_without_500(tmp_path: Path) -> None:
    """Sibling stat sites map null-byte ValueError to 404, never 500.

    Scope extension of ``test_stat_helpers_reject_null_byte_without_500``:
    the same one-line widening applied to ``_load_archive_source``,
    ``_serve_archive_entry``, the sprite-endpoint archive ``stat()``, and
    the loose-file ``_stat_source`` / ``load_source_bytes`` path. Fed
    directly at unit level (HTTP servers/clients normalize embedded null
    bytes before the app sees them).
    """
    from fastapi import HTTPException

    from app.routes import _serve_archive_entry
    from app.thumbs import _load_archive_source, _stat_source, load_source_bytes

    downloads = tmp_path / "downloads"
    downloads.mkdir()

    # (1) loose-file _stat_source: early \x00 guard -> 404 (not 500).
    try:
        _stat_source(downloads, "a\x00b.png")
    except HTTPException as exc:
        assert exc.status_code == 404
    else:  # pragma: no cover
        raise AssertionError("null-byte loose path must raise HTTPException")

    # (2) loose-file load_source_bytes: same early guard -> 404 (not 500).
    try:
        drive(load_source_bytes(downloads, "a\x00b.png"))
    except HTTPException as exc:
        assert exc.status_code == 404
    else:  # pragma: no cover
        raise AssertionError("null-byte loose path must raise HTTPException")

    # (3) _load_archive_source archive stat() widening -> 404 (not 500).
    try:
        drive(_load_archive_source(downloads / "a\x00b.zip", "1.png"))
    except HTTPException as exc:
        assert exc.status_code == 404
    else:  # pragma: no cover
        raise AssertionError("null-byte archive path must raise HTTPException")

    # (4) _serve_archive_entry archive stat() widening -> 404 (not 500).
    # request is unused before the stat guard, so None suffices here.
    try:
        drive(_serve_archive_entry(downloads / "a\x00b.zip", "1.png", None))  # type: ignore[arg-type]
    except HTTPException as exc:
        assert exc.status_code == 404
    else:  # pragma: no cover
        raise AssertionError("null-byte archive path must raise HTTPException")
