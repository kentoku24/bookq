"""Folder scanning utilities for the image viewer."""

import errno
import os
import re
import stat as statmod
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


def _sort_key(name: str) -> tuple[int, ...]:
    """Extract numeric parts for natural sort (01.avif < 02.avif < 10.avif)."""
    parts = _NUMERIC_RE.split(name)
    key: list[int] = []
    for p in parts:
        if p.isascii() and p.isdigit():
            key.append(int(p))
    return tuple(key)


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
_KANJI_PRE    = set("第【之の・ー- ")    # must match JS byte-for-byte
_KANJI_POST   = set("部話章巻回集編幕期頁号節月】")


def _kanji_run_to_int(run: str) -> int | None:
    """'二十三'->23, '十'->10, '二百五'->205; None if run contains 万/億/兆."""
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


def _normalize_kanji(name: str) -> str:
    """Convert kanji numerals in numeric slots to ASCII digits; else leave as-is."""
    def repl(m: re.Match) -> str:
        before = name[m.start() - 1] if m.start() > 0 else None
        after  = name[m.end()] if m.end() < len(name) else None
        if before is not None and before not in _KANJI_PRE:  return m.group(0)
        if after  is not None and after  not in _KANJI_POST: return m.group(0)
        value = _kanji_run_to_int(m.group(0))
        return m.group(0) if value is None else str(value)
    return _KANJI_RUN_RE.sub(repl, name)


def natural_key(name: str) -> tuple[str | int, ...]:
    """Server-side natural sort key (sole source of sort truth; ch-2 < ch-10; 第一部 < 第二部)."""
    return tuple(int(p) if p.isascii() and p.isdigit() else p
                 for p in _NUMERIC_RE.split(_normalize_kanji(name)))


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


# Directory-walk fan-out: scandir+stat classification is I/O-bound over the
# downloads volume (virtiofs latency dominates on the host), so one pool per
# call walks each BFS level concurrently — the same pattern
# tree_signature.compute_signature uses.
_WALK_MAX_WORKERS = 8


def _walk_dir(path: Path) -> dict | None:
    """Scan ONE directory's immediate entries for the parallel walk.

    Runs inside the shared pool (pure function of *path* — no ``_visited``
    contact). Returns the four classification lists: ``files`` /
    ``archives`` / ``pdfs`` fully sorted (their order is claim-independent)
    and ``dirs`` as ``(name, (st_dev, st_ino))`` pairs in raw scandir order
    — the visited-claim decision belongs to the main thread, which must
    replay the serial loop's scandir-order claim sequence, so the worker
    neither sorts nor filters this list. ``archive_nodes``/``pdf_nodes``
    start empty and are filled later by the archive fan-out futures.

    Returns ``None`` when the directory vanished before listing
    (``ENOENT``/``ENOTDIR``): the serial recursion's ``base.exists()``
    guard maps the same condition to an empty subtree. Any other
    ``OSError`` (``EACCES`` etc.) propagates — the serial ``iterdir``
    would raise it too.

    Per entry, ONE ``entry.stat()`` covers classification AND the visited
    identity. ``Path.is_dir``/``is_file`` delegate to ``os.path.isdir``/
    ``isfile``, which swallow every stat ``OSError``/``ValueError`` into
    ``False`` (Python 3.14+), so a failed stat maps to "not a dir, not a
    file" — exactly the entry the serial loop drops. The serial loop's
    per-entry stat calls (``is_dir`` → ``stat`` → ``is_file``) collapse
    into this one.
    """
    from app.archive import ARCHIVE_EXTENSIONS

    try:
        with os.scandir(path) as it:
            entries = [e for e in it if not e.name.startswith(".")]
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            return None
        raise
    files: list[str] = []
    archives: list[str] = []
    pdfs: list[str] = []
    dirs: list[tuple[str, tuple[int, int]]] = []
    for entry in entries:
        try:
            st = entry.stat()  # follows symlinks; cached on the DirEntry
        except (OSError, ValueError):
            continue  # Path.is_dir()/is_file() → False → serial drops it
        if statmod.S_ISDIR(st.st_mode):
            dirs.append((entry.name, (st.st_dev, st.st_ino)))
        elif statmod.S_ISREG(st.st_mode):
            suffix = Path(entry.name).suffix.lower()
            if suffix in IMAGE_EXTENSIONS:
                files.append(entry.name)
            elif suffix in ARCHIVE_EXTENSIONS:
                archives.append(entry.name)
            elif suffix in PDF_EXTENSIONS:
                pdfs.append(entry.name)
    files.sort(key=lambda f: (_sort_key(f), f))
    archives.sort(key=lambda a: (natural_key(a), a))
    pdfs.sort(key=lambda a: (natural_key(a), a))
    return {"files": files, "archives": archives, "pdfs": pdfs,
            "dirs": dirs, "archive_nodes": {}, "pdf_nodes": {}}


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
    identity — shared through ``_visited`` across the walk — so a symlink
    cycle is traversed only once instead of recursing forever. Identity uses the
    following ``stat()``, never ``lstat`` (which would return the link's own
    inode and miss the cycle). Unreadable entries (``OSError``) are skipped.

    Parallel walk (#242): the traversal is a level-synchronous BFS over ONE
    ``ThreadPoolExecutor`` per call (same pattern as
    ``tree_signature.compute_signature`` — no nested submissions, so no
    deadlock): each level's directories run their scandir+stat+sort
    classification concurrently in :func:`_walk_dir` while the main thread
    alone owns ``_visited`` and expands the next level. The output stays
    byte-identical to :func:`_scan_tree_serial` because claiming is
    two-phase: the walk claims each inode once so the SAME reachability
    closure of directories gets scanned as the serial DFS visits (the
    first-seen path may differ where one inode is aliased at different
    depths, but the inode's contents are identical under any alias), then
    ``_assemble`` replays the serial claim order — each directory's
    scandir-order children are claimed before recursion walks its sorted
    ``dirs`` (DFS preorder) — on a fresh ``emitted`` set, so every alias
    resolves to the same winning path the serial walk picks and the
    resulting tree is identical node-for-node. ``_visited`` itself ends
    holding the same final set the serial walk leaves behind.

    Archive/PDF virtual-folder nodes are built by the same pure helpers
    (``_timed_archive_node`` / ``_timed_pdf_node``), fanned out on the same
    pool as each level's record lands — results key by directory and name,
    so enumeration overlaps the walk tail without changing any node.

    Phase timing (pure observation, output-identical): when ``_phases`` is
    given, ``walk_ms`` accumulates the main-thread wall time of each BFS
    level (the number that drops with parallelization), while
    ``archive_ms``/``pdf_ms`` keep summing every archive/pdf future's own
    wall span — CPU-span sums that inflate with overlap, never the
    critical path. Callers that need the totals pass their own dict in
    (explicit param — no globals); a bare ``scan_tree(root)`` call
    accumulates into a throwaway.

    Disk-cache key root: ``_downloads_root`` defaults to the top-level
    *base* and is passed unchanged into the archive/pdf node helpers, so
    cache keys stay ``r:``-relative to the scan root even when
    ``DOWNLOADS_DIR`` is unset in the container. A bare ``scan_tree(root)``
    call and every existing 1–3 positional-arg call shape work unchanged.

    Shallow mode (``list_archives=False``): the archive/pdf enumeration is
    skipped entirely — archives/PDFs are appended as opaque nodes
    (``{"name", "type": "folder", "children": [], "thumb": None,
    "archive": True}``, PDFs additionally ``"pdf": True``) in the SAME
    sorted files→archives→pdfs→dirs positions. files/dirs
    classification, all four sorts, single-child flattening and
    ``_visited`` handling are byte-identical to the full scan; only the
    warmup path uses this (request path keeps the default True).
    """
    if _downloads_root is None:
        _downloads_root = base
    if _phases is None:
        _phases = {"walk_ms": 0.0, "archive_ms": 0.0, "pdf_ms": 0.0}
    if not base.exists():
        return []
    try:
        root_st = base.stat()
    except OSError:
        root_st = None
    if _visited is None:
        _visited = set()
        if root_st is not None:
            _visited.add((root_st.st_dev, root_st.st_ino))
    # scans is keyed by the claimed (st_dev, st_ino); the root's key is its
    # stat identity when known, else a non-inode sentinel (the serial walk
    # proceeds identically without it — a descendant symlink may still
    # claim the real inode later, which then scans and emits normally).
    root_key: tuple = (root_st.st_dev, root_st.st_ino) if root_st is not None \
        else ("<scan-root>", -1)
    # emitted replays the serial claim order during assembly (DFS preorder:
    # a directory's scandir-order children are claimed before the recursion
    # descends its sorted dirs). _visited drives the walk's dedup and ends
    # with the identical final contents; emitted is snapshot-copied so the
    # BFS claims cannot leak into the output ordering.
    emitted: set = set(_visited)
    scans: dict[tuple, dict | None] = {}
    archive_futs: list = []
    pdf_futs: list = []
    level = [(base, root_key)]
    with ThreadPoolExecutor(max_workers=_WALK_MAX_WORKERS) as pool:
        while level:
            walk_start = time.perf_counter()
            futs = [(key, pool.submit(_walk_dir, path)) for path, key in level]
            next_level: list[tuple[Path, tuple[int, int]]] = []
            for (path, key), (_key, fut) in zip(level, futs):
                rec = fut.result()  # OSError propagates like serial's iterdir
                if rec is None:
                    if path == base:
                        # Root listing failed — serial raises iterdir's
                        # OSError here (its exists() check already passed).
                        with os.scandir(path):
                            pass
                    scans[key] = None
                    continue
                scans[key] = rec
                if list_archives:
                    for aname in rec["archives"]:
                        archive_futs.append(
                            (rec, aname, pool.submit(
                                _timed_archive_node, path, aname, _downloads_root)))
                    for pname in rec["pdfs"]:
                        pdf_futs.append(
                            (rec, pname, pool.submit(
                                _timed_pdf_node, path, pname, _downloads_root)))
                for cname, cident in rec["dirs"]:
                    if cident in _visited:
                        continue
                    _visited.add(cident)
                    next_level.append((path / cname, cident))
            _phases["walk_ms"] += (time.perf_counter() - walk_start) * 1000
            level = next_level
        # Archive/pdf enumeration overlapped the walk tail on the same
        # pool; collect per-future spans exactly like the serial fan-out.
        for rec, aname, fut in archive_futs:
            node, span_ms = fut.result()
            rec["archive_nodes"][aname] = node
            _phases["archive_ms"] += span_ms
        for rec, pname, fut in pdf_futs:
            node, span_ms = fut.result()
            rec["pdf_nodes"][pname] = node
            _phases["pdf_ms"] += span_ms

    def _assemble(key) -> list[dict]:
        """Build one directory's entry list from its collected scan record.

        Reproduces _scan_tree_serial's per-directory node construction
        verbatim — files→archives→pdfs→dirs append order, the scandir-order
        claim filter feeding the natural_key dir sort, and single-child
        flattening — so the emitted tree is byte-identical.
        """
        rec = scans[key]
        if rec is None:
            return []
        files = rec["files"]
        archives = rec["archives"]
        pdfs = rec["pdfs"]
        entries: list[dict] = []
        for fname in files:
            entries.append({"name": fname, "type": "image", "children": None})
        archive_nodes = rec["archive_nodes"]
        for aname in archives:
            if list_archives:
                entries.append(archive_nodes[aname])
            else:
                entries.append({"name": aname, "type": "folder", "children": [],
                                "thumb": None, "archive": True})
        pdf_nodes = rec["pdf_nodes"]
        for pname in pdfs:
            if list_archives:
                entries.append(pdf_nodes[pname])
            else:
                entries.append({"name": pname, "type": "folder", "children": [],
                                "thumb": None, "archive": True, "pdf": True})
        pairs: list[tuple[str, tuple[int, int]]] = []
        for cname, cident in rec["dirs"]:
            if cident in emitted:
                continue
            emitted.add(cident)
            pairs.append((cname, cident))
        pairs.sort(key=lambda t: (natural_key(t[0]), t[0]))
        for dname, cident in pairs:
            child = _assemble(cident)
            if not files and not archives and not pdfs and len(pairs) == 1:
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

    return _assemble(root_key)


def _scan_tree_serial(base: Path, _visited: set[tuple[int, int]] | None = None,
                      _phases: dict[str, float] | None = None,
                      _downloads_root=None, list_archives: bool = True) -> list[dict]:
    """Serial ``scan_tree`` kept verbatim as the byte-parity reference (#242).

    This is the pre-parallelization implementation: the same recursion,
    the same claim order, the same node construction. ``scan_tree``'s
    parallel BFS + assembly must produce an identical tree; tests pin that
    equivalence on fixture trees (including symlink cycles and cross-depth
    inode aliases, where BFS and DFS claim order diverge).
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

    dirs.sort(key=lambda d: (natural_key(d), d))
    files.sort(key=lambda f: (_sort_key(f), f))
    archives.sort(key=lambda a: (natural_key(a), a))
    pdfs.sort(key=lambda a: (natural_key(a), a))
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
        child = _scan_tree_serial(base / dname, _visited, _phases, _downloads_root,
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
    files.sort(key=lambda f: (_sort_key(f), f))
    return files
