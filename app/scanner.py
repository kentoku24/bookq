"""Folder scanning utilities for the image viewer."""

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# ASCII-only split on purpose: Python's \d matches full-width digits ("２")
# while ASCII [0-9] does not. A shared \d+ would split "第２話" differently
# ("第２話" → ("第","２","話") vs "第10話" →
# ("第",10,"話") → TypeError str vs int). The isascii() guard additionally
# keeps names made ENTIRELY of non-ASCII digits ("２", "²") as text,
# avoiding int("²") ValueError.
_NUMERIC_RE = re.compile(r"([0-9]+)")

IMAGE_EXTENSIONS = {".avif", ".webp", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg"}

# Archive support (Roadmap P1-8): ZIP/CBZ/RAR/EPUB live in app/archive.py and
# are read on-demand; PDF pages are rendered server-side (app/archive_pdf.py).
# scan_tree imports app.archive lazily (archive.py imports natural_key from here).
PDF_EXTENSIONS = {".pdf"}


# ── native sort-key core (Phase 1 W1 pilot, issue #163) ──────────────────────
# bookq_rs (rust/bookq_rs) implements _sort_key/_normalize_kanji/
# _kanji_run_to_int/natural_key byte-for-byte. When the extension imports, the
# wrappers below delegate per call; BOOKQ_RUST=0 forces the Python path.
# _py_* keep the reference implementations — the parity harness
# (tests/migration/test_rust_parity.py) diffs them against bookq_rs, and they
# are the per-call fallback when the Rust call itself cannot run (non-str
# input, or names with lone surrogates that Rust's &str cannot represent).
try:
    import bookq_rs as _rs
except ImportError:
    _rs = None
if _rs is not None and os.environ.get("BOOKQ_RUST", "1") == "0":
    _rs = None


def _py_sort_key(name: str) -> tuple[int, ...]:
    parts = _NUMERIC_RE.split(name)
    key: list[int] = []
    for p in parts:
        if p.isascii() and p.isdigit():
            key.append(int(p))
    return tuple(key)


def _sort_key(name: str) -> tuple[int, ...]:
    """Extract numeric parts for natural sort (01.avif < 02.avif < 10.avif)."""
    if _rs is not None:
        try:
            return _rs._sort_key(name)
        except Exception:
            pass
    return _py_sort_key(name)


# Kanji numeral normalization (漢数字 → ASCII digits in numeric slots).
# Context-guarded: only runs in a "numeric slot" convert — immediately preceded
# by one of _KANJI_PRE and followed by one of _KANJI_POST (or a string edge).
# This protects proper nouns (千尋, 六本木, 千と千尋の神隠し) while sorting
# 第X部/第X話/第十章 numerically. 万/億/兆 are never numericized (whole run
# stays text — no partial conversion).
_KANJI_DIGITS = {"〇": 0, "一": 1, "二": 2, "三": 3, "四": 4,
                 "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_KANJI_UNITS  = {"十": 10, "百": 100, "千": 1000}
_KANJI_BIG    = "万億兆"          # out of scope: bail the whole run
_KANJI_RUN_RE = re.compile("[" + "".join(_KANJI_DIGITS) + "".join(_KANJI_UNITS) + _KANJI_BIG + "]+")
_KANJI_PRE    = set("第【之の・ー- ")    # context guard (mirrored by bookq_rs)
_KANJI_POST   = set("部話章巻回集編幕期頁号節月】")


def _py_kanji_run_to_int(run: str) -> int | None:
    if any(ch in _KANJI_BIG for ch in run):
        return None
    total, cur = 0, 0
    for ch in run:
        if ch in _KANJI_DIGITS:
            cur = _KANJI_DIGITS[ch]
        elif ch in _KANJI_UNITS:
            total += (cur or 1) * _KANJI_UNITS[ch]
            cur = 0
    return total + cur


def _kanji_run_to_int(run: str) -> int | None:
    """'二十三'->23, '十'->10, '二百五'->205; None if run contains 万/億/兆."""
    if _rs is not None:
        try:
            return _rs._kanji_run_to_int(run)
        except Exception:
            pass
    return _py_kanji_run_to_int(run)


def _py_normalize_kanji(name: str) -> str:
    def repl(m: re.Match) -> str:
        before = name[m.start() - 1] if m.start() > 0 else None
        after  = name[m.end()] if m.end() < len(name) else None
        if before is not None and before not in _KANJI_PRE:  return m.group(0)
        if after  is not None and after  not in _KANJI_POST: return m.group(0)
        value = _py_kanji_run_to_int(m.group(0))
        return m.group(0) if value is None else str(value)
    return _KANJI_RUN_RE.sub(repl, name)


def _normalize_kanji(name: str) -> str:
    """Convert kanji numerals in numeric slots to ASCII digits; else leave as-is."""
    if _rs is not None:
        try:
            return _rs._normalize_kanji(name)
        except Exception:
            pass
    return _py_normalize_kanji(name)


def _py_natural_key(name: str) -> tuple[str | int, ...]:
    return tuple(int(p) if p.isascii() and p.isdigit() else p
                 for p in _NUMERIC_RE.split(_py_normalize_kanji(name)))


def natural_key(name: str) -> tuple[str | int, ...]:
    """Server-side natural sort key (sole source of sort truth; ch-2 < ch-10; 第一部 < 第二部)."""
    if _rs is not None:
        try:
            return _rs.natural_key(name)
        except Exception:
            pass
    return _py_natural_key(name)


def _sort_natural(names: list[str]) -> None:
    """In-place ``sorted(key=lambda n: (natural_key(n), n))``.

    The Rust batch path computes all keys in one FFI call and sorts
    ``zip(keys, names)`` — element-for-element the same (key, name) tuple
    comparison, just faster.
    """
    if _rs is not None:
        try:
            keys = _rs.natural_keys(names)
            names[:] = [n for _, n in sorted(zip(keys, names))]
            return
        except Exception:
            pass
    names.sort(key=lambda n: (natural_key(n), n))


def _sort_file(names: list[str]) -> None:
    """In-place ``sorted(key=lambda n: (_sort_key(n), n))``; batch path like _sort_natural."""
    if _rs is not None:
        try:
            keys = _rs.sort_keys(names)
            names[:] = [n for _, n in sorted(zip(keys, names))]
            return
        except Exception:
            pass
    names.sort(key=lambda n: (_sort_key(n), n))


def is_pdf_file(path: Path) -> bool:
    """True if *path* is an existing PDF file."""
    return path.is_file() and path.suffix.lower() in PDF_EXTENSIONS


def _first_image_of(children: list[dict]) -> str | None:
    """Return the relative path of the first image leaf in built *children*.

    Bottom-up companion to the scan: every folder child already carries its
    own folder-relative ``thumb`` (None when imageless), so the parent's
    thumb is the first image child or ``"<child>/<child.thumb>"`` — a single
    level, never a re-DFS. By induction (imageless folders always carry
    ``thumb=None``) this matches the old per-folder recursive
    walk result while keeping the whole scan linear.
    """
    for child in children:
        if child["type"] == "image":
            return child["name"]
        thumb = child.get("thumb")
        if thumb is not None:
            return child["name"] + "/" + thumb
    return None


def folder_cover_fields(children: list[dict]) -> tuple[str | None, list[str] | None]:
    """Return ``(thumb, thumbs)`` for one folder node from its built *children*.

    Single source of the folder-level cover rule shared by ``scan_tree``,
    ``_scan_archive_node``, ``app.archive.build_archive_tree`` and the
    shallow-warmup fill's ancestor refresh (``app.routes._background_fill``):
    ``thumb`` is the first image of the children (see ``_first_image_of``),
    ``thumbs`` the first up-to-9 immediate sub-folder covers — returned as
    ``None`` when fewer than two exist, so callers never emit the key (the
    rule that keeps multi-thumb grids off single-cover folders).
    """
    thumb = _first_image_of(children)
    sub_thumbs: list[str] = []
    for child in children:
        if child["type"] == "folder" and child.get("thumb"):
            sub_thumbs.append(child["name"] + "/" + child["thumb"])
        if len(sub_thumbs) >= 9:
            break
    return thumb, (sub_thumbs if len(sub_thumbs) >= 2 else None)


def _scan_archive_node(base: Path, aname: str, downloads_root=None) -> dict:
    """Build the virtual-folder node for one archive file.

    Pure function of its arguments: no shared state, no ``_visited``
    contact. Thread-safe by construction — each thread opens its own
    readers via :func:`list_archive_entries` (the ``rarfile`` lazy import
    stays inside the callee, serialised by the CPython import lock).
    *downloads_root* is the scan's top-level base, forwarded as the
    disk-cache key root so keys stay ``r:``-relative (None keeps the
    env-lookup behavior).
    """
    from app.archive import EPUB_EXTENSIONS, build_archive_tree, list_archive_entries

    inner = list_archive_entries(base / aname, downloads_root=downloads_root)
    children: list[dict]
    thumb: str | None
    if Path(aname).suffix.lower() in EPUB_EXTENSIONS:
        children = [{"name": n, "type": "image", "children": None} for n in inner]
        thumb = inner[0] if inner else None
        return {
            "name": aname,
            "type": "folder",
            "children": children,
            "thumb": thumb,
            "archive": True,
        }
    children = build_archive_tree(inner)
    thumb, sub_thumbs = folder_cover_fields(children)
    node: dict = {
        "name": aname,
        "type": "folder",
        "children": children,
        "thumb": thumb,
        "archive": True,
    }
    if sub_thumbs is not None:
        node["thumbs"] = sub_thumbs
    return node


def _scan_pdf_node(base: Path, pname: str, downloads_root=None) -> dict:
    """Build the virtual-folder node for one PDF file.

    Pure function of its arguments: no shared state, no ``_visited``
    contact. Each thread opens its own ``pypdfium2.PdfDocument`` (never
    shared); the ``_PDF_COUNT_MEMO`` check-then-set race inside
    :func:`pdf_page_count` is benign-by-design under the GIL (duplicate
    computation or a lost counter update is possible but correctness is
    unaffected), so no locks are taken and counter exactness is not
    required. *downloads_root* is the scan's top-level base, forwarded as
    the disk-cache key root so keys stay ``r:``-relative (None keeps the
    env-lookup behavior).
    """
    from app.archive_pdf import pdf_page_count, pdf_page_name

    count = pdf_page_count(base / pname, downloads_root=downloads_root)
    inner = [pdf_page_name(i) for i in range(count)]
    children = [{"name": n, "type": "image", "children": None} for n in inner]
    return {
        "name": pname,
        "type": "folder",
        "children": children,
        "thumb": inner[0] if inner else None,
        "archive": True,
        "pdf": True,
    }


def _timed_archive_node(base: Path, aname: str, downloads_root=None) -> tuple[dict, float]:
    """Run :func:`_scan_archive_node`, returning (node, wall_ms).

    The wall span covers this one future's own execution only. Under the
    ThreadPoolExecutor fan-out several futures overlap, so summing these
    spans across a level yields a CPU-span sum, NOT the critical path —
    the sum inflates with overlap by design (pure observation for the
    rebuild log's ``archive_ms`` field).
    """
    t0 = time.perf_counter()
    node = _scan_archive_node(base, aname, downloads_root=downloads_root)
    return node, (time.perf_counter() - t0) * 1000


def _timed_pdf_node(base: Path, pname: str, downloads_root=None) -> tuple[dict, float]:
    """Run :func:`_scan_pdf_node`, returning (node, wall_ms).

    Same overlap caveat as :func:`_timed_archive_node`: the per-future
    spans summed into ``pdf_ms`` are a CPU-span sum, not critical-path.
    """
    t0 = time.perf_counter()
    node = _scan_pdf_node(base, pname, downloads_root=downloads_root)
    return node, (time.perf_counter() - t0) * 1000


def scan_tree(base: Path, _visited: set[tuple[int, int]] | None = None,
              _phases: dict[str, float] | None = None,
              _downloads_root=None, list_archives: bool = True) -> list[dict]:
    """Return recursive folder tree with image-leaves mixed in.

    Each node is a dict:
      {"name": str, "type": "folder"|"image", "children": [...] | None}

    Folder nodes also include:
      "thumb": the relative path of the first image in the subtree, or None.
      "thumbs": up to 9 thumb paths from immediate sub-folders (only if 2+ exist).

    Rules:
    - Directories become "folder" nodes with sorted children.
    - If a folder contains ONLY other folders (no images), its single-item children
      are flattened: the subfolder's children are appended directly to the parent
      instead of creating a nested folder node.

    Symlinked directories are followed (a top-level symlinked collection stays
    browsable), but each visited directory is recorded by its ``(st_dev, st_ino)``
    identity — shared through ``_visited`` across the recursion — so a symlink
    cycle is traversed only once instead of recursing forever. Identity uses the
    following ``stat()``, never ``lstat`` (which would return the link's own
    inode and miss the cycle). Unreadable entries (``OSError``) are skipped.

    Phase timing (pure observation, output-identical): when ``_phases`` is
    given, wall-time is accumulated into it across the whole recursion —
    ``walk_ms`` (iterdir+stat+sort classification per directory level),
    ``archive_ms`` (every ``_scan_archive_node`` future's own wall span,
    summed), ``pdf_ms`` (same for ``_scan_pdf_node``). The archive/pdf sums
    inflate with ThreadPoolExecutor overlap: they are CPU-span sums, not
    the critical path. Callers that need the totals pass their own dict in
    (explicit param — no globals); a bare ``scan_tree(root)`` call
    accumulates into a throwaway.

    Disk-cache key root: ``_downloads_root`` defaults to the top-level
    *base* and is passed unchanged through the recursion (subdirectory
    levels do NOT re-root it) into the archive/pdf node helpers, so cache
    keys stay ``r:``-relative to the scan root even when ``DOWNLOADS_DIR``
    is unset in the container. A bare ``scan_tree(root)`` call and every
    existing 1–3 positional-arg call shape work unchanged.

    Shallow mode (``list_archives=False``): the archives/pdfs enumeration
    loops are skipped entirely — archives/PDFs are appended as opaque
    nodes (``{"name", "type": "folder", "children": [], "thumb": None,
    "archive": True}``, PDFs additionally ``"pdf": True``) in the SAME
    sorted files→archives→pdfs→dirs positions. files/dirs
    classification, all four sorts, single-child flattening and
    ``_visited`` handling are byte-identical to the full scan; only the
    warmup path uses this (request path keeps the default True).
    """
    from app.archive import ARCHIVE_EXTENSIONS

    if _downloads_root is None:
        _downloads_root = base
    if _phases is None:
        _phases = {"walk_ms": 0.0, "archive_ms": 0.0, "pdf_ms": 0.0}
    if not base.exists():
        return []
    if _visited is None:
        _visited = set()
        try:
            st = base.stat()
            _visited.add((st.st_dev, st.st_ino))
        except OSError:
            pass  # unreadable root — children will fail individually
    entries: list[dict] = []

    files: list[str] = []
    dirs: list[str] = []
    archives: list[str] = []
    pdfs: list[str] = []
    walk_start = time.perf_counter()
    for entry in base.iterdir():
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            try:
                st = entry.stat()
            except OSError:
                continue  # broken symlink / unreadable entry — nothing to show
            ident = (st.st_dev, st.st_ino)
            if ident in _visited:
                continue
            _visited.add(ident)
            dirs.append(entry.name)
        elif entry.is_file():
            suffix = entry.suffix.lower()
            if suffix in IMAGE_EXTENSIONS:
                files.append(entry.name)
            elif suffix in ARCHIVE_EXTENSIONS:
                archives.append(entry.name)
            elif suffix in PDF_EXTENSIONS:
                pdfs.append(entry.name)

    _sort_natural(dirs)
    _sort_file(files)
    _sort_natural(archives)
    _sort_natural(pdfs)
    _phases["walk_ms"] += (time.perf_counter() - walk_start) * 1000

    if files:
        for fname in files:
            entries.append({"name": fname, "type": "image", "children": None})

    # Archives/PDFs are virtual folders: their image entries become child
    # leaves so the viewer (node.children + /api/images + /image/<path>)
    # works unchanged, including hash restore (#/view/<archive>?p=<n>).
    # Empty archives yield children=[] + thumb=None (empty message, no 500).
    # Enumeration within this one directory level fans out over a small
    # thread pool; results are re-sorted back into the archives/pdfs sorted
    # order so the files→archives→pdfs→dirs sequence is strictly preserved.
    # Recursion into subdirectories below stays sequential with the shared
    # _visited untouched. Each future returns its own wall span alongside
    # the node; the main thread sums the spans into the phase accumulator
    # (no locks needed — only this thread touches _phases here). Overlapping
    # futures inflate the sum: archive_ms/pdf_ms are CPU-span sums, not the
    # critical path.
    archive_nodes: dict[str, dict] = {}
    pdf_nodes: dict[str, dict] = {}
    if list_archives and (archives or pdfs):
        with ThreadPoolExecutor(max_workers=min(4, len(archives) + len(pdfs))) as pool:
            archive_futs = [(aname, pool.submit(_timed_archive_node, base, aname, _downloads_root))
                            for aname in archives]
            pdf_futs = [(pname, pool.submit(_timed_pdf_node, base, pname, _downloads_root))
                        for pname in pdfs]
            for aname, fut in archive_futs:
                node, span_ms = fut.result()
                archive_nodes[aname] = node
                _phases["archive_ms"] += span_ms
            for pname, fut in pdf_futs:
                node, span_ms = fut.result()
                pdf_nodes[pname] = node
                _phases["pdf_ms"] += span_ms
    for aname in archives:
        if list_archives:
            entries.append(archive_nodes[aname])
        else:
            entries.append({"name": aname, "type": "folder", "children": [],
                            "thumb": None, "archive": True})
    for pname in pdfs:
        if list_archives:
            entries.append(pdf_nodes[pname])
        else:
            entries.append({"name": pname, "type": "folder", "children": [],
                            "thumb": None, "archive": True, "pdf": True})

    for dname in dirs:
        child = scan_tree(base / dname, _visited, _phases, _downloads_root,
                          list_archives=list_archives)
        if not files and not archives and not pdfs and len(dirs) == 1:
            for c in child:
                c["name"] = dname + "/" + c["name"]
            entries.extend(child)
        else:
            thumb, sub_thumbs = folder_cover_fields(child)
            entry: dict = {"name": dname, "type": "folder", "children": child,
                            "thumb": thumb}
            if sub_thumbs is not None:
                entry["thumbs"] = sub_thumbs
            entries.append(entry)

    return entries


def list_images(folder_path: Path) -> list[str]:
    """Return image files sorted naturally by numeric prefix."""
    files = [
        f.name
        for f in folder_path.iterdir()
        if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS and not f.name.startswith(".")
    ]
    _sort_file(files)
    return files
