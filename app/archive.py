"""Archive works — ZIP/CBZ, RAR/CBR and EPUB as virtual folders.

An archive file is exposed by ``scan_tree`` as a ``folder`` node whose children
are its image entries; ``/image/<archive>/<inner>`` serves one entry on demand.
Nothing here ever raises for a bad archive: listing yields ``[]`` and reading
yields ``None`` so the API renders an empty message / 404 instead of a 500.
"""

import hashlib
import logging
import os
import posixpath
import shutil
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections import OrderedDict
from pathlib import Path
from urllib.parse import unquote, urlsplit

from app.scanner import IMAGE_EXTENSIONS, folder_cover_fields, natural_key
from app import warmup_cache

logger = logging.getLogger(__name__)

RAR_EXTENSIONS = {".rar", ".cbr"}
EPUB_EXTENSIONS = {".epub"}
ARCHIVE_EXTENSIONS = {".zip", ".cbz"} | RAR_EXTENSIONS | EPUB_EXTENSIONS

_NS_CONTAINER = "{urn:oasis:names:tc:opendocument:xmlns:container}"
_NS_OPF = "{http://www.idpf.org/2007/opf}"
_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"

# Zip-bomb / memory guard: never enumerate more than this many entries.
MAX_ARCHIVE_ENTRIES = 2000

# Traversal guard: entries containing these segments are never exposed.
_UNSAFE_PARTS = {"", ".", ".."}


def _is_safe_entry(name: str) -> bool:
    """Reject absolute paths, drive letters, and dot-dot traversal segments."""
    if not name or name.startswith(("/", "\\")):
        return False
    if len(name) >= 2 and name[1] == ":":
        return False
    parts = name.replace("\\", "/").split("/")
    if any(p in _UNSAFE_PARTS for p in parts):
        return False
    return True


def _is_image_entry(name: str) -> bool:
    """True if an archive inner path is an exposable image (no dirs, dotfiles, junk)."""
    if not _is_safe_entry(name):
        return False
    if name.endswith("/"):
        return False
    base = name.rsplit("/", 1)[-1]
    if not base or base.startswith("."):
        return False
    if name.startswith("__MACOSX/") or "/__MACOSX/" in name:
        return False
    return Path(base).suffix.lower() in IMAGE_EXTENSIONS


class ArchiveToolMissing(Exception):
    """The archive format needs an external tool that is not installed."""


def is_archive_file(path: Path) -> bool:
    """True if *path* is an existing archive file we can open as a work."""
    return path.is_file() and path.suffix.lower() in ARCHIVE_EXTENSIONS


def _finalize(names: list[str], *, keep_order: bool = False) -> list[str]:
    """Shared tail of every lister: image filter, de-dup (first wins), sort, cap."""
    seen: set[str] = set()
    unique: list[str] = []
    for n in names:
        if n not in seen and _is_image_entry(n):
            seen.add(n)
            unique.append(n)
    if not keep_order:
        unique.sort(key=lambda n: (natural_key(n), n))
    return unique[:MAX_ARCHIVE_ENTRIES]


def split_archive_entries(entries: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """Split finalized image entry names into root-loose names and top-dir groups.

    Returns ``(loose, groups)`` where *loose* holds entries without a ``/``
    and *groups* maps each distinct top-level directory to its entries with
    the top segment stripped (``v01/sub/a.png`` → ``sub/a.png``), so the
    names stay servable under ``/image/<archive>/<top>/<rest>``. Order
    within each list follows *entries* order. Callers split only when
    ``len(groups) >= 2``; anything else stays flat.
    """
    loose: list[str] = []
    groups: dict[str, list[str]] = {}
    for name in entries:
        top, sep, rest = name.partition("/")
        if not sep:
            loose.append(name)
        else:
            groups.setdefault(top, []).append(rest)
    return loose, groups


# Nesting guard for build_archive_tree: at most this many folder levels are
# materialised as nested folder nodes. A deeper remainder is joined with "/"
# into a single leaf name, which stays servable via _resolve_archive (the
# inner entry string is preserved verbatim, exactly like today's flat
# "ch1/1.png" leaves).
_ARCHIVE_TREE_MAX_DEPTH = 32


def build_archive_tree(entries: list[str]) -> list[dict]:
    """Build a nested folder tree from finalized archive image entry names.

    Each node matches the ``scan_tree`` shape: image leaves are
    ``{"name", "type": "image", "children": None}`` and folders are
    ``{"name", "type": "folder", "children": [...], "thumb": ...}`` with an
    optional ``"thumbs"`` (up to 9 ``"<child>/<child.thumb>"`` paths, only
    when 2+ immediate sub-folders bear images). No node ever carries an
    ``"archive"`` key — the scanner sets ``archive: True`` on the outer
    root only.

    Rules (mirroring ``scan_tree``):

    - Loose entries (no ``/``) come first, then sub-folders, at every
      level; each level is re-sorted by ``(natural_key, name)``.
    - Folder ``thumb``/``thumbs`` come from ``scanner.folder_cover_fields``
      (the shared folder-cover rule): ``thumb`` is the first image below the
      folder; imageless folders carry ``thumb=None`` and are excluded from the
      parent's ``thumbs`` (same guard as ``scanner.py``).
    - Iterative with a depth cap of 32: deeper remainders collapse into a
      single ``/``-joined leaf name (still servable as one inner entry).
    - Input is assumed pre-filtered (``_finalize`` output). Unsafe or
      directory-like names are skipped; anything else never raises —
      ``[]`` yields ``[]``.
    """
    if not entries:
        return []

    class _Dir:
        __slots__ = ("files", "file_set", "dirs")

        def __init__(self) -> None:
            self.files: list[str] = []
            self.file_set: set[str] = set()
            self.dirs: dict[str, "_Dir"] = {}

    root = _Dir()
    for name in entries:
        if not isinstance(name, str) or not name or name.endswith("/"):
            continue
        parts = name.split("/")
        if any(p in _UNSAFE_PARTS for p in parts):
            continue
        if len(parts) - 1 > _ARCHIVE_TREE_MAX_DEPTH:
            parts = parts[:_ARCHIVE_TREE_MAX_DEPTH] + [
                "/".join(parts[_ARCHIVE_TREE_MAX_DEPTH:])
            ]
        node = root
        for part in parts[:-1]:
            child = node.dirs.get(part)
            if child is None:
                child = _Dir()
                node.dirs[part] = child
            node = child
        leaf = parts[-1]
        if leaf and leaf not in node.file_set:
            node.file_set.add(leaf)
            node.files.append(leaf)

    # Post-order without recursion: BFS order has every parent before its
    # child, so processing the collected nodes in reverse builds thumbs
    # bottom-up (depth <= 32, but no stack growth either way).
    bfs: list[_Dir] = [root]
    for node in bfs:
        bfs.extend(node.dirs.values())

    built: dict[int, list[dict]] = {}

    def _build_children(node: _Dir) -> list[dict]:
        files = sorted(node.files, key=lambda n: (natural_key(n), n))
        children: list[dict] = [
            {"name": n, "type": "image", "children": None} for n in files
        ]
        for dirname in sorted(node.dirs, key=lambda d: (natural_key(d), d)):
            child = node.dirs[dirname]
            sub = built[id(child)]
            # One shared rule for the level's cover fields (scanner's
            # folder_cover_fields): the child's own thumb AND its multi-thumb
            # grid both come from its already-built children.
            thumb, sub_thumbs = folder_cover_fields(sub)
            entry: dict = {
                "name": dirname,
                "type": "folder",
                "children": sub,
                "thumb": thumb,
            }
            if sub_thumbs is not None:
                entry["thumbs"] = sub_thumbs
            children.append(entry)
        return children

    for node in reversed(bfs):
        built[id(node)] = _build_children(node)

    return built[id(root)]


def _zip_names_uncached(path: Path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as zf:
            return [info.filename for info in zf.infolist()]
    except Exception:
        return []


def _rar_names_uncached(path: Path) -> list[str]:
    try:
        import rarfile

        with rarfile.RarFile(str(path)) as rf:
            return [info.filename for info in rf.infolist() if not info.is_dir()]
    except Exception:
        return []


def _epub_resolve(base_dir: str, href: str) -> str:
    """Resolve an (possibly percent-encoded) href relative to *base_dir* into a zip name."""
    rel = unquote(urlsplit(href).path)
    if not rel:
        return ""
    joined = posixpath.join(base_dir, rel) if base_dir else rel
    return posixpath.normpath(joined)


def _epub_spine_images(zf: zipfile.ZipFile) -> list[str]:
    """Image zip names in spine order (document order within each page).

    Raises on a missing/invalid container.xml or OPF — the caller falls back
    to natural order. A single unreadable page document is skipped.
    """
    container = ET.fromstring(zf.read("META-INF/container.xml"))
    rootfile = container.find(f".//{_NS_CONTAINER}rootfile")
    if rootfile is None or not rootfile.get("full-path"):
        raise ValueError("EPUB container has no rootfile")
    opf_path = posixpath.normpath(rootfile.get("full-path"))
    opf_dir = posixpath.dirname(opf_path)
    opf = ET.fromstring(zf.read(opf_path))
    manifest = {
        item.get("id"): item.get("href")
        for item in opf.iter(f"{_NS_OPF}item")
        if item.get("id") and item.get("href")
    }
    out: list[str] = []
    for ref in opf.iter(f"{_NS_OPF}itemref"):
        if len(out) >= MAX_ARCHIVE_ENTRIES:
            break
        href = manifest.get(ref.get("idref") or "")
        if not href:
            continue
        doc_path = _epub_resolve(opf_dir, href)
        try:
            root = ET.fromstring(zf.read(doc_path))
        except Exception:
            continue
        doc_dir = posixpath.dirname(doc_path)
        for el in root.iter():
            tag = el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""
            if tag == "img":
                src = el.get("src")
            elif tag == "image":
                src = el.get(_XLINK_HREF) or el.get("href")
            else:
                continue
            if src:
                out.append(_epub_resolve(doc_dir, src))
    return out


def _epub_names_from_zf(zf: zipfile.ZipFile) -> tuple[list[str], list[str]]:
    """(raw infolist names, spine-resolved names) from an open epub ZipFile.

    A spine-level failure yields an empty spine list — the caller falls back
    to natural order over *raw*, exactly like the uncached path.
    """
    raw = [info.filename for info in zf.infolist()]
    try:
        spine = _epub_spine_images(zf)
    except Exception:
        spine = []
    return raw, spine


def _epub_finalize(raw: list[str], spine: list[str]) -> list[str]:
    present = set(raw)
    ordered = _finalize([n for n in spine if n in present], keep_order=True)
    if ordered:
        return ordered
    return _finalize(raw)


def _epub_entries_uncached(path: Path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as zf:
            raw, spine = _epub_names_from_zf(zf)
    except Exception:
        return []
    return _epub_finalize(raw, spine)


def list_archive_entries(archive_path: Path, downloads_root=None) -> list[str]:
    """Return image entry names inside an archive, in reading order.

    ZIP/CBZ and RAR/CBR entries are naturally sorted; EPUB entries follow the
    OPF spine (falling back to natural order when the package is unreadable).
    Unsafe entries are skipped. An unreadable or empty archive yields ``[]``
    — never raises.

    *downloads_root* selects the disk-cache key root explicitly (scan_tree
    passes its top-level base so keys stay ``r:``-relative even when
    ``DOWNLOADS_DIR`` is unset in the container). None (default) keeps the
    current env-lookup behavior via ``cache_key_for_path``; all existing
    callers work unchanged.
    """
    # Restart-spanning disk cache in front of the live readers (fail-open:
    # any cache error falls through to the live read below). At-cap
    # listings are stored with truncated=True (#260) so oversized archives
    # stay cacheable across restarts; the stored bytes are exactly what a
    # live capped read serves, so the flag is informational only. The hit
    # is re-sliced defensively: never serve more than the cap, even from
    # a foreign/hand-edited cache file.
    disk_key = None
    try:
        if downloads_root is not None:
            disk_key = warmup_cache.cache_key(downloads_root, archive_path)
        else:
            disk_key = warmup_cache.cache_key_for_path(archive_path)
        if disk_key is not None:
            hit = warmup_cache.get_archive_entries(disk_key)
            if hit is not None:
                return hit[:MAX_ARCHIVE_ENTRIES]
    except Exception:
        disk_key = None
    suffix = archive_path.suffix.lower()
    if suffix in RAR_EXTENSIONS:
        entries = _finalize(_rar_names(archive_path))
    elif suffix in EPUB_EXTENSIONS:
        entries = _epub_entries(archive_path)
    else:
        entries = _finalize(_zip_names(archive_path))
    try:
        if disk_key is not None:
            # _finalize already capped at MAX_ARCHIVE_ENTRIES, so reaching
            # the cap means the listing may have been cut (an exact-fit
            # listing is indistinguishable without a second pass, and the
            # flag changes nothing about the served bytes).
            warmup_cache.put_archive_entries(
                disk_key,
                entries,
                truncated=len(entries) >= MAX_ARCHIVE_ENTRIES,
            )
    except Exception:
        pass
    return entries


# ── open-archive memo (read_archive_entry + listing hot paths) ───────────
#
# Serving /image/<archive>/<entry> used to re-open the archive — re-parsing
# every header / the ZIP central directory / all RAR blocks — on every page
# request. A reading session issues one request per page against the same
# work, so open archive objects are memoized in a small LRU keyed by
# (resolved path, st_mtime_ns, st_size): a modified archive is a different
# key and gets re-opened, while its stale object ages out and is closed.
# #256 additionally serves ``list_archive_entries`` through the same memo
# (names/epub captured lazily on the entry), so a chapter's listing and
# page reads share one parse — the Rust `Parsed` design (fb1a469/c1c3e03).
#
# rarfile.RarFile and zipfile.ZipFile are NOT thread-safe (each entry open
# seeks one shared file object) and read_archive_entry runs under
# run_in_threadpool, so every cached object carries a private lock that
# serializes its reads; the LRU itself is guarded by _open_archives_lock.
# Eviction closes the object under its own lock — a thread that fetched the
# entry just before eviction sees ``obj is None`` and falls back to a
# direct one-shot open, so a closed file is never read through.
_OPEN_ARCHIVE_CACHE_MAX = 8


class _OpenArchive:
    """One cached open archive (zipfile.ZipFile / rarfile.RarFile) + its lock.

    ``obj`` becomes None once the entry has been evicted and closed; readers
    holding the entry then fall back to an uncached open.

    ``names``/``epub`` lazily capture the listing derivations so
    ``list_archive_entries`` rides the same open object (#256 — the Rust
    ``Parsed`` enum's stored names/spine): a chapter's listing and page
    reads share ONE open+parse instead of paying it per side. Both are
    computed under ``lock`` — the shared seekable object is not
    thread-safe, and ``_epub_names_from_zf`` issues many entry reads that
    must serialize against concurrent page reads.
    """

    __slots__ = ("obj", "lock", "names", "epub")

    def __init__(self, obj) -> None:
        self.obj = obj
        self.lock = threading.Lock()
        self.names: list[str] | None = None
        self.epub: tuple[list[str], list[str]] | None = None


_open_archives: "OrderedDict[tuple[str, int, int], _OpenArchive]" = OrderedDict()
_open_archives_lock = threading.Lock()


def _archive_cache_key(path: Path) -> tuple[str, int, int] | None:
    """LRU key: resolved path + (mtime, size) so an edited file re-opens."""
    try:
        st = path.stat()
    except (OSError, ValueError):
        return None
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError, ValueError):
        resolved = path
    return (str(resolved), st.st_mtime_ns, st.st_size)


def _close_open_archive(entry: _OpenArchive) -> None:
    """Close a cached archive object, serialized against in-flight readers."""
    with entry.lock:
        obj, entry.obj = entry.obj, None
    if obj is not None:
        try:
            obj.close()
        except Exception:
            pass


def _open_archive_entry(path: Path, open_fn) -> _OpenArchive | None:
    """Return the cached open archive for *path*, opening via *open_fn* on miss.

    ``None`` means "no cache key" (unstat'able path) — callers then do a
    direct one-shot open. *open_fn* exceptions propagate so callers keep
    classifying them exactly as before (rarfile.RarCannotExec →
    ArchiveToolMissing, anything else → None). Open failures are not
    cached: the next request retries.
    """
    key = _archive_cache_key(path)
    if key is None:
        return None
    with _open_archives_lock:
        hit = _open_archives.get(key)
        if hit is not None:
            _open_archives.move_to_end(key)
            return hit
    # Open outside the LRU lock: header parsing can be slow and must not
    # serialize unrelated archive traffic. If another thread won the race,
    # the loser object is closed unused.
    obj = open_fn(path)
    entry = _OpenArchive(obj)
    loser = evicted = None
    with _open_archives_lock:
        existing = _open_archives.get(key)
        if existing is not None:
            _open_archives.move_to_end(key)
            loser = entry
            entry = existing
        else:
            _open_archives[key] = entry
            if len(_open_archives) > _OPEN_ARCHIVE_CACHE_MAX:
                _, evicted = _open_archives.popitem(last=False)
    for e in (loser, evicted):
        if e is not None:
            _close_open_archive(e)
    return entry


def _clear_open_archive_cache() -> None:
    """Drop and close every cached open archive (test isolation)."""
    while True:
        with _open_archives_lock:
            if not _open_archives:
                return
            _, entry = _open_archives.popitem(last=False)
        _close_open_archive(entry)


# ── memo-backed listings (#256) ──────────────────────────────────────────
#
# ``list_archive_entries`` used to open a FRESH archive on every disk-cache
# miss even though the read path memoizes open archives — a chapter paid
# open+parse once for its listing and again for its first page. The Rust
# ``bookq_core::open_archive`` stores parsed names on the memo object and
# serves listings through it (viewer-rs fb1a469 / c1c3e03); these wrappers
# do the same: capture names/epub once on the ``_OpenArchive`` under its
# lock, fall back to the uncached one-shot functions on any cache
# unavailability (no key / evicted / open failure), never cache failures.


def _zip_names(path: Path) -> list[str]:
    try:
        entry = _open_archive_entry(path, zipfile.ZipFile)
    except Exception:
        return _zip_names_uncached(path)
    if entry is None:
        return _zip_names_uncached(path)
    with entry.lock:
        if entry.obj is None:
            return _zip_names_uncached(path)
        if entry.names is None:
            try:
                entry.names = [i.filename for i in entry.obj.infolist()]
            except Exception:
                return []
        return list(entry.names)


def _rar_names(path: Path) -> list[str]:
    try:
        import rarfile

        entry = _open_archive_entry(path, lambda p: rarfile.RarFile(str(p)))
    except Exception:
        return _rar_names_uncached(path)
    if entry is None:
        return _rar_names_uncached(path)
    with entry.lock:
        if entry.obj is None:
            return _rar_names_uncached(path)
        if entry.names is None:
            try:
                entry.names = [
                    i.filename for i in entry.obj.infolist() if not i.is_dir()
                ]
            except Exception:
                return []
        return list(entry.names)


def _epub_entries(path: Path) -> list[str]:
    try:
        entry = _open_archive_entry(path, zipfile.ZipFile)
    except Exception:
        return _epub_entries_uncached(path)
    if entry is None:
        return _epub_entries_uncached(path)
    with entry.lock:
        if entry.obj is None:
            return _epub_entries_uncached(path)
        if entry.epub is None:
            try:
                entry.epub = _epub_names_from_zf(entry.obj)
            except Exception:
                return []
        raw, spine = entry.epub
    # finalize per call (dedupe/cap/order), matching _epub_entries_uncached.
    return _epub_finalize(raw, spine)


# ── RAR sequential-extract tmp cache (issue #246) ────────────────────────
#
# RAR entries that need the external extractor used to spawn it once per
# page request. For a SOLID archive each entry n additionally re-decompresses
# 0..n internally, so reading a P-page chapter cost O(P²) of extractor work.
# Two tmp-file caches under ``thumbs.cache_root()/rar-ext/`` turn that into
# O(P):
#
# - Solid archives: on the first compressed/solid read, ONE extractor pass
#   (``7z x`` / ``unar -D`` / ``unrar x`` / ``bsdtar -x`` — whichever tool
#   rarfile itself resolved via ``tool_setup()``) unpacks the whole archive
#   into a staging dir; verified entries are then published atomically under
#   ``<dir>/x/<sanitized name>`` and later reads serve plain files.
# - Compressed non-solid entries: the first successful ``rf.read`` bytes are
#   stored at ``<dir>/s/<sha1(inner)>`` (unique tmp + ``os.replace``), so a
#   re-read never respawns.
# - Stored entries keep the in-process direct read untouched.
#
# Layout: ``<cache_root>/rar-ext/<sha1(resolved|mtime_ns|size)>/`` — an
# edited archive is a different digest, which is the invalidation story
# (mirrors the thumbs ``path|mtime|size`` key). Per-archive state
# (``_rar_ext_states``, LRU like ``_open_archives``) holds an "extraction
# attempted" flag so a failed extraction never retries: missing entries fall
# through to the legacy per-request ``rf.read``, i.e. never a regression vs
# today. Reads of one archive serialize on the memoized ``_OpenArchive``
# lock, so concurrent first reads run a single extraction; the uncached
# fallback serializes on the state's own lock instead.
#
# ``_prune_rar_ext`` mirrors ``thumbs.prune_cache_dir``'s lazy
# prune-back-under-cap regime, but the eviction unit is one archive-version
# directory (a whole extracted chapter), not one file.

_RAR_EXT_STATE_MAX = 8  # mirrors _OPEN_ARCHIVE_CACHE_MAX
# Max archive-version dirs kept under rar-ext/. Unlike the thumbs cap (5000
# *files*) one dir holds a whole chapter, so a small count bounds disk use.
_RAR_EXT_MAX_DIRS = 16
# Staging dirs older than this are crash leftovers — safe to reap.
_RAR_EXT_STALE_STAGE_SEC = 3600


class _RarExtState:
    """Per-archive-version extraction state: done flag + target dir."""

    __slots__ = ("lock", "ext_dir", "extracted")

    def __init__(self, ext_dir: Path) -> None:
        self.lock = threading.Lock()
        self.ext_dir = ext_dir
        # A preexisting dir means an extraction already ran for this exact
        # archive version (possibly a previous process) — never re-run.
        self.extracted = ext_dir.is_dir()


_rar_ext_states: "OrderedDict[tuple[str, int, int], _RarExtState]" = OrderedDict()
_rar_ext_states_lock = threading.Lock()


def _rar_ext_dir_from_key(key: tuple[str, int, int]) -> Path:
    """``<cache_root>/rar-ext/<sha1(resolved|mtime_ns|size)>`` for *key*."""
    digest = hashlib.sha1(
        f"{key[0]}|{key[1]}|{key[2]}".encode("utf-8")
    ).hexdigest()
    from app.thumbs import cache_root

    return cache_root() / "rar-ext" / digest


def _rar_ext_dir(path: Path) -> Path | None:
    key = _archive_cache_key(path)
    if key is None:
        return None
    return _rar_ext_dir_from_key(key)


def _rar_ext_state(path: Path) -> _RarExtState | None:
    """Return the extraction state for *path*, creating it on miss."""
    key = _archive_cache_key(path)
    if key is None:
        return None
    ext_dir = _rar_ext_dir_from_key(key)
    with _rar_ext_states_lock:
        st = _rar_ext_states.get(key)
        if st is None:
            st = _RarExtState(ext_dir)
            _rar_ext_states[key] = st
            if len(_rar_ext_states) > _RAR_EXT_STATE_MAX:
                _rar_ext_states.popitem(last=False)
        else:
            _rar_ext_states.move_to_end(key)
    return st


def _clear_rar_ext_state() -> None:
    """Drop in-memory extraction states (test isolation; disk cache stays)."""
    with _rar_ext_states_lock:
        _rar_ext_states.clear()


def _rar_read_mode(rf, info, rarfile) -> str:
    """Classify a RAR entry: ``solid`` / ``compressed`` / ``stored``.

    Solid wins over stored: when the archive-level flag (or the entry's own
    solid bit) is set, whole-archive extraction is the strategy for every
    entry — in a real solid archive all entries are compressed anyway, and a
    stored entry inside one extracts just as correctly. ``compress_type`` is
    normalized across RAR3/RAR5 (``RAR_M0`` = stored); ``RAR_FILE_SOLID`` is
    the normalized per-entry bit (RAR5 ``RAR5_COMPR_SOLID`` maps onto it).
    """
    try:
        if rf.is_solid() or (info.flags & rarfile.RAR_FILE_SOLID):
            return "solid"
    except Exception:
        pass
    if getattr(info, "compress_type", None) == rarfile.RAR_M0:
        return "stored"
    return "compressed"


def _rar_extract_all_cmdline(rarfile, setup, archive: str, destdir: str) -> list[str] | None:
    """Extract-everything-to-*destdir* argv for the SAME tool rarfile picked.

    ``setup.setup["open_cmd"][0]`` names the module global holding the tool
    executable (e.g. ``"SEVENZIP_TOOL"`` → ``rarfile.SEVENZIP_TOOL``); each
    tool's ``x``/extract verb keeps inner paths. The ``no_password`` args
    mirror rarfile's tool configs (our archives carry no passwords — a
    password-flagged entry fails here and falls back like today). ``None``
    means "unknown tool" → caller skips extraction and falls back.
    """
    tool = setup.setup["open_cmd"][0]
    exe = getattr(rarfile, tool, None)
    if exe is None:
        return None
    if tool == "UNRAR_TOOL":
        # `x` = extract with full paths; trailing sep marks destdir as a dir.
        return [exe, "x", "-o+", "-inul", "-p-", "--", archive, destdir + os.sep]
    if tool == "UNAR_TOOL":
        # -D: never wrap output in an archive-named dir (deterministic layout);
        # -nr: don't recurse into nested archives.
        return [exe, "-D", "-nr", "-f", "-q", "-o", destdir, "-p", "", "--", archive]
    if tool in ("SEVENZIP_TOOL", "SEVENZIP2_TOOL"):
        return [exe, "x", "-y", "-aoa", "-bb0", "-p", f"-o{destdir}", "--", archive]
    if tool == "BSDTAR_TOOL":
        # bsdtar cannot do solid archives; a failure is fail-open (fallback).
        return [exe, "-x", "-f", archive, "-C", destdir]
    return None


def _rar_verified_extract(info, cand: Path) -> bool:
    """True when extracted *cand* provably matches *info* (size, then CRC32).

    CRC is verified only "where available": RAR5 entries may carry Blake2sp
    instead of CRC32 (``blake2sp_hash`` set, ``CRC`` None) and stdlib has no
    blake2sp — size is the check there. A successful tool already CRC-checked
    on its side; this guards partial output (never serve a truncated page).
    """
    try:
        if not cand.is_file() or cand.stat().st_size != info.file_size:
            return False
        crc = getattr(info, "CRC", None)
        if crc is None or getattr(info, "blake2sp_hash", None) is not None:
            return True
        return (zlib.crc32(cand.read_bytes()) & 0xFFFFFFFF) == (crc & 0xFFFFFFFF)
    except OSError:
        return False


def _rar_publish_extracted(rf, stage: Path, ext_dir: Path, rarfile) -> None:
    """Publish verified entries from *stage* into ``<ext_dir>/x/`` atomically.

    Only entries the archive itself lists (``is_file``, ``_is_image_entry``,
    no link/redir placeholders) are candidates; each must pass the
    size/CRC check and a path-containment re-check (a symlink member inside
    the archive could otherwise point a candidate outside *stage*).
    Publishing is per file via ``os.replace`` — a racing reader either sees
    the complete file or nothing, and partial extraction (tool died midway)
    still publishes whatever landed verified.
    """
    try:
        infos = rf.infolist()
    except Exception:
        return
    try:
        stage_root = stage.resolve()
    except OSError:
        return
    for info in infos:
        try:
            if not info.is_file() or getattr(info, "file_redir", None) is not None:
                continue
            name = info.filename
            if not _is_image_entry(name):
                continue
            rel = rarfile.sanitize_filename(name, os.path.sep, rarfile.WIN32)
            if not rel:
                continue
            cand = None
            for relname in (rel, name):
                probe = stage / relname
                try:
                    resolved = probe.resolve()
                except OSError:
                    continue
                if resolved != stage_root and str(resolved).startswith(
                    str(stage_root) + os.sep
                ) and probe.is_file():
                    cand = probe
                    break
            if cand is None or not _rar_verified_extract(info, cand):
                continue
            dest = ext_dir / "x" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(cand, dest)
        except Exception:
            continue


def _rar_extract_all(rf, path: Path, st: _RarExtState, rarfile) -> None:
    """Run ONE extractor pass unpacking *path* into a staging dir, then publish.

    Sets ``st.extracted`` first: extraction is attempted at most once per
    archive version — on any failure the missing entries keep the legacy
    per-request ``rf.read`` path. Never raises: a missing tool just means no
    tmp files (the fallback then raises ArchiveToolMissing from ``rf.read``
    exactly as before — and stored entries still serve in-process).
    """
    st.extracted = True
    try:
        setup = rarfile.tool_setup()
    except Exception as exc:
        logger.debug("RAR extract-all skipped for %s: %r", path, exc)
        return
    stage = st.ext_dir.parent / (
        f"{st.ext_dir.name}.part-{os.getpid()}.{os.urandom(6).hex()}"
    )
    try:
        stage.mkdir(parents=True)
    except OSError:
        return
    try:
        cmd = _rar_extract_all_cmdline(rarfile, setup, str(path), str(stage))
        if cmd is not None:
            try:
                proc = rarfile.custom_popen(cmd)
                out, _ = proc.communicate()
            except Exception as exc:
                logger.warning("RAR extract-all failed for %s: %r", path, exc)
            else:
                if proc.returncode != 0:
                    logger.warning(
                        "RAR extract-all rc=%s for %s: %r",
                        proc.returncode,
                        path,
                        out[-300:],
                    )
        _rar_publish_extracted(rf, stage, st.ext_dir, rarfile)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    _prune_rar_ext(st.ext_dir.parent)


def _rar_extracted_file(ext_dir: Path, name: str, rarfile) -> Path | None:
    """Published tmp file for archive entry *name*, or None (miss/unsafe)."""
    try:
        rel = rarfile.sanitize_filename(name, os.path.sep, rarfile.WIN32)
    except Exception:
        return None
    if not rel:
        return None
    dest = ext_dir / "x" / rel
    try:
        root = (ext_dir / "x").resolve()
        resolved = dest.resolve()
        if resolved != root and not str(resolved).startswith(str(root) + os.sep):
            return None
        return dest if dest.is_file() else None
    except OSError:
        return None


def _touch_rar_ext(ext_dir: Path) -> None:
    """Refresh dir mtime so a hot archive isn't the first prune victim."""
    try:
        os.utime(ext_dir)
    except OSError:
        pass


def _rar_solid_entry_bytes(rf, info, path: Path, rarfile) -> bytes | None:
    """Serve a solid-archive entry from the extract-all tmp dir, or None."""
    st = _rar_ext_state(path)
    if st is None:
        return None
    if not st.extracted:
        with st.lock:
            if not st.extracted:
                _rar_extract_all(rf, path, st, rarfile)
    dest = _rar_extracted_file(st.ext_dir, info.filename, rarfile)
    if dest is None:
        return None
    try:
        data = dest.read_bytes()
    except OSError:
        return None
    _touch_rar_ext(st.ext_dir)
    return data


def _rar_single_entry_bytes(path: Path, inner: str) -> bytes | None:
    """Serve a compressed non-solid entry from its per-entry tmp file."""
    ext_dir = _rar_ext_dir(path)
    if ext_dir is None:
        return None
    dest = ext_dir / "s" / hashlib.sha1(inner.encode("utf-8")).hexdigest()
    try:
        if not dest.is_file():
            return None
        data = dest.read_bytes()
    except OSError:
        return None
    _touch_rar_ext(ext_dir)
    return data


def _rar_store_single_entry(path: Path, inner: str, data: bytes) -> None:
    """Persist one extracted entry as ``<ext_dir>/s/<sha1(inner)>`` (fail-open).

    Unique sibling tmp + ``os.replace`` so a half-written page is never
    published; mirrors ``thumbs._store_thumb``'s write pattern.
    """
    ext_dir = _rar_ext_dir(path)
    if ext_dir is None:
        return
    dest = ext_dir / "s" / hashlib.sha1(inner.encode("utf-8")).hexdigest()
    tmp = None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.parent / f".{dest.name}.{os.getpid()}.{os.urandom(6).hex()}.tmp"
        tmp.write_bytes(data)
        tmp.replace(dest)
    except OSError:
        return
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
    _prune_rar_ext(ext_dir.parent)


def _prune_rar_ext(namespace_dir: Path, cap: int | None = None) -> None:
    """Prune *namespace_dir* (``rar-ext/``) back under *cap* archive dirs.

    Mirrors ``thumbs.prune_cache_dir`` (lazy, oldest-mtime-first, fail-open)
    with two differences: the eviction unit is a DIRECTORY (one extracted
    archive version, removed via rmtree) and stale ``*.part-*`` staging dirs
    older than ``_RAR_EXT_STALE_STAGE_SEC`` are crash leftovers reaped on
    sight. Read at call time so a monkeypatched cap takes effect.
    """
    if cap is None:
        cap = _RAR_EXT_MAX_DIRS
    try:
        entries = [p for p in namespace_dir.iterdir() if p.is_dir()]
    except Exception:
        return
    now = time.time()
    dirs: list[Path] = []
    for p in entries:
        if ".part-" in p.name:
            try:
                stale = now - p.stat().st_mtime > _RAR_EXT_STALE_STAGE_SEC
            except OSError:
                stale = False
            if stale:
                shutil.rmtree(p, ignore_errors=True)
            continue
        dirs.append(p)
    if len(dirs) <= cap:
        return
    try:
        dirs.sort(key=lambda p: p.stat().st_mtime)
    except Exception:
        return
    for stale in dirs[: len(dirs) - cap]:
        shutil.rmtree(stale, ignore_errors=True)


def _zip_entry_bytes(zf: zipfile.ZipFile, inner: str) -> bytes | None:
    try:
        info = zf.getinfo(inner)
    except KeyError:
        return None
    with zf.open(info) as fp:
        return fp.read()


def _read_zip_uncached(path: Path, inner: str) -> bytes | None:
    try:
        with zipfile.ZipFile(path) as zf:
            return _zip_entry_bytes(zf, inner)
    except Exception:
        return None


def _read_zip(path: Path, inner: str) -> bytes | None:
    try:
        entry = _open_archive_entry(path, zipfile.ZipFile)
    except Exception:
        return None
    if entry is None:
        return _read_zip_uncached(path, inner)
    with entry.lock:
        if entry.obj is None:
            return _read_zip_uncached(path, inner)
        try:
            return _zip_entry_bytes(entry.obj, inner)
        except Exception:
            return None


def _rar_entry_bytes(rf, inner: str, path: Path, rarfile) -> bytes | None:
    """Read *inner* from an open RarFile. Raises ArchiveToolMissing when the
    external extractor is absent; every other failure logs and yields None.

    Solid/compressed entries first consult the rar-ext tmp cache (#246):
    a hit serves plain file bytes, a miss falls through to the legacy
    ``rf.read`` below — never a regression vs the pre-cache behavior.
    """
    try:
        info = rf.getinfo(inner)
    except rarfile.NoRarEntry:
        return None
    except Exception as exc:
        logger.debug("RAR list failed for %s: %r", path, exc)
        return None
    mode = _rar_read_mode(rf, info, rarfile)
    if mode == "solid":
        data = _rar_solid_entry_bytes(rf, info, path, rarfile)
        if data is not None:
            return data
    elif mode == "compressed":
        data = _rar_single_entry_bytes(path, inner)
        if data is not None:
            return data
    try:
        data = rf.read(info)
    except rarfile.RarCannotExec as exc:
        raise ArchiveToolMissing(
            "RAR extraction requires 7-Zip (7z) or unrar on PATH"
        ) from exc
    except Exception as exc:
        logger.warning("RAR extraction failed for %s!%s: %r", path, inner, exc)
        return None
    if data is not None and mode == "compressed":
        _rar_store_single_entry(path, inner, data)
    return data


def _read_rar_uncached(path: Path, inner: str, rarfile) -> bytes | None:
    try:
        rf = rarfile.RarFile(str(path))
    except rarfile.RarCannotExec as exc:
        raise ArchiveToolMissing(
            "RAR extraction requires 7-Zip (7z) or unrar on PATH"
        ) from exc
    except Exception as exc:
        logger.debug("RAR open failed for %s: %r", path, exc)
        return None
    with rf:
        return _rar_entry_bytes(rf, inner, path, rarfile)


def _read_rar(path: Path, inner: str) -> bytes | None:
    try:
        import rarfile
    except ImportError as exc:
        raise ArchiveToolMissing("RAR support requires the rarfile package") from exc
    try:
        entry = _open_archive_entry(path, lambda p: rarfile.RarFile(str(p)))
    except rarfile.RarCannotExec as exc:
        raise ArchiveToolMissing(
            "RAR extraction requires 7-Zip (7z) or unrar on PATH"
        ) from exc
    except Exception as exc:
        logger.debug("RAR open failed for %s: %r", path, exc)
        return None
    if entry is None:
        return _read_rar_uncached(path, inner, rarfile)
    with entry.lock:
        if entry.obj is None:
            return _read_rar_uncached(path, inner, rarfile)
        return _rar_entry_bytes(entry.obj, inner, path, rarfile)


def read_archive_entry(archive_path: Path, inner: str) -> bytes | None:
    """Return the raw bytes of one image entry, or ``None`` if it cannot be served.

    Open archives are memoized across requests (see ``_open_archive_entry``)
    so repeat page reads skip the full header re-parse; .zip/.cbz/.epub go
    through ``_read_zip`` (EPUB is a ZIP) and .rar/.cbr through ``_read_rar``.

    Raises :class:`ArchiveToolMissing` only when the format is recognised but
    the external extractor is absent — callers turn that into HTTP 501.
    """
    if not _is_image_entry(inner):
        return None
    if archive_path.suffix.lower() in RAR_EXTENSIONS:
        return _read_rar(archive_path, inner)
    return _read_zip(archive_path, inner)
