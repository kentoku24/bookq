"""Thumb cache-hit opens zero archives (failing-first, T1 of thumb-serving-speedup).

``handle_thumb`` (app/thumbs.py:154-189) calls ``load_source_bytes``
(archive open + single-entry extract) BEFORE the ``_get_or_make`` disk-cache
check, so even a cache HIT pays a full open+extract whose bytes are
discarded. This test pins the desired behavior: the second ``/thumb/``
request for the same entry served from disk cache must open zero archives
and return identical bytes. RED on current code (open-count > 0).

Driving route: ASGI app route via TestClient (same pattern as
tests/test_thumbs.py), NOT a direct ``handle_thumb`` call.

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_thumb_cache_hit_no_open.py -q
"""

import os
import sys
import zipfile
from pathlib import Path

os.environ["BOOKQ_TEST_MODE"] = "true"

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from PIL import Image  # noqa: E402


def _make_noise_png(path: Path, w: int = 400, h: int = 300) -> None:
    """Write a noisy PNG (same idea as tests/test_thumbs.py)."""
    Image.frombytes("RGB", (w, h), os.urandom(w * h * 3)).save(path, format="PNG")


def test_cache_hit_opens_zero_archives(tmp_path: Path, monkeypatch) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    _make_noise_png(downloads / "src.png")
    with zipfile.ZipFile(downloads / "work.zip", "w") as zf:
        zf.write(downloads / "src.png", "1.png")
        zf.write(downloads / "src.png", "2.png")

    cache = tmp_path / "thumbcache"
    monkeypatch.setenv("BOOKQ_THUMB_CACHE_DIR", str(cache))
    monkeypatch.setenv("DOWNLOADS_DIR", str(downloads))

    opens: list[str] = []
    real_zipfile = zipfile.ZipFile

    def counting(*args, **kwargs):
        opens.append(str(args[0]) if args else "")
        return real_zipfile(*args, **kwargs)

    # app/archive.py does `import zipfile` + `zipfile.ZipFile(path)`,
    # so patching the attribute on the zipfile module intercepts it.
    monkeypatch.setattr(zipfile, "ZipFile", counting)

    from app.main import create_app

    from fastapi.testclient import TestClient

    client = TestClient(create_app())
    url = "/thumb/work.zip/1.png?w=200"

    first = client.get(url)
    assert first.status_code == 200
    assert list((cache / "thumbs" / "200").glob("*.jpg")), "warm request must populate disk cache"

    del opens[:]
    second = client.get(url)
    assert second.status_code == 200
    assert second.content == first.content
    assert len(opens) == 0, f"cache hit opened archives: {opens}"
