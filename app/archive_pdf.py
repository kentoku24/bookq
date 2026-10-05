"""Server-side PDF page count + PNG rendering (Roadmap P1-8).

Uses pypdfium2 (Apache-2.0 / PDFium BSD-3-Clause — no AGPL concern, unlike
PyMuPDF) when installed. Falls back to a regex page-count heuristic so an
archive listing never 500s without the dependency — page *serving* then
answers 501 with a hint to install pypdfium2.
"""

import re
import time
from pathlib import Path

from app import warmup_cache

_PAGE_RE = re.compile(rb"/Type\s*/Page[^s]")

# Process-internal page-count memo, keyed ``(path, mtime_ns, size)``.
# Same generation horizon as the index cache (``app/routes.py``
# ``INDEX_CACHE_TTL`` = 300): entries older than ``_PDF_MEMO_TTL`` force a
# re-read, and any mtime/size mismatch re-reads immediately, so grown pages
# are picked up and a stale count is never served. The memo only replaces the
# *count* lookup — callers always build ``children`` from the count, never
# skip it. Branch behavior is unchanged: with ``pypdfium2`` installed the
# count comes from opening the ``PdfDocument``; without it the ``read_bytes``
# + regex fallback applies (page *serving* then answers 501).
_PDF_MEMO_TTL = 300.0
_PDF_COUNT_MEMO: dict[tuple[str, int, int], tuple[int, float]] = {}
_PDF_MEMO_STATS = {"hits": 0, "misses": 0}


def pdf_memo_stats() -> dict[str, int]:
    """Return a copy of the memo hit/miss counters (observability for tests)."""
    return dict(_PDF_MEMO_STATS)


def clear_pdf_page_count_memo() -> None:
    """Drop all memoized page counts and reset hit/miss counters."""
    _PDF_COUNT_MEMO.clear()
    _PDF_MEMO_STATS["hits"] = 0
    _PDF_MEMO_STATS["misses"] = 0


def _pdf_memo_key(path: Path) -> tuple[str, int, int] | None:
    """Return the memo key for *path*, or None when it cannot be stated."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (str(path), st.st_mtime_ns, st.st_size)


def _uncached_pdf_page_count(path: Path) -> int:
    """Count pages in *path* without consulting the memo (0 on error)."""
    try:
        import pypdfium2 as pdfium

        try:
            doc = pdfium.PdfDocument(str(path))
        except Exception:
            return 0
        try:
            return len(doc)
        finally:
            doc.close()
    except ImportError:
        pass
    try:
        data = path.read_bytes()
    except OSError:
        return 0
    return len(_PAGE_RE.findall(data))


def pdf_page_count(path: Path, downloads_root=None) -> int:
    """Return the number of pages in *path* (0 on error).

    *downloads_root* selects the disk-cache key root explicitly (scan_tree
    passes its top-level base so keys stay ``r:``-relative even when
    ``DOWNLOADS_DIR`` is unset in the container). None (default) keeps the
    current env-lookup behavior via ``cache_key_for_path``; all existing
    callers work unchanged.
    """
    key = _pdf_memo_key(path)
    if key is None:
        return 0
    now = time.monotonic()
    cached = _PDF_COUNT_MEMO.get(key)
    if cached is not None and now - cached[1] <= _PDF_MEMO_TTL:
        _PDF_MEMO_STATS["hits"] += 1
        return cached[0]
    _PDF_MEMO_STATS["misses"] += 1
    # Restart-spanning disk cache, consulted ONLY on memo miss (fail-open:
    # any cache error falls through to the live read below).
    disk_key = None
    try:
        if downloads_root is not None:
            disk_key = warmup_cache.cache_key(downloads_root, path)
        else:
            disk_key = warmup_cache.cache_key_for_path(path)
        if disk_key is not None:
            hit = warmup_cache.get_pdf_count(disk_key)
            if hit is not None:
                _PDF_COUNT_MEMO[key] = (hit, now)
                return hit
    except Exception:
        disk_key = None
    count = _uncached_pdf_page_count(path)
    _PDF_COUNT_MEMO[key] = (count, now)
    try:
        if disk_key is not None:
            warmup_cache.put_pdf_count(disk_key, count)
    except Exception:
        pass
    return count


def render_pdf_page(path: Path, index: int, scale: float = 2.0) -> bytes | None:
    """Render 0-based page *index* to PNG bytes, or None if unavailable."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return None
    try:
        doc = pdfium.PdfDocument(str(path))
    except Exception:
        return None
    try:
        if index < 0 or index >= len(doc):
            return None
        page = doc[index]
        try:
            bitmap = page.render(scale=scale)
            pil_image = bitmap.to_pil()
            import io

            buf = io.BytesIO()
            pil_image.save(buf, format="PNG")
            return buf.getvalue()
        finally:
            page.close()
    except Exception:
        return None
    finally:
        doc.close()


def pdf_page_name(index: int) -> str:
    """Synthetic 1-based page filename exposed to the viewer (p001.png …)."""
    return f"p{index + 1:03d}.png"


def parse_pdf_page_name(name: str, page_count: int) -> int | None:
    """Map ``p001.png``-style names back to a 0-based index, or None."""
    m = re.fullmatch(r"p(\d+)\.png", name)
    if not m:
        return None
    idx = int(m.group(1)) - 1
    if 0 <= idx < page_count:
        return idx
    return None
