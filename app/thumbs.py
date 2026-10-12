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


def make_thumbnail(data: bytes, width: int) -> bytes:
    """EXIF-aware shrink to *width* (aspect kept) → JPEG quality 70.

    Animated sources keep first frame only; non-image bytes raise
    HTTPException 404 like the ``/image/*`` flow.
    """
    from PIL import Image, ImageOps

    try:
        im = Image.open(io.BytesIO(data))
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


def _store_thumb(dest: Path, thumb_bytes: bytes) -> None:
    """Persist *thumb_bytes* at *dest* (atomic write, then prune the namespace).

    Parents are created, the body lands via tmp-file + replace so
    concurrent readers never see a partial file, and the namespace dir is
    pruned back under cap. Fail-open like prune_cache_dir: callers decide
    whether a store error matters.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(thumb_bytes)
    tmp.replace(dest)  # atomic so concurrent readers never see partial body
    prune_cache_dir(dest.parent)


async def warm_thumb_cache(downloads_dir: Path, path: str, width: int) -> bool:
    """Prepare the disk-cache thumb for *path* at *width*; True when cached.

    Stat-only hit check first (no archive open on a hit), miss path loads
    the source bytes and stores via the same _make_thumb_bytes/_store_thumb
    helpers handle_thumb uses, so warmed files are byte-identical to served
    ones. Fail-open: ANY exception (missing source, corrupt bytes, missing
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
            return True
        data, _mtime, _size, key = await load_source_bytes(downloads_dir, path)
        dest = cache_path_for(cache_root(), width, key)
        if is_svg:
            dest = dest.with_suffix(".svg")
        if dest.is_file():
            return True
        thumb = _make_thumb_bytes(data, is_svg, width)
        await run_in_threadpool(_store_thumb, dest, thumb)
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


async def handle_thumb_sprite(request: Request) -> Response:
    """Serve ``GET /api/thumb-sprite?folder=&names=&w=`` (called by routes.py).

    ``folder`` must be a SINGLE archive file under the downloads dir
    (anything else → 400; symlink escape → 403, same ``_is_within`` guard as
    ``/thumb/``). ``names`` is a comma-separated list of entry names (up to
    SPRITE_MAX_ENTRIES, extras ignored); each entry passes the same
    ``_is_image_entry`` traversal guard as the single-thumb path, but a
    per-entry failure paints that cell gray instead of failing the request.
    All entries failing → 404. Sheets are cached on disk under a ``sprites/``
    namespace (keyed by archive mtime/size + names) so repeat views are free.
    """
    from urllib.parse import unquote

    from app.archive import ArchiveToolMissing, is_archive_file
    from app.routes import _is_within, _not_modified

    downloads_dir = request.app.state.settings.downloads_path
    params = request.query_params
    folder = params.get("folder", "")
    # Sheet geometry is fixed (SPRITE_CELL_WIDTH); `w` is accepted for URL-shape
    # compatibility with the single-thumb endpoint and otherwise ignored.
    cell = SPRITE_CELL_WIDTH

    if not folder or folder.startswith("/") or folder.endswith("/"):
        raise HTTPException(status_code=400, detail="Invalid sprite folder")
    if ".." in folder.split("/"):
        raise HTTPException(status_code=400, detail="Invalid sprite folder")
    archive = downloads_dir / folder
    if not _is_within(downloads_dir, archive):
        raise HTTPException(status_code=403, detail="Forbidden")
    archive = archive.resolve()
    if not archive.is_file() or not is_archive_file(archive):
        raise HTTPException(status_code=400, detail="Not a single archive")

    raw_names = _raw_query_value(request.scope.get("query_string", b""), "names")
    if raw_names is None:
        raise HTTPException(status_code=400, detail="Missing names")
    names = [unquote(p) for p in raw_names.split(",")]
    names = [n for n in names if n][:SPRITE_MAX_ENTRIES]
    if not names:
        raise HTTPException(status_code=400, detail="Missing names")

    try:
        stat = archive.stat()
    except (OSError, ValueError):
        # ValueError: embedded null byte makes stat() raise instead of
        # failing with OSError — map to 404 like any other stat failure.
        raise HTTPException(status_code=404, detail="File not found")

    # \x00 separator (not ","): a literal comma inside one entry name
    # (e.g. "a,b") would otherwise collide with two entries "a"+"b"
    # (same key/etag → wrong sheet served). Matches warmup_cache's key
    # style; safe here because the key is only ever sha1-hashed.
    names_part = "\x00".join(names)
    key = f"{archive}|{stat.st_mtime}|{stat.st_size}|{names_part}|{cell}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    etag = f'"{stat.st_mtime:.0f}-{stat.st_size:.0f}-{cell}-s{digest[:16]}"'
    if _not_modified(request, etag, stat.st_mtime):
        return Response(status_code=304)
    dest = sprite_cache_path_for(cache_root(), cell, key)

    def _get_or_make() -> bytes:
        if dest.is_file():
            return dest.read_bytes()
        try:
            raws = _read_sprite_entries(archive, names)
        except ArchiveToolMissing as exc:
            raise HTTPException(status_code=501, detail=str(exc))
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
        body = await run_in_threadpool(_get_or_make)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=404, detail="File not found")
    headers = {
        "Cache-Control": f"public, max-age={CACHE_MAX_AGE}",
        "ETag": etag,
        "Last-Modified": utils.formatdate(stat.st_mtime, usegmt=True),
        "X-Content-Type-Options": "nosniff",
    }
    return Response(content=body, media_type="image/jpeg", headers=headers)
