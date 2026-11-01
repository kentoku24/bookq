"""Startup cover pre-warming (plan thumb-warmup-stale-serve Todo 3).

``warm_thumb_cache``/``warm_thumb_cache_multi`` prepare folder covers in
the thumb disk cache (fail-open: never raises) — one source read + one
draft()-assisted decode feeds every width (issue #240);
``_enumerate_cover_jobs`` lists the cover-path jobs for every folder's
single ``thumb``; ``_warm_covers`` runs those jobs right after startup
with bounded concurrency and stops after quiet passes.

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
from app.routes import (  # noqa: E402
    _COVER_WARM_WIDTHS,
    _count_opaque_nodes,
    _enumerate_cover_jobs,
    _warm_covers,
)
from app.thumbs import (  # noqa: E402
    _stat_source,
    _store_thumb,
    cache_path_for,
    cache_root,
    make_thumbnail,
    warm_thumb_cache,
    warm_thumb_cache_multi,
)

from tests.fixtures.rar5 import make_rar5_store  # noqa: E402
from tests.helpers import drive, make_pdf  # noqa: E402


def _make_noise_png(path: Path, w: int = 800, h: int = 600) -> bytes:
    """Write a large noisy PNG; return its bytes for archive reuse."""
    from PIL import Image

    data = Image.frombytes("RGB", (w, h), os.urandom(w * h * 3))
    data.save(path, format="PNG")
    return path.read_bytes()


def _make_tiny_png(path: Path, rgb=(200, 30, 30)) -> bytes:
    """Write a tiny solid PNG; return its bytes (fast fixture, no noise cost)."""
    from PIL import Image

    Image.new("RGB", (32, 24), rgb).save(path, format="PNG")
    return path.read_bytes()


def _build_warm_tree(root: Path) -> dict[str, str]:
    """Cover sources of every kind; return {kind: thumb-relative-path}."""
    (root / "plain").mkdir(parents=True, exist_ok=True)
    png_bytes = _make_noise_png(root / "plain" / "a.png")
    with zipfile.ZipFile(root / "work.zip", "w") as zf:
        zf.writestr("cover.png", png_bytes)
    with zipfile.ZipFile(root / "book.epub", "w") as zf:
        zf.writestr("OEBPS/Images/p01.png", png_bytes)
    make_rar5_store(root / "arch.rar", [("p01.png", png_bytes)])
    make_pdf(root / "doc.pdf", pages=2)
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
    # One job per cover path — every _COVER_WARM_WIDTHS size renders from
    # the same read + decode inside warm_thumb_cache_multi (issue #240).
    assert _enumerate_cover_jobs(tree) == [
        "wrap/inner/vol1/p1.png",
        "series/cov.png",
        "series/sub/a.png",
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

    assert drive(main())


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

    assert drive(main())


def _make_noise_jpeg(path: Path, w: int = 2000, h: int = 3600) -> bytes:
    """Write a large noisy JPEG; return its bytes.

    Sized so draft() can downscale-decode it (min(w//200, h//800) >= 2 and
    min(w//400, h//1600) >= 2 — libjpeg 1/2 scale for both warm widths).
    """
    from PIL import Image

    Image.frombytes("RGB", (w, h), os.urandom(w * h * 3)).save(
        path, format="JPEG", quality=90
    )
    return path.read_bytes()


def test_warm_thumb_cache_multi_decodes_once_for_all_widths(tmp_path, monkeypatch):
    """One warm job opens + decodes the source ONCE for every width (#240).

    The old (path, width) job pairs paid one open + one full decode per
    width; the aggregated job must produce both cache files from a single
    Image.open and a single real decode (later load() calls on the shared
    frame early-return, so counting ``self._im is None`` isolates real
    decodes — ``self.im`` is an asserting property, not a null check).
    """
    import PIL.Image
    from PIL import ImageFile

    monkeypatch.setenv("BOOKQ_THUMB_CACHE_DIR", str(tmp_path / "thumbcache"))
    downloads = tmp_path / "downloads"
    (downloads / "plain").mkdir(parents=True)
    _make_noise_jpeg(downloads / "plain" / "a.jpg")

    opens = []
    real_open = PIL.Image.open

    def _counting_open(fp, *args, **kwargs):
        opens.append(fp)
        return real_open(fp, *args, **kwargs)

    decodes = []
    real_load = ImageFile.ImageFile.load

    def _counting_load(self):
        if self._im is None:  # only the first load() actually decodes
            decodes.append(self)
        return real_load(self)

    monkeypatch.setattr(PIL.Image, "open", _counting_open)
    monkeypatch.setattr(ImageFile.ImageFile, "load", _counting_load)

    async def main():
        assert await warm_thumb_cache_multi(
            downloads, "plain/a.jpg", (200, 400)
        ) is True
        return True

    assert drive(main())
    assert len(opens) == 1, f"cover warm re-opened the source per width: {opens}"
    assert len(decodes) == 1, f"cover warm re-decoded the source per width: {decodes}"
    for w in (200, 400):
        dest = _cache_dest(downloads, "plain/a.jpg", w)
        assert dest.is_file(), f"missing warmed cover w={w}"
        with real_open(dest) as im:
            assert im.size[0] == w, f"warmed cover w={w} is {im.size}"


def test_warm_thumb_cache_multi_partial_hit_renders_only_missing(tmp_path, monkeypatch):
    """An already-cached width is touched, not re-decoded; the rest share one decode."""
    import PIL.Image
    from PIL import ImageFile

    monkeypatch.setenv("BOOKQ_THUMB_CACHE_DIR", str(tmp_path / "thumbcache"))
    downloads = tmp_path / "downloads"
    (downloads / "plain").mkdir(parents=True)
    _make_noise_jpeg(downloads / "plain" / "a.jpg")

    async def prewarm():
        assert await warm_thumb_cache(downloads, "plain/a.jpg", 200) is True
        return True

    assert drive(prewarm())
    body_200 = _cache_dest(downloads, "plain/a.jpg", 200).read_bytes()

    decodes = []
    real_load = ImageFile.ImageFile.load

    def _counting_load(self):
        if self._im is None:
            decodes.append(self)
        return real_load(self)

    monkeypatch.setattr(ImageFile.ImageFile, "load", _counting_load)

    async def main():
        assert await warm_thumb_cache_multi(
            downloads, "plain/a.jpg", (200, 400)
        ) is True
        return True

    assert drive(main())
    assert len(decodes) == 1, (
        f"one missing width must not re-decode the cached one: {len(decodes)}"
    )
    assert _cache_dest(downloads, "plain/a.jpg", 400).is_file()
    assert _cache_dest(downloads, "plain/a.jpg", 200).read_bytes() == body_200


def test_make_thumbnail_invokes_draft_for_jpeg(tmp_path, monkeypatch):
    """JPEG: draft("RGB", (w, w*4)) runs before load and shrinks the decode."""
    import io

    from PIL import Image, JpegImagePlugin

    calls = []
    real_draft = JpegImagePlugin.JpegImageFile.draft

    def _spy(self, mode, size):
        result = real_draft(self, mode, size)
        calls.append((mode, size, self.size))
        return result

    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft", _spy)
    data = _make_noise_jpeg(tmp_path / "big.jpg", w=1600, h=2400)
    thumb = make_thumbnail(data, 200)
    # Box (200, 800) → libjpeg decodes at 1/2 scale (800x1200); the thumb
    # itself still lands at the requested (200, 300).
    assert calls == [("RGB", (200, 800), (800, 1200))]
    with Image.open(io.BytesIO(thumb)) as im:
        assert im.size == (200, 300)


def test_make_thumbnail_draft_noop_for_webp_and_png(tmp_path, monkeypatch):
    """webp/png have no draft hook — the call must be a harmless no-op."""
    import io

    from PIL import Image, ImageFile

    calls = []
    real_draft = ImageFile.ImageFile.draft

    def _spy(self, mode, size):
        calls.append((self.format, mode, size))
        return real_draft(self, mode, size)

    monkeypatch.setattr(ImageFile.ImageFile, "draft", _spy)
    for fmt in ("PNG", "WEBP"):
        src = tmp_path / f"src.{fmt.lower()}"
        Image.frombytes("RGB", (900, 700), os.urandom(900 * 700 * 3)).save(
            src, format=fmt
        )
        thumb = make_thumbnail(src.read_bytes(), 200)
        with Image.open(io.BytesIO(thumb)) as im:
            assert im.size[0] == 200 and im.size[1] < 200
    assert calls == [
        ("PNG", "RGB", (200, 800)),
        ("WEBP", "RGB", (200, 800)),
    ]


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
    state = _cover_state(downloads, tree)

    async def main():
        started = time.monotonic()
        await _warm_covers(state)  # must return (quiet passes), not run forever
        return time.monotonic() - started

    elapsed = drive(main())
    assert elapsed < 60, f"_warm_covers took {elapsed:.1f}s (quiet passes expected)"
    for rel in jobs:
        for width in _COVER_WARM_WIDTHS:
            assert _cache_dest(downloads, rel, width).is_file(), (
                f"unwarmed: {rel} w={width}"
            )


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

    assert drive(main())


def test_count_opaque_nodes_only_empty_archive_folders():
    """Only archive-flagged folders with empty children count, at any depth."""
    tree = [
        {"name": "opaque.zip", "type": "folder", "archive": True, "children": []},
        {"name": "none-kids", "type": "folder", "archive": True, "children": None},
        {"name": "filled.zip", "type": "folder", "archive": True, "children": [
            {"name": "p1.png", "type": "image", "children": None},
        ]},
        {"name": "plain", "type": "folder", "children": []},
        {"name": "nested", "type": "folder", "children": [
            {"name": "deep.rar", "type": "folder", "archive": True, "children": []},
            {"name": "pic.png", "type": "image", "children": None},
        ]},
        {"name": "loose.png", "type": "image", "children": None},
    ]
    # opaque.zip + none-kids + nested/deep.rar; materialized + plain + images excluded.
    assert _count_opaque_nodes(tree) == 3
    assert _count_opaque_nodes([]) == 0


def _fast_warm_monkeypatch(monkeypatch):
    """Shrink warm-loop timing so termination tests run in milliseconds."""
    import app.routes as routes_mod

    monkeypatch.setattr(routes_mod, "_WARM_COVERS_PASS_SLEEP_SECONDS", 0.01)
    monkeypatch.setattr(routes_mod, "_WARM_COVERS_QUIET_PASSES", 2)
    return routes_mod


def test_warm_covers_returns_when_opaque_count_stable(tmp_path, monkeypatch):
    """A stuck opaque node (count never changes) must still terminate via quiet passes."""
    import app.thumbs as thumbs_mod

    _fast_warm_monkeypatch(monkeypatch)

    async def _noop(downloads_dir, path, widths):
        return True

    monkeypatch.setattr(thumbs_mod, "warm_thumb_cache_multi", _noop)
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    tree = [{"name": "arch", "type": "folder", "archive": True, "thumb": None, "children": []}]
    state = _cover_state(downloads, tree)

    async def main():
        started = time.monotonic()
        await _warm_covers(state)  # count stuck at 1; quiet passes must stop it
        return time.monotonic() - started

    elapsed = drive(main())
    assert elapsed < 10, f"_warm_covers spun instead of returning ({elapsed:.1f}s)"


def test_warm_covers_returns_when_no_opaque_nodes(tmp_path, monkeypatch):
    """Zero opaque nodes stops the loop on the first pass (no 20-minute spin)."""
    import app.thumbs as thumbs_mod

    _fast_warm_monkeypatch(monkeypatch)

    async def _noop(downloads_dir, path, widths):
        return True

    monkeypatch.setattr(thumbs_mod, "warm_thumb_cache_multi", _noop)
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    tree = [{"name": "plain", "type": "folder", "thumb": None, "children": []}]
    state = _cover_state(downloads, tree)

    async def main():
        started = time.monotonic()
        await _warm_covers(state)
        return time.monotonic() - started

    elapsed = drive(main())
    assert elapsed < 10, f"_warm_covers spun instead of returning ({elapsed:.1f}s)"


def test_warm_covers_idle_gate_parks_jobs_while_requests_fresh(tmp_path, monkeypatch):
    """A request inside the idle window must hold every warm job back.

    Same last_request_at contract as _background_fill: with a fresh stamp
    and a long idle window the pass parks before the first
    warm_thumb_cache_multi call — interactive use never competes with warm.
    """
    import app.routes as routes_mod
    import app.thumbs as thumbs_mod

    _fast_warm_monkeypatch(monkeypatch)
    monkeypatch.setattr(routes_mod, "_FILL_IDLE_SECONDS", 3600.0)
    calls = []

    async def _spy(downloads_dir, path, widths):
        calls.append((path, widths))
        return True

    monkeypatch.setattr(thumbs_mod, "warm_thumb_cache_multi", _spy)
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    tree = [{"name": "plain", "type": "folder", "thumb": "a.png", "children": []}]
    state = _cover_state(downloads, tree)
    state.last_request_at = time.time()  # a request just arrived

    async def main():
        task = asyncio.create_task(_warm_covers(state))
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return calls

    assert drive(main()) == [], (
        "warm jobs ran while a request was fresh inside the idle window"
    )


def test_warm_thumb_cache_pillow_runs_off_event_loop(tmp_path, monkeypatch):
    """Pillow decode/resize must run in a worker thread, never on the event loop."""
    import io

    import app.thumbs as thumbs_mod
    from PIL import Image

    monkeypatch.setenv("BOOKQ_THUMB_CACHE_DIR", str(tmp_path / "thumbcache"))
    downloads = tmp_path / "downloads"
    (downloads / "plain").mkdir(parents=True)
    _make_tiny_png(downloads / "plain" / "a.png")
    buf = io.BytesIO()
    Image.new("RGB", (16, 12), (10, 200, 90)).save(buf, format="PNG")
    tiny = buf.getvalue()
    seen = []

    def _spy(data, width):
        seen.append(threading.current_thread())
        return tiny

    monkeypatch.setattr(thumbs_mod, "make_thumbnail", _spy)

    async def main():
        assert await warm_thumb_cache(downloads, "plain/a.png", 200) is True
        return threading.current_thread()  # the loop thread, for comparison

    loop_thread = drive(main())
    assert len(seen) == 1, "Pillow path must run exactly once on a miss"
    assert seen[0] is not threading.main_thread()
    assert seen[0] is not loop_thread, "make_thumbnail ran on the event loop thread"


def test_warm_thumb_cache_hit_refreshes_mtime(tmp_path, monkeypatch):
    """A cache hit must touch the file so warmed covers survive mtime pruning."""
    monkeypatch.setenv("BOOKQ_THUMB_CACHE_DIR", str(tmp_path / "thumbcache"))
    downloads = tmp_path / "downloads"
    (downloads / "plain").mkdir(parents=True)
    _make_tiny_png(downloads / "plain" / "a.png")

    async def main():
        assert await warm_thumb_cache(downloads, "plain/a.png", 200) is True
        return True

    assert drive(main())
    dest = _cache_dest(downloads, "plain/a.png", 200)
    assert dest.is_file()
    old = time.time() - 100
    os.utime(dest, (old, old))  # simulate an aged entry the prune would evict first

    async def hit():
        assert await warm_thumb_cache(downloads, "plain/a.png", 200) is True
        return True

    assert drive(hit())
    assert dest.stat().st_mtime > old, "hit must advance the cache file mtime"


def test_warm_covers_prunes_once_per_width_per_pass(tmp_path, monkeypatch):
    """Warm stores skip per-write prune; the pass prunes each namespace once."""
    import app.thumbs as thumbs_mod

    _fast_warm_monkeypatch(monkeypatch)
    calls = []
    real_prune = thumbs_mod.prune_cache_dir

    def _counting(namespace_dir, cap=None):
        calls.append(namespace_dir)
        return real_prune(namespace_dir, cap)

    monkeypatch.setattr(thumbs_mod, "prune_cache_dir", _counting)
    monkeypatch.setenv("BOOKQ_THUMB_CACHE_DIR", str(tmp_path / "thumbcache"))
    downloads = tmp_path / "downloads"
    (downloads / "plain").mkdir(parents=True)
    _make_tiny_png(downloads / "plain" / "a.png")

    async def miss():
        assert await warm_thumb_cache(downloads, "plain/a.png", 200) is True
        return True

    assert drive(miss())
    assert calls == [], "warm stores must pass prune=False (no per-write prune)"

    tree = [{"name": "plain", "type": "folder", "thumb": "a.png", "children": []}]
    state = _cover_state(downloads, tree)

    async def warm():
        await _warm_covers(state)
        return True

    assert drive(warm())
    widths = list(_COVER_WARM_WIDTHS)
    assert len(calls) >= len(widths), f"each pass must prune every width: {calls}"
    assert len(calls) % len(widths) == 0, f"prune calls must batch per width: {calls}"


def test_store_thumb_concurrent_same_dest_never_corrupts(tmp_path):
    """Two racing stores to one dest: no raise, one whole payload wins, no tmp left."""
    import io

    from PIL import Image

    def _payload(rgb):
        buf = io.BytesIO()
        Image.new("RGB", (24, 18), rgb).save(buf, format="PNG")
        return buf.getvalue()

    first, second = _payload((220, 40, 40)), _payload((40, 40, 220))
    assert first != second
    dest = tmp_path / "ns" / "cover.jpg"
    barrier = threading.Barrier(2)
    errors = []

    def _worker(payload):
        try:
            barrier.wait(timeout=10)
            _store_thumb(dest, payload)
        except Exception as exc:  # surfaced below, never swallowed
            errors.append(exc)

    for _ in range(5):  # repeat to make the interleave likely, still milliseconds
        workers = [threading.Thread(target=_worker, args=(p,)) for p in (first, second)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        assert not errors, errors
        assert dest.read_bytes() in (first, second), "dest must hold exactly one whole payload"
    leftovers = [
        p for p in dest.parent.iterdir()
        if p.suffix == ".tmp" or p.name.startswith(f".{dest.name}.")
    ]
    assert leftovers == [], f"sibling tmp files must never linger: {leftovers}"
