"""Archive works — ZIP/CBZ, RAR/CBR and EPUB as virtual folders.

An archive file is exposed by ``scan_tree`` as a ``folder`` node whose children
are its image entries; ``/image/<archive>/<inner>`` serves one entry on demand.
Nothing here ever raises for a bad archive: listing yields ``[]`` and reading
yields ``None`` so the API renders an empty message / 404 instead of a 500.
"""

import logging
import os
import posixpath
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

from app.scanner import IMAGE_EXTENSIONS, folder_cover_fields, natural_key
from app import warmup_cache

logger = logging.getLogger(__name__)

# ── native archive readers (Phase 1 W2, issue #163) ──────────────────────────
# bookq_rs (rust/bookq_rs) re-implements the listing/read paths of zipfile and
# rarfile byte-for-byte: EOCD/ZIP64/SFX handling, cp437/UTF-8 name decoding and
# the 0x7075 unicode-name extra for ZIP; RAR3 (incl. the compressed-UTF16
# UnicodeFilename scheme) and RAR5 header parsing for RAR. The Rust functions
# return ``None`` whenever they cannot *prove* the Python implementation would
# produce the identical result — the wrappers then run the reference code
# below, so behaviour is unchanged in every uncertain case. ``BOOKQ_RUST=0``
# forces the Python path; extraction still goes through the external tool.
try:
    import bookq_rs as _rs
except ImportError:
    _rs = None
if _rs is not None and os.environ.get("BOOKQ_RUST", "1") == "0":
    _rs = None

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


def _py_zip_names(path: Path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as zf:
            return [info.filename for info in zf.infolist()]
    except Exception:
        return []


def _zip_names(path: Path) -> list[str]:
    if _rs is not None:
        try:
            got = _rs.zip_names(str(path))
        except Exception:
            got = None
        if got is not None:
            return got
    return _py_zip_names(path)


def _py_rar_names(path: Path) -> list[str]:
    try:
        import rarfile

        with rarfile.RarFile(str(path)) as rf:
            return [info.filename for info in rf.infolist() if not info.is_dir()]
    except Exception:
        return []


def _rar_names(path: Path) -> list[str]:
    if _rs is not None:
        try:
            got = _rs.rar_names(str(path))
        except Exception:
            got = None
        if got is not None:
            # (filename, is_dir) pairs in header order → same as
            # ``infolist() if not info.is_dir()``
            return [name for name, is_dir in got if not is_dir]
    return _py_rar_names(path)


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


def _epub_from_parts(raw: list[str], spine: list[str]) -> list[str]:
    """Compose the finalized listing from raw zip names + spine-resolved names."""
    present = set(raw)
    ordered = _finalize([n for n in spine if n in present], keep_order=True)
    if ordered:
        return ordered
    return _finalize(raw)


def _py_epub_entries(path: Path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as zf:
            raw = [info.filename for info in zf.infolist()]
            try:
                spine = _epub_spine_images(zf)
            except Exception:
                spine = []
    except Exception:
        return []
    return _epub_from_parts(raw, spine)


def _epub_entries(path: Path) -> list[str]:
    if _rs is not None:
        try:
            got = _rs.epub_raw(str(path))
        except Exception:
            got = None
        if got is not None:
            raw, spine = got
            return _epub_from_parts(raw, spine)
    return _py_epub_entries(path)


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


def _py_read_zip(path: Path, inner: str) -> bytes | None:
    try:
        with zipfile.ZipFile(path) as zf:
            if inner not in zf.namelist():
                return None
            with zf.open(inner) as fp:
                return fp.read()
    except Exception:
        return None


def _read_zip(path: Path, inner: str) -> bytes | None:
    if _rs is not None:
        try:
            got = _rs.zip_read_entry(str(path), inner)
        except Exception:
            got = None
        if got is not None:
            return got
    return _py_read_zip(path, inner)


def _read_rar(path: Path, inner: str) -> bytes | None:
    try:
        import rarfile
    except ImportError as exc:
        raise ArchiveToolMissing("RAR support requires the rarfile package") from exc
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
        try:
            names = rf.namelist()
        except Exception as exc:
            logger.debug("RAR list failed for %s: %r", path, exc)
            return None
        if inner not in names:
            return None
        try:
            return rf.read(inner)
        except rarfile.RarCannotExec as exc:
            raise ArchiveToolMissing(
                "RAR extraction requires 7-Zip (7z) or unrar on PATH"
            ) from exc
        except Exception as exc:
            logger.warning("RAR extraction failed for %s!%s: %r", path, inner, exc)
            return None


def read_archive_entry(archive_path: Path, inner: str) -> bytes | None:
    """Return the raw bytes of one image entry, or ``None`` if it cannot be served.

    Raises :class:`ArchiveToolMissing` only when the format is recognised but
    the external extractor is absent — callers turn that into HTTP 501.
    """
    if not _is_image_entry(inner):
        return None
    if archive_path.suffix.lower() in RAR_EXTENSIONS:
        return _read_rar(archive_path, inner)
    return _read_zip(archive_path, inner)
