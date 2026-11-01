"""Resized JPEG thumbnails for the top page (Todo 3: top-page display speed).

``GET /thumb/{path}?w=`` serves a shrink-only JPEG rendered from the same
source ``/image/*`` serves (plain file, ZIP/CBZ entry, or PDF page) and
caches it on disk. ``/image/*`` behavior and scanner ``thumb`` values are
untouched — only the URL-building side switches to these endpoints.

Resolution, guards, and conditional semantics reuse the ``/image/*`` flow
(``_resolve_archive``, ``render_pdf_page``, ``_is_within``,
``_not_modified``); this module only adds resize + disk cache.
"""

import hashlib
import io
import os
import re
import tempfile
from email import utils
from pathlib import Path

from fastapi import HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

ALLOWED_WIDTHS = (200, 400, 800)
DEFAULT_WIDTH = 400
JPEG_QUALITY = 70
CACHE_MAX_AGE = 86400

# Disk-cache eviction cap (issue #265): EACH of ``thumbs/<w>/`` and
# ``sprites/<w>/`` keeps at most this many files. N=5000 matches the warmup
# cache's ``_MAX_DISK_ENTRIES`` (app/warmup_cache.py) so every on-disk cache
# in the app shares one bound; sprite keys scale with distinct user-supplied
# name sets, hence the shared bound matters there most. Eviction is lazy
# (prune back under cap right after storing a new file, oldest mtime first)
# and fail-open (any error is swallowed — eviction never breaks serving).
THUMB_CACHE_MAX_FILES = 5000

# Sprite (contact-sheet) bounds: one /api/thumb-sprite request renders at most
# this many cells, each SPRITE_CELL_WIDTH px wide. Failed cells are painted
# SPRITE_GRAY so a single bad entry never fails the whole sheet (fail-open).
SPRITE_MAX_ENTRIES = 9
SPRITE_CELL_WIDTH = 200
# 2D grid sheet (rows= param): the tile's 2:3 aspect. Sheet size follows the
# `w` class: w=200 → 240x360 (the ~128px sidebar tile at ~1.8x — an exact
# fit at the ~133px/dpr1.75 low-end Android tile), w=400 → 480x720 (the
# original fixed size, kept for dpr>=2), w=800 → 960x1440. Bigger sheets
# decode measurably slower on low-end Android (a 6-img hydration burst of
# 600x900 sheets produced ~240-330ms frames on Unisoc T620; 38 x 480x720
# sheets ≈ 52MB of decoded bitmaps thrashed the GPU image-decode cache
# into ~34ms frames on CP08-J1, vs ~22ms once the sheet bytes shrank ~4x).
SPRITE_GRID_DIMS = {200: (240, 360), 400: (480, 720), 800: (960, 1440)}
SPRITE_GRAY = (128, 128, 128)

# Syntactic half of ``parse_pdf_page_name`` (``p001.png`` shape WITHOUT the
# range check). The stat-only fast path uses this so it never opens the PDF
# document; the range check stays on the miss path in ``_load_archive_source``.
_PDF_PAGE_SHAPE = re.compile(r"p\d+\.png")


def normalize_width(raw: str | int | None) -> int:
    """Map *raw* to 200|400|800, defaulting to 400 for anything else."""
    try:
        width = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_WIDTH
    return width if width in ALLOWED_WIDTHS else DEFAULT_WIDTH


def cache_root() -> Path:
    """Writable dir for cached thumbs (overridable for tests)."""
    override = os.getenv("BOOKQ_THUMB_CACHE_DIR", "")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / "bookq-thumbs"


def cache_path_for(cache_dir: Path, width: int, key: str) -> Path:
    """``<cache_dir>/thumbs/<w>/<sha1(path+mTime+size)>.jpg``."""
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return cache_dir / "thumbs" / str(width) / f"{digest}.jpg"


def prune_cache_dir(namespace_dir: Path, cap: int | None = None) -> None:
    """Prune *namespace_dir* back under *cap* files, oldest mtime first.

    Shared by the ``thumbs/<w>/`` and ``sprites/<w>/`` namespaces (single
    pruner — no per-namespace copies). Fail-open: every error (unreadable
    dir, unwritable dir, racing deletes) is swallowed so eviction never
    breaks serving.
    """
    if cap is None:
        # Read at call time (never a default arg) so a monkeypatched
        # THUMB_CACHE_MAX_FILES takes effect immediately.
        cap = THUMB_CACHE_MAX_FILES
    try:
        entries = [p for p in namespace_dir.iterdir() if p.is_file()]
    except Exception:
        return
    if len(entries) <= cap:
        return
    try:
        entries.sort(key=lambda p: p.stat().st_mtime)
    except Exception:
        return
    for stale in entries[: len(entries) - cap]:
        try:
            stale.unlink()
        except Exception:
            pass


async def load_source_bytes(
    downloads_dir: Path, path: str
) -> tuple[bytes, float, float, str]:
    """Return (raw bytes, mtime, size, cache key) for *path*.

    Mirrors ``serve_image`` validation (400 invalid / 403 outside / 404
    missing) and resolves archive/PDF virtual paths through
    ``_resolve_archive`` + ``render_pdf_page`` — never plain ``open``.
    """
    from app.routes import _is_within, _resolve_archive

    resolved = _resolve_archive(downloads_dir, path)
    if resolved is not None:
        archive, inner = resolved
        if not _is_within(downloads_dir, archive):
            raise HTTPException(status_code=403, detail="Forbidden")
        return await _load_archive_source(archive.resolve(), inner)

    # Root-level loose files arrive as a bare filename (no folder segment);
    # let them flow into the guards below. Traversal stays blocked:
    # ".." resolves outside downloads_dir (-> 403 via _is_within).
    if path.startswith("/") or path.endswith("/"):
        raise HTTPException(status_code=400, detail="Invalid image path")
    if "\x00" in path:
        # Embedded null byte: resolve()/is_file()/stat() below raise
        # ValueError (not OSError) — map to 404 like any other stat
        # failure. One choke point covers all three calls.
        raise HTTPException(status_code=404, detail="File not found")
    file_path = downloads_dir / path
    if not _is_within(downloads_dir, file_path):
        raise HTTPException(status_code=403, detail="Forbidden")
    file_path = file_path.resolve()
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    def _read() -> bytes:
        return file_path.read_bytes()

    stat = file_path.stat()
    data = await run_in_threadpool(_read)
    key = f"{file_path}|{stat.st_mtime}|{stat.st_size}"
    return data, stat.st_mtime, float(stat.st_size), key


def _stat_source(downloads_dir: Path, path: str) -> tuple[float, float, str]:
    """Return (mtime, size, cache key) for *path* WITHOUT extracting bytes.

    Mirrors ``load_source_bytes`` validation (400 invalid / 403 outside /
    404 missing) and builds the byte-identical cache key, but performs only
    ``stat`` calls plus inner-name shape checks — no archive open, no entry
    extract, no PDF render. The caller checks the disk cache FIRST with this
    key and only calls ``load_source_bytes`` on a miss.

    PDF choice: ``parse_pdf_page_name`` needs a page count, and counting
    opens the document (``pdf_page_count`` → pypdfium2 / read+regex), which
    would defeat the fast path — so the stat path validates only the
    ``pNNN.png`` shape and leaves the range check (and 501 rendering check)
    to the miss path in ``_load_archive_source``. A bad page can never be a
    cache hit (it could never have been written), so hit behavior is
    unchanged. Extraction-derived 404s (corrupt entry) and
    ArchiveToolMissing→501 likewise surface only on the miss path.
    """
    from app.routes import _is_within, _resolve_archive

    resolved = _resolve_archive(downloads_dir, path)
    if resolved is not None:
        archive, inner = resolved
        if not _is_within(downloads_dir, archive):
            raise HTTPException(status_code=403, detail="Forbidden")
        return _stat_archive_source(archive.resolve(), inner)

    # Root-level loose files arrive as a bare filename (no folder segment);
    # let them flow into the guards below. Traversal stays blocked:
    # ".." resolves outside downloads_dir (-> 403 via _is_within).
    if path.startswith("/") or path.endswith("/"):
        raise HTTPException(status_code=400, detail="Invalid image path")
    if "\x00" in path:
        # Embedded null byte: resolve()/is_file()/stat() below raise
        # ValueError (not OSError) — map to 404 like any other stat
        # failure. One choke point covers all three calls.
        raise HTTPException(status_code=404, detail="File not found")
    file_path = downloads_dir / path
    if not _is_within(downloads_dir, file_path):
        raise HTTPException(status_code=403, detail="Forbidden")
    file_path = file_path.resolve()
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    stat = file_path.stat()
    key = f"{file_path}|{stat.st_mtime}|{stat.st_size}"
    return stat.st_mtime, float(stat.st_size), key


def _stat_archive_source(archive: Path, inner: str) -> tuple[float, float, str]:
    """Return (mtime, size, cache key) for one archive entry, stat-only."""
    from app.archive import _is_image_entry
    from app.scanner import is_pdf_file

    if not inner or inner.startswith("/") or ".." in inner.split("/"):
        raise HTTPException(status_code=400, detail="Invalid image path")
    try:
        stat = archive.stat()
    except (OSError, ValueError):
        # ValueError: embedded null byte makes stat() raise instead of
        # failing with OSError — map to 404 like any other stat failure.
        raise HTTPException(status_code=404, detail="File not found")

    if is_pdf_file(archive):
        if _PDF_PAGE_SHAPE.fullmatch(inner) is None:
            raise HTTPException(status_code=404, detail="File not found")
    elif not _is_image_entry(inner):
        raise HTTPException(status_code=404, detail="File not found")
    return stat.st_mtime, float(stat.st_size), f"{archive}|{inner}|{stat.st_mtime}|{stat.st_size}"


async def _load_archive_source(
    archive: Path, inner: str
) -> tuple[bytes, float, float, str]:
    """Raw bytes for one ZIP/CBZ entry or one rendered PDF page."""
    from app.archive import ArchiveToolMissing, _is_image_entry, read_archive_entry
    from app.archive_pdf import parse_pdf_page_name, pdf_page_count, render_pdf_page
    from app.scanner import is_pdf_file

    if not inner or inner.startswith("/") or ".." in inner.split("/"):
        raise HTTPException(status_code=400, detail="Invalid image path")
    try:
        stat = archive.stat()
    except (OSError, ValueError):
        # ValueError: embedded null byte makes stat() raise instead of
        # failing with OSError — map to 404 like any other stat failure.
        raise HTTPException(status_code=404, detail="File not found")

    if is_pdf_file(archive):
        count = await run_in_threadpool(pdf_page_count, archive)
        idx = parse_pdf_page_name(inner, count)
        if idx is None:
            raise HTTPException(status_code=404, detail="File not found")
        data = await run_in_threadpool(render_pdf_page, archive, idx)
        if data is None:
            raise HTTPException(
                status_code=501,
                detail="PDF rendering requires pypdfium2 (pip install pypdfium2)",
            )
        return data, stat.st_mtime, float(stat.st_size), f"{archive}|{inner}|{stat.st_mtime}|{stat.st_size}"

    if not _is_image_entry(inner):
        raise HTTPException(status_code=404, detail="File not found")

    try:
        data = await run_in_threadpool(read_archive_entry, archive, inner)
    except ArchiveToolMissing as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    if data is None:
        raise HTTPException(status_code=404, detail="File not found")
    return data, stat.st_mtime, float(stat.st_size), f"{archive}|{inner}|{stat.st_mtime}|{stat.st_size}"


def _open_drafted(data: bytes, width: int):
    """Image.open *data* with ``draft()`` aimed at the (width, width*4) box.

    ``draft()`` must run after open but before the first load()/thumbnail()
    — and before ``exif_transpose``, whose internal ``transpose()`` would
    otherwise trigger a full-size decode on rotated sources. libjpeg then
    decodes at 1/2–1/8 scale, returning the nearest size still covering the
    target; for formats without a draft hook (webp/avif/png, i.e. most
    plugins) it is a no-op returning None. Pillow never drafts below the
    target, but a plugin that did would leave the frame inside the box —
    ``thumbnail()`` only shrinks, so such an image is re-opened undrafted.
    """
    from PIL import Image

    im = Image.open(io.BytesIO(data))
    original = im.size
    try:
        im.draft("RGB", (width, width * 4))
    except Exception:
        # A draft() failure may leave the decoder half-configured —
        # re-open for a guaranteed clean image rather than guess.
        return Image.open(io.BytesIO(data))
    if (
        im.size != original
        and im.size[0] <= width
        and im.size[1] <= width * 4
    ):
        # draft() shrank the frame until it fit inside the target box —
        # the thumb would come out undersized, so decode at full size.
        return Image.open(io.BytesIO(data))
    return im


def make_thumbnail(data: bytes, width: int) -> bytes:
    """EXIF-aware shrink to *width* (aspect kept) → JPEG quality 70.

    Animated sources keep first frame only; non-image bytes raise
    HTTPException 404 like the ``/image/*`` flow. The decode is
    ``draft()``-assisted (see _open_drafted): JPEG sources are decoded by
    libjpeg at 1/2–1/8 scale toward the target box, cutting decode cost on
    both the cover warm and the on-demand ``/thumb`` paths.
    """
    from PIL import Image, ImageOps

    try:
        im = _open_drafted(data, width)
        im = ImageOps.exif_transpose(im)
        im.thumbnail((width, width * 4), Image.LANCZOS)
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=JPEG_QUALITY)
        return buf.getvalue()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=404, detail="File not found")


def _make_thumb_bytes(data: bytes, is_svg: bool, width: int) -> bytes:
    """Render *data* to thumb bytes: SVG passes through, else resized JPEG.

    SVG sources have no Pillow decoder and browsers scale them natively,
    so the original bytes are returned unchanged; anything else goes
    through make_thumbnail (non-image bytes raise 404 there).
    """
    return data if is_svg else make_thumbnail(data, width)


def _store_thumb(dest: Path, thumb_bytes: bytes, *, prune: bool = True) -> None:
    """Persist *thumb_bytes* at *dest* (atomic write, optional namespace prune).

    Parents are created and the body lands via a UNIQUELY named sibling tmp
    file + ``replace`` so concurrent writers of the SAME cover (the startup
    warm and a ``/thumb`` request, now both real) never share a tmp path. A
    fixed tmp name would let one writer truncate the other's in-flight tmp
    and publish a half-written JPEG, and would make the loser's replace raise
    FileNotFoundError (a 500 on the request path). The namespace dir is
    pruned back under cap when *prune* is true; batched callers (the warm
    pass) pass ``prune=False`` and prune once per pass, because per-write
    pruning over ~5k files is an O(N^2) stat storm. Fail-open like
    prune_cache_dir: callers decide whether a store error matters.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{dest.name}.{os.getpid()}.{os.urandom(6).hex()}.tmp"
    try:
        tmp.write_bytes(thumb_bytes)
        tmp.replace(dest)  # atomic so concurrent readers never see partial body
    finally:
        try:
            tmp.unlink()  # replaced (or failed) tmp must never linger
        except OSError:
            pass
    if prune:
        prune_cache_dir(dest.parent)


def _touch_thumb(dest: Path) -> None:
    """Refresh *dest*'s mtime so a warm hit counts as recent use.

    The namespace prune evicts oldest-mtime-first, so without this a warmed
    cover (written once at startup, never rewritten on a hit) would be the
    first thing evicted once viewer thumbnails fill the namespace — and
    first paint would silently go cold again. Fail-open: a failed touch only
    restores the pre-existing eviction order.
    """
    try:
        os.utime(dest)
    except OSError:
        pass


def thumb_cache_namespace(width: int) -> Path:
    """Namespace dir holding cached thumbs for *width* (batched-prune target).

    Mirrors ``cache_path_for``'s ``thumbs/<w>/`` layout so a caller that
    stores with ``prune=False`` can prune the same directory once per batch.
    """
    return cache_root() / "thumbs" / str(width)


async def warm_thumb_cache(downloads_dir: Path, path: str, width: int) -> bool:
    """Prepare the disk-cache thumb for *path* at *width*; True when cached.

    Stat-only hit check first (no archive open on a hit), miss path loads
    the source bytes and stores via the same _make_thumb_bytes/_store_thumb
    helpers handle_thumb uses, so warmed files are byte-identical to served
    ones. A hit refreshes the cache file's mtime (see _touch_thumb) so the
    warmed covers are not the first victims of namespace pruning. Both heavy
    steps run in the threadpool: the Pillow decode/resize is CPU-bound and
    would otherwise starve concurrent requests for the whole pass. Stores
    pass ``prune=False``; the warm pass prunes each namespace once per pass.
    Fail-open: ANY exception (missing source, corrupt bytes, missing
    extractor, unwritable cache) is swallowed and reported as False —
    warming must never break serving or startup.
    """
    try:
        mtime, size, key = _stat_source(downloads_dir, path)
        is_svg = Path(path).suffix.lower() == ".svg"
        dest = cache_path_for(cache_root(), width, key)
        if is_svg:
            dest = dest.with_suffix(".svg")
        if dest.is_file():
            _touch_thumb(dest)
            return True
        data, _mtime, _size, key = await load_source_bytes(downloads_dir, path)
        dest = cache_path_for(cache_root(), width, key)
        if is_svg:
            dest = dest.with_suffix(".svg")
        if dest.is_file():
            _touch_thumb(dest)
            return True
        thumb = await run_in_threadpool(_make_thumb_bytes, data, is_svg, width)
        await run_in_threadpool(_store_thumb, dest, thumb, prune=False)
        return True
    except Exception:
        return False


async def handle_thumb(request: Request, path: str, w: str | int | None) -> Response:
    """Serve ``GET /thumb/{path}?w=`` (called by the thin route in routes.py).

    SVG sources (``.svg`` suffix, case-insensitive, plain files and archive
    entries alike) are passed through as the original bytes with
    ``image/svg+xml`` — browsers scale SVG natively, and Pillow has no SVG
    decoder. They get their own cache suffix (``.svg`` vs ``.jpg``) so a
    JPEG thumb and an SVG passthrough never share a cache file.

    Stat-only fast path: ``_stat_source`` builds the etag + ``dest`` from
    mtime/size/key WITHOUT opening archives or extracting bytes. On a
    ``dest`` hit the cached bytes are returned immediately (still honoring
    ``_not_modified``/304). Only on a miss does the existing
    ``load_source_bytes`` extraction path run.
    """
    from app.routes import _not_modified

    width = normalize_width(w)
    downloads_dir = request.app.state.settings.downloads_path
    mtime, size, key = _stat_source(downloads_dir, path)
    is_svg = Path(path).suffix.lower() == ".svg"

    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    etag = f'"{mtime:.0f}-{size:.0f}-{width}-{digest[:16]}"'
    if _not_modified(request, etag, mtime):
        return Response(status_code=304)

    dest = cache_path_for(cache_root(), width, key)
    if is_svg:
        dest = dest.with_suffix(".svg")
    if dest.is_file():
        body = await run_in_threadpool(dest.read_bytes)
    else:
        data, mtime, size, key = await load_source_bytes(downloads_dir, path)
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        etag = f'"{mtime:.0f}-{size:.0f}-{width}-{digest[:16]}"'
        if _not_modified(request, etag, mtime):
            return Response(status_code=304)

        dest = cache_path_for(cache_root(), width, key)
        if is_svg:
            dest = dest.with_suffix(".svg")

        def _get_or_make() -> bytes:
            if dest.is_file():
                return dest.read_bytes()
            thumb = _make_thumb_bytes(data, is_svg, width)
            _store_thumb(dest, thumb)
            return thumb

        body = await run_in_threadpool(_get_or_make)
    headers = {
        "Cache-Control": f"public, max-age={CACHE_MAX_AGE}",
        "ETag": etag,
        "Last-Modified": utils.formatdate(mtime, usegmt=True),
        "X-Content-Type-Options": "nosniff",
    }
    if is_svg:
        # SVG is an active document: served as image/svg+xml it runs script when
        # navigated to as a top-level document (in-app it is only ever loaded via
        # <img>/new Image(), which does not execute it). `sandbox` puts such a
        # navigation in an opaque origin, so a crafted .svg in the library
        # cannot reach cookies, storage, or /api/* of this origin.
        # `Content-Disposition: attachment` is deliberately NOT used — it would
        # break <img> rendering, which is the whole point of the passthrough.
        headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return Response(
        content=body,
        media_type="image/svg+xml" if is_svg else "image/jpeg",
        headers=headers,
    )


# ── /view/{path}: reader-page format normalization ──────────────────────────
# Reader pages are original-size by policy; /view keeps that size but
# normalizes formats that decode slowly on low-end hardware (webp/avif —
# software-decoded there) into baseline JPEG. Sources already optimal for
# decode (jpeg/png/gif) or natively scalable (svg) pass through byte-identical.
VIEW_JPEG_QUALITY = 85
VIEW_MAX_WIDTH = 2560
VIEW_PASSTHROUGH = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
}


def view_cache_path_for(cache_dir: Path, key: str) -> Path:
    """``<cache_dir>/view/<sha1(key)>.jpg`` — own namespace under the shared cap."""
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return cache_dir / "view" / f"{digest}.jpg"


def view_cache_namespace() -> Path:
    """Namespace dir for normalized reader pages (batched-prune target)."""
    return cache_root() / "view"


def make_view_bytes(data: bytes) -> bytes:
    """Normalize any Pillow-decodable source to JPEG q85 (≤VIEW_MAX_WIDTH).

    Alpha is flattened onto white (paper); like make_thumbnail, non-image
    bytes raise 404 to mirror the ``/image/*`` flow.
    """
    from PIL import Image, ImageOps

    try:
        im = Image.open(io.BytesIO(data))
        im = ImageOps.exif_transpose(im)
        im.thumbnail((VIEW_MAX_WIDTH, VIEW_MAX_WIDTH * 4), Image.LANCZOS)
        if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
            rgba = im.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[-1])
            im = bg
        elif im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=VIEW_JPEG_QUALITY)
        return buf.getvalue()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=404, detail="File not found")


async def handle_view(request: Request, path: str) -> Response:
    """Serve ``GET /view/{path}`` — a reader page in a fast-decode format.

    Passthrough formats serve the original bytes straight through (a
    re-encode would only lose quality and still cost the same decode). The
    normalized branch mirrors ``handle_thumb``: stat-only fast path checks
    the ``view/`` disk cache before any archive open or byte extraction, and
    the same ``_not_modified``/304 conditional semantics apply.
    """
    from app.routes import _not_modified

    downloads_dir = request.app.state.settings.downloads_path
    suffix = Path(path).suffix.lower()

    if suffix in VIEW_PASSTHROUGH or suffix == ".svg":
        data, mtime, size, key = await load_source_bytes(downloads_dir, path)
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        etag = f'"{mtime:.0f}-{size:.0f}-view-{digest[:16]}"'
        if _not_modified(request, etag, mtime):
            return Response(status_code=304)
        headers = {
            "Cache-Control": f"public, max-age={CACHE_MAX_AGE}",
            "ETag": etag,
            "Last-Modified": utils.formatdate(mtime, usegmt=True),
            "X-Content-Type-Options": "nosniff",
        }
        if suffix == ".svg":
            # Same rationale as handle_thumb: served as image/svg+xml it runs
            # script on top-level navigation; sandbox it to an opaque origin.
            headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
            media = "image/svg+xml"
        else:
            media = VIEW_PASSTHROUGH[suffix]
        return Response(content=data, media_type=media, headers=headers)

    mtime, size, key = _stat_source(downloads_dir, path)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    etag = f'"{mtime:.0f}-{size:.0f}-view-{digest[:16]}"'
    if _not_modified(request, etag, mtime):
        return Response(status_code=304)

    dest = view_cache_path_for(cache_root(), key)
    if dest.is_file():
        body = await run_in_threadpool(dest.read_bytes)
    else:
        data, mtime, size, key = await load_source_bytes(downloads_dir, path)
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        etag = f'"{mtime:.0f}-{size:.0f}-view-{digest[:16]}"'
        if _not_modified(request, etag, mtime):
            return Response(status_code=304)
        dest = view_cache_path_for(cache_root(), key)

        def _get_or_make() -> bytes:
            if dest.is_file():
                return dest.read_bytes()
            body = make_view_bytes(data)
            _store_thumb(dest, body)
            return body

        body = await run_in_threadpool(_get_or_make)
    return Response(
        content=body,
        media_type="image/jpeg",
        headers={
            "Cache-Control": f"public, max-age={CACHE_MAX_AGE}",
            "ETag": etag,
            "Last-Modified": utils.formatdate(mtime, usegmt=True),
            "X-Content-Type-Options": "nosniff",
        },
    )


def _raw_query_value(query_string: bytes, key: str) -> str | None:
    """First still-percent-encoded value of *key* in a raw query string.

    The sprite ``names`` CSV is split on literal commas BEFORE decoding so
    entry names containing a comma (``%2C`` — the frontend encodes each name
    with ``encodeURIComponent``) survive the split.
    """
    try:
        qs = query_string.decode("latin-1")
    except Exception:
        return None
    value: str | None = None
    for part in qs.split("&"):
        k, sep, v = part.partition("=")
        if sep and k == key and value is None:
            value = v
    return value


def compose_sprite(cells: list[bytes | None], width: int) -> bytes:
    """Concatenate JPEG thumb bytes horizontally into one contact sheet.

    ``cells`` holds one entry per grid cell in request order; ``None`` marks
    a failed entry and paints that cell SPRITE_GRAY (fail-open). The sheet is
    ``len(cells) * width`` px wide; height is the tallest cell (aspect kept
    per cell, never stretched). Failed cells span the full sheet height.
    """
    from PIL import Image

    thumbs: list[object] = []
    for raw in cells:
        if raw is None:
            thumbs.append(None)
            continue
        im = Image.open(io.BytesIO(raw))
        thumbs.append(im.convert("RGB"))
    heights = [im.height for im in thumbs if im is not None]  # type: ignore[union-attr]
    sheet_h = max(heights) if heights else width
    sheet = Image.new("RGB", (len(cells) * width, sheet_h), SPRITE_GRAY)
    for i, im in enumerate(thumbs):
        if im is None:
            continue
        sheet.paste(im, (i * width, 0))  # type: ignore[arg-type]
    buf = io.BytesIO()
    sheet.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


def _read_sprite_entries(archive: Path, names: list[str]) -> list[bytes | None]:
    """Read up to SPRITE_MAX_ENTRIES entries with exactly ONE archive open.

    Entries are read SEQUENTIALLY in request order. Any per-entry failure
    (non-image name, traversal shape, missing entry, corrupt bytes) yields
    ``None`` for that cell — never an exception. ``ArchiveToolMissing`` (RAR
    without an extractor) propagates so the caller can answer 501 like the
    single-thumb path.
    """
    import zipfile

    from app.archive import (
        ArchiveToolMissing,
        RAR_EXTENSIONS,
        _is_image_entry,
    )

    suffix = archive.suffix.lower()
    if suffix in RAR_EXTENSIONS:
        try:
            import rarfile
        except ImportError as exc:
            raise ArchiveToolMissing("RAR support requires the rarfile package") from exc
        try:
            rf = rarfile.RarFile(str(archive))
        except rarfile.RarCannotExec as exc:
            raise ArchiveToolMissing(
                "RAR extraction requires 7-Zip (7z) or unrar on PATH"
            ) from exc
        except Exception:
            return [None] * len(names)
        with rf:
            try:
                available = set(rf.namelist())
            except Exception:
                return [None] * len(names)
            out: list[bytes | None] = []
            for name in names:
                if not _is_image_entry(name) or name not in available:
                    out.append(None)
                    continue
                try:
                    out.append(rf.read(name))
                except rarfile.RarCannotExec as exc:
                    raise ArchiveToolMissing(
                        "RAR extraction requires 7-Zip (7z) or unrar on PATH"
                    ) from exc
                except Exception:
                    out.append(None)
            return out
    # ZIP/CBZ/EPUB (EPUB is ZIP-based): one open, one namelist, sequential reads.
    try:
        zf = zipfile.ZipFile(archive)
    except Exception:
        return [None] * len(names)
    with zf:
        try:
            available = set(zf.namelist())
        except Exception:
            return [None] * len(names)
        out = []
        for name in names:
            if not _is_image_entry(name) or name not in available:
                out.append(None)
                continue
            try:
                out.append(zf.read(name))
            except Exception:
                out.append(None)
        return out


def sprite_cache_path_for(cache_dir: Path, width: int, key: str) -> Path:
    """``<cache_dir>/sprites/<w>/<sha1(archive+mtime+size+names)>.jpg``."""
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return cache_dir / "sprites" / str(width) / f"{digest}.jpg"


def _parse_sprite_rows(raw: str | None) -> list[int] | None:
    """Parse ``rows=3,3,3`` into a per-row item-count list (2D grid sheet).

    Returns None for missing/invalid specs — callers fall back to the legacy
    horizontal strip. Each row holds 1..9 items, at most 3 rows (the
    frontend's multiThumbSpec table).
    """
    if not raw:
        return None
    try:
        rows = [int(x) for x in raw.split(",")]
    except ValueError:
        return None
    if not 1 <= len(rows) <= 3 or any(r < 1 or r > 9 for r in rows):
        return None
    return rows


def compose_sprite_grid(cells: list[bytes | None], rows: list[int], size: tuple[int, int] | None = None) -> tuple[bytes, int]:
    """Compose a 2D contact sheet matching the DOM multi-thumb grid.

    The sheet is ``size`` (default ``SPRITE_GRID_DIMS[DEFAULT_WIDTH]`` — the
    tile's 2:3 aspect) so a single ``object-fit:cover`` img spanning the
    tile reproduces the multi-img grid with ONE decoded image. Each row's
    cells share the row width equally (matching the grid's column
    spanning); every cell is center-crop fitted like CSS
    ``object-fit:cover``. Failed entries leave their slot SPRITE_GRAY
    (fail-open). Returns (jpeg, ok-count) so the caller can map all-fail
    to 404 like the strip path.
    """
    from PIL import Image, ImageOps

    sheet_w, sheet_h = size or SPRITE_GRID_DIMS[DEFAULT_WIDTH]
    sheet = Image.new("RGB", (sheet_w, sheet_h), SPRITE_GRAY)
    row_h = sheet_h // len(rows)
    ok = 0
    idx = 0
    for r, items in enumerate(rows):
        cw = sheet_w // items
        for c in range(items):
            if idx >= len(cells):
                break
            raw = cells[idx]
            idx += 1
            if raw is None:
                continue
            try:
                im = Image.open(io.BytesIO(raw))
                im = ImageOps.exif_transpose(im)
                cell_im = ImageOps.fit(im, (cw, row_h), Image.LANCZOS)
                sheet.paste(cell_im.convert("RGB"), (c * cw, r * row_h))
                ok += 1
            except Exception:
                continue
    buf = io.BytesIO()
    sheet.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue(), ok


def _dir_sprite_name_bad(name: str) -> bool:
    """Per-name guard for dir sprites: traversal/absolute/null → bad cell."""
    return (
        not name
        or name.startswith("/")
        or ".." in name.split("/")
        or "\x00" in name
    )


def _stat_dir_sprite_entries(
    downloads_dir: Path, folder: str, names: list[str]
) -> tuple[list[str], float]:
    """Stat-only pass over dir-sprite names → (key parts, max mtime).

    Each name is a path RELATIVE to *folder* (e.g. ``sub/cover.jpg``, or a
    virtual ``child.cbz/entry.png`` resolved by ``_resolve_archive`` inside
    the shared helpers). Bad/failed names contribute a placeholder part so
    the cache key still distinguishes them — matching the archive path's
    fail-open semantics.
    """
    keys: list[str] = []
    max_mtime = 0.0
    for name in names:
        if _dir_sprite_name_bad(name):
            keys.append("bad:" + name)
            continue
        try:
            mtime, _size, skey = _stat_source(downloads_dir, folder + "/" + name)
        except HTTPException:
            keys.append("miss:" + name)
            continue
        max_mtime = max(max_mtime, mtime)
        keys.append(skey)
    return keys, max_mtime


async def _load_dir_sprite_entries(
    downloads_dir: Path, folder: str, names: list[str]
) -> list[bytes | None]:
    """Load dir-sprite cell bytes; per-entry failure → None (fail-open)."""
    raws: list[bytes | None] = []
    for name in names:
        if _dir_sprite_name_bad(name):
            raws.append(None)
            continue
        try:
            data, _m, _s, _k = await load_source_bytes(downloads_dir, folder + "/" + name)
        except HTTPException:
            data = None
        raws.append(data)
    return raws


async def handle_thumb_sprite(request: Request) -> Response:
    """Serve ``GET /api/thumb-sprite?folder=&names=&w=&rows=`` (called by routes.py).

    ``folder`` is either a SINGLE archive file (``names`` = entry names) or a
    directory (``names`` = paths relative to it, e.g. ``sub/cover.jpg`` — the
    same ``thumbs[]`` shape the frontend emits, resolved through the shared
    ``_stat_source``/``load_source_bytes`` so archive children inside a
    directory sprite work too). Anything else → 400; symlink escape → 403,
    same ``_is_within`` guard as ``/thumb/``. ``names`` is a comma-separated
    list (up to SPRITE_MAX_ENTRIES, extras ignored); each entry passes the
    same traversal guards as the single-thumb path, but a per-entry failure
    paints that cell gray instead of failing the request. All entries
    failing → 404. Sheets are cached on disk under a ``sprites/`` namespace
    so repeat views are free.

    ``rows`` (e.g. ``rows=3,3,3``) composes a 2D sheet matching the DOM
    multi-thumb grid (one decoded img per tile) at the SPRITE_GRID_DIMS
    size for ``w`` (200 → 240x360, 400 → 480x720, 800 → 960x1440);
    omitted → the legacy horizontal strip.
    """
    from urllib.parse import unquote

    from app.archive import ArchiveToolMissing, is_archive_file
    from app.routes import _is_within, _not_modified

    downloads_dir = request.app.state.settings.downloads_path
    params = request.query_params
    folder = params.get("folder", "")
    # Strip cells keep the fixed SPRITE_CELL_WIDTH geometry; `w` selects
    # the grid-sheet size class (SPRITE_GRID_DIMS) for the rows= path —
    # the frontend picks w by devicePixelRatio so low-dpr Android gets a
    # ~4x smaller decode footprint.
    cell = SPRITE_CELL_WIDTH
    rows = _parse_sprite_rows(params.get("rows"))
    grid_size = SPRITE_GRID_DIMS[normalize_width(params.get("w"))]

    if not folder or folder.startswith("/") or folder.endswith("/") or "\x00" in folder:
        raise HTTPException(status_code=400, detail="Invalid sprite folder")
    if ".." in folder.split("/"):
        raise HTTPException(status_code=400, detail="Invalid sprite folder")
    folder_path = downloads_dir / folder
    if not _is_within(downloads_dir, folder_path):
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        folder_path = folder_path.resolve()
        is_archive = folder_path.is_file() and is_archive_file(folder_path)
        is_dir = folder_path.is_dir()
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="File not found")
    if not folder_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if not is_archive and not is_dir:
        raise HTTPException(status_code=400, detail="Not an archive or directory")

    raw_names = _raw_query_value(request.scope.get("query_string", b""), "names")
    if raw_names is None:
        raise HTTPException(status_code=400, detail="Missing names")
    names = [unquote(p) for p in raw_names.split(",")]
    names = [n for n in names if n][:SPRITE_MAX_ENTRIES]
    if not names:
        raise HTTPException(status_code=400, detail="Missing names")

    # \x00 separator (not ","): a literal comma inside one entry name
    # (e.g. "a,b") would otherwise collide with two entries "a"+"b"
    # (same key/etag → wrong sheet served). Matches warmup_cache's key
    # style; safe here because the key is only ever sha1-hashed.
    names_part = "\x00".join(names)
    # Grid geometry goes in the key so a sheet-size change invalidates old
    # cache entries instead of serving stale-dimension sheets forever.
    rows_part = ("-".join(str(r) for r in rows) + f"@{grid_size[0]}x{grid_size[1]}") if rows else ""

    if is_archive:
        try:
            stat = folder_path.stat()
        except (OSError, ValueError):
            # ValueError: embedded null byte makes stat() raise instead of
            # failing with OSError — map to 404 like any other stat failure.
            raise HTTPException(status_code=404, detail="File not found")
        key = f"{folder_path}|{stat.st_mtime}|{stat.st_size}|{names_part}|{cell}|{rows_part}"
        etag_mtime = stat.st_mtime
        etag_size = stat.st_size
    else:
        entry_keys, max_mtime = _stat_dir_sprite_entries(downloads_dir, folder, names)
        entries_part = "\x00".join(entry_keys)
        key = f"dir|{folder_path}|{names_part}|{entries_part}|{cell}|{rows_part}"
        etag_mtime = max_mtime or 0.0
        etag_size = float(len(entry_keys))

    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    etag = f'"{etag_mtime:.0f}-{etag_size:.0f}-{cell}-s{digest[:16]}"'
    if _not_modified(request, etag, etag_mtime):
        return Response(status_code=304)
    dest = sprite_cache_path_for(cache_root(), cell, key)

    # Cache-hit check BEFORE loading source bytes: dir sprites would
    # otherwise pay one file/archive read per cell on every view.
    if dest.is_file():
        return Response(
            content=await run_in_threadpool(dest.read_bytes),
            media_type="image/jpeg",
            headers={
                "Cache-Control": f"public, max-age={CACHE_MAX_AGE}",
                "ETag": etag,
                "Last-Modified": utils.formatdate(etag_mtime, usegmt=True),
                "X-Content-Type-Options": "nosniff",
            },
        )

    if is_archive:
        def _load_raws() -> list[bytes | None]:
            try:
                return _read_sprite_entries(folder_path, names)
            except ArchiveToolMissing as exc:
                raise HTTPException(status_code=501, detail=str(exc))
        try:
            raws = await run_in_threadpool(_load_raws)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=404, detail="File not found")
    else:
        raws = await _load_dir_sprite_entries(downloads_dir, folder, names)

    def _make() -> bytes:
        if rows:
            # Grid cells take the raw bytes — compose_sprite_grid does its
            # own decode + cover-crop and reports how many cells rendered.
            sheet, ok = compose_sprite_grid(raws, rows, grid_size)
            if ok == 0:
                raise HTTPException(status_code=404, detail="File not found")
        else:
            thumbs: list[bytes | None] = []
            failed = 0
            for raw in raws:
                if raw is None:
                    thumbs.append(None)
                    failed += 1
                    continue
                try:
                    thumbs.append(make_thumbnail(raw, cell))
                except HTTPException:
                    thumbs.append(None)
                    failed += 1
            if failed == len(names):
                raise HTTPException(status_code=404, detail="File not found")
            sheet = compose_sprite(thumbs, cell)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(sheet)
        tmp.replace(dest)  # atomic so concurrent readers never see partial body
        prune_cache_dir(dest.parent)
        return sheet

    try:
        body = await run_in_threadpool(_make)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=404, detail="File not found")
    headers = {
        "Cache-Control": f"public, max-age={CACHE_MAX_AGE}",
        "ETag": etag,
        "Last-Modified": utils.formatdate(etag_mtime, usegmt=True),
        "X-Content-Type-Options": "nosniff",
    }
    return Response(content=body, media_type="image/jpeg", headers=headers)
