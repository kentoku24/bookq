"""Startup cover pre-warming (plan thumb-warmup-stale-serve Todo 3).

``warm_thumb_cache`` prepares one folder cover at one width in the thumb
disk cache (fail-open: never raises); ``_enumerate_cover_jobs`` lists the
``(cover_path, width)`` jobs for every folder's single ``thumb`` at w=200
and w=400; ``_warm_covers`` runs those jobs right after startup with
bounded concurrency and stops after quiet passes.

One asyncio.run() per test in a helper thread (same constraint as
tests/test_ensure_index.py: Playwright leaves a running loop behind, and
module-level asyncio state must not be shared across loops).

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_cover_warmup.py -q
"""

import asyncio
import os
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

os.environ["BOOKQ_TEST_MODE"] = "true"

from app.config import Settings  # noqa: E402
from app.routes import _enumerate_cover_jobs, _warm_covers  # noqa: E402
from app.thumbs import _stat_source, cache_path_for, cache_root, warm_thumb_cache  # noqa: E402

from tests.fixtures.rar5 import make_rar5_store  # noqa: E402


def _drive(coro):
    """Run *coro* to completion on a fresh event loop in a helper thread."""
    outcome = {}

    def target():
        outcome["value"] = asyncio.run(coro)

    worker = threading.Thread(target=target)
    worker.start()
    worker.join()
    return outcome["value"]


def _make_noise_png(path: Path, w: int = 800, h: int = 600) -> bytes:
    """Write a large noisy PNG; return its bytes for archive reuse."""
    from PIL import Image

    data = Image.frombytes("RGB", (w, h), os.urandom(w * h * 3))
    data.save(path, format="PNG")
    return path.read_bytes()


def _make_pdf(path: Path, pages: int = 2) -> None:
    """Minimal multi-page PDF (same shape as tests/test_thumbs.py)."""
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


def _build_warm_tree(root: Path) -> dict[str, str]:
    """Cover sources of every kind; return {kind: thumb-relative-path}."""
    (root / "plain").mkdir(parents=True, exist_ok=True)
    png_bytes = _make_noise_png(root / "plain" / "a.png")
    with zipfile.ZipFile(root / "work.zip", "w") as zf:
        zf.writestr("cover.png", png_bytes)
    with zipfile.ZipFile(root / "book.epub", "w") as zf:
        zf.writestr("OEBPS/Images/p01.png", png_bytes)
    make_rar5_store(root / "arch.rar", [("p01.png", png_bytes)])
    _make_pdf(root / "doc.pdf", pages=2)
    return {
        "plain": "plain/a.png",
        "zip": "work.zip/cover.png",
        "epub": "book.epub/OEBPS/Images/p01.png",
        "rar": "arch.rar/p01.png",
        "pdf": "doc.pdf/p001.png",
    }


def _cache_dest(downloads: Path, rel: str, width: int) -> Path:
    """Disk-cache file ``warm_thumb_cache`` must produce for (*rel*, *width*)."""
    _, _, key = _stat_source(downloads, rel)
    return cache_path_for(cache_root(), width, key)


def test_enumerate_cover_jobs_skips_none_and_never_splits_names():
    """Flattened names containing '/' compose; thumb=None skipped; images ignored."""
    tree = [
        {"name": "wrap/inner/vol1", "type": "folder", "thumb": "p1.png",
         "children": [{"name": "p1.png", "type": "image", "children": None}]},
        {"name": "empty", "type": "folder", "thumb": None, "children": []},
        {"name": "loose.png", "type": "image", "children": None},
        {"name": "series", "type": "folder", "thumb": "cov.png", "children": [
            {"name": "sub", "type": "folder", "thumb": "a.png", "children": []},
        ]},
    ]
    assert _enumerate_cover_jobs(tree) == [
        ("wrap/inner/vol1/p1.png", 200),
        ("wrap/inner/vol1/p1.png", 400),
        ("series/cov.png", 200),
        ("series/cov.png", 400),
        ("series/sub/a.png", 200),
        ("series/sub/a.png", 400),
    ]
    assert _enumerate_cover_jobs([]) == []


def _warm_case(tmp_path, monkeypatch, kind, rel):
    monkeypatch.setenv("BOOKQ_THUMB_CACHE_DIR", str(tmp_path / "thumbcache"))
    downloads = tmp_path / "downloads"
    downloads.mkdir(exist_ok=True)
    paths = _build_warm_tree(downloads)

    async def main():
        # Miss warms the disk cache; hit returns True without rebuilding.
        assert await warm_thumb_cache(downloads, paths[kind], 200) is True
        dest = _cache_dest(downloads, paths[kind], 200)
        assert dest.is_file(), f"no disk cache written for {kind}"
        assert await warm_thumb_cache(downloads, paths[kind], 200) is True
        assert await warm_thumb_cache(downloads, paths[kind], 400) is True
        assert _cache_dest(downloads, paths[kind], 400).is_file()
        return True

    assert _drive(main())


def test_warm_thumb_cache_plain_image(tmp_path, monkeypatch):
    _warm_case(tmp_path, monkeypatch, "plain", "plain/a.png")


def test_warm_thumb_cache_zip_entry(tmp_path, monkeypatch):
    _warm_case(tmp_path, monkeypatch, "zip", "work.zip/cover.png")


def test_warm_thumb_cache_epub_entry(tmp_path, monkeypatch):
    _warm_case(tmp_path, monkeypatch, "epub", "book.epub/OEBPS/Images/p01.png")


def test_warm_thumb_cache_rar_entry(tmp_path, monkeypatch):
    _warm_case(tmp_path, monkeypatch, "rar", "arch.rar/p01.png")


def test_warm_thumb_cache_pdf_page(tmp_path, monkeypatch):
    import pytest

    pytest.importorskip("pypdfium2")
    _warm_case(tmp_path, monkeypatch, "pdf", "doc.pdf/p001.png")


def test_warm_thumb_cache_bogus_path_returns_false(tmp_path, monkeypatch):
    """Fail-open: a missing source never raises, just reports False."""
    monkeypatch.setenv("BOOKQ_THUMB_CACHE_DIR", str(tmp_path / "thumbcache"))
    downloads = tmp_path / "downloads"
    downloads.mkdir()

    async def main():
        assert await warm_thumb_cache(downloads, "nope/missing.png", 200) is False
        assert await warm_thumb_cache(downloads, "work.zip/nope.png", 400) is False
        return True

    assert _drive(main())


def _cover_state(downloads: Path, tree: list[dict]):
    from app.metadata import MetadataIndex

    return SimpleNamespace(
        settings=Settings(downloads_dir=str(downloads)),
        metadata_index_cache={
            "tree": tree, "index": MetadataIndex(),
            "timestamp": time.time(), "generation": 1,
        },
    )


def test_warm_covers_warms_every_single_thumb_and_terminates(tmp_path, monkeypatch):
    """Synthetic tree: every folder's thumb lands in cache at both widths."""
    import pytest

    pytest.importorskip("pypdfium2")
    monkeypatch.setenv("BOOKQ_THUMB_CACHE_DIR", str(tmp_path / "thumbcache"))
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    paths = _build_warm_tree(downloads)
    from app.scanner import scan_tree

    tree = scan_tree(downloads)
    assert tree, "fixture must scan to a non-empty tree"
    jobs = _enumerate_cover_jobs(tree)
    assert jobs, "fixture tree must yield cover jobs"
    assert {w for _, w in jobs} == {200, 400}
    state = _cover_state(downloads, tree)

    async def main():
        started = time.monotonic()
        await _warm_covers(state)  # must return (quiet passes), not run forever
        return time.monotonic() - started

    elapsed = _drive(main())
    assert elapsed < 60, f"_warm_covers took {elapsed:.1f}s (quiet passes expected)"
    for rel, width in jobs:
        assert _cache_dest(downloads, rel, width).is_file(), f"unwarmed: {rel} w={width}"


def test_warm_covers_never_raises_on_corrupt_sources(tmp_path, monkeypatch):
    """Corrupt bytes and bogus paths are swallowed per job (fail-open)."""
    monkeypatch.setenv("BOOKQ_THUMB_CACHE_DIR", str(tmp_path / "thumbcache"))
    downloads = tmp_path / "downloads"
    (downloads / "bad").mkdir(parents=True)
    (downloads / "bad" / "cover.png").write_bytes(b"not an image at all")
    tree = [
        {"name": "bad", "type": "folder", "thumb": "cover.png", "children": []},
        {"name": "ghost", "type": "folder", "thumb": "missing.png", "children": []},
    ]
    state = _cover_state(downloads, tree)

    async def main():
        await _warm_covers(state)
        return True

    assert _drive(main())
