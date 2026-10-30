"""Filesystem tree signature for top-page cache gating.

``compute_signature`` returns a stable sha256 hexdigest over the cheap walk
terms ``scan_tree`` already pays for (per visited directory: ``mtime_ns`` +
the sorted entry-name set) plus the two terms a dir-mtime walk alone would
miss:

- per archive/PDF file (suffix lowercased before ``ARCHIVE_EXTENSIONS`` /
  ``PDF_EXTENSIONS`` membership): ``mtime_ns`` + size;
- per visited directory: ``metadata.toml`` ``mtime_ns`` + size when present
  (in-place rewrites never bump the parent dir mtime, so without this term
  metadata-only edits go stale forever).

Composition is per-directory: each directory folds its own local terms
(``\\x00d`` dir mtime + per entry in sorted-name order ``\\x00n`` name plus
``\\x00f``/``\\x00t`` mtime+size for archives/PDFs/metadata.toml) into a
local byte string, and the final digest combines bottom-up as
``sha256(local(dir) || name + digest(child) for children in sorted-name
order)`` with each child digest as raw bytes and the bare name (no tag).
The VALUE therefore differs from the pre-parallel streaming fold; that is
safe because signatures are compared only against the in-memory
``cache_data["signature"]`` and never persisted.

The walk itself is level-synchronous parallel: ONE ``ThreadPoolExecutor``
per call scans each BFS level's directories concurrently (no nested
submissions — deadlock freedom), while the MAIN thread alone owns the
``(st_dev, st_ino)`` visited set (no locks) and expands the next level.
Each entry is classified via its cached ``d_type`` (``is_symlink`` first, then
``is_dir``/``is_file`` — no syscall for known types) and ``entry.stat()``
runs ONLY where its result feeds a term or an identity: symlinks (one
follow-stat), subdirs (mtime/ino), archives/PDFs/metadata.toml (mtime+size).
Plain images hash their NAME ONLY with zero stat calls.

Dot-names are skipped. Directories are visited once by ``(st_dev, st_ino)``
identity — ``stat()`` follows symlinks (never ``lstat``), mirroring
``scan_tree``, so a symlink cycle terminates. Exactly two error classes:
a per-entry stat failure hashes the NAME ONLY and continues (a dangling
symlink never aborts the walk); a scandir or root-stat failure (including
one surfacing via a worker future) yields ``None`` so the caller rescans
instead of serving stale data.

Never opens or extracts archives; no recursive-walk helper; never imports
``app.routes`` (the gate/warmup/refresh call sites call this via
``run_in_threadpool`` and must stay unchanged).
"""

import hashlib
import os
import stat as statmod
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.archive import ARCHIVE_EXTENSIONS
from app.scanner import PDF_EXTENSIONS

# I/O-bound cold walk over an external volume; one pool per call, matching
# the pre-existing per-call semantics.
_MAX_WORKERS = 8

_TOML_NAME = "metadata.toml"


def _scan_dir_local(path: Path, dir_st: os.stat_result) -> tuple[bytes, list[tuple[str, os.stat_result]]]:
    """Scan ONE directory: build its local digest terms, list subdir children.

    ONE ``os.scandir`` per call — a failure here propagates to the caller
    (via the worker future) and fails the whole walk open. Entries are
    sorted by name and classified via the cached ``d_type`` probes
    (``is_symlink``/``is_dir``/``is_file`` cost no syscall for known types),
    so ``entry.stat()`` runs ONLY where its result feeds a digest term or
    an identity: symlinks (one follow-stat to resolve), subdirs (mtime/ino),
    and archives/PDFs/metadata.toml (mtime+size). Plain images and exotic
    types hash the NAME ONLY with zero stat calls. Any per-entry ``OSError``
    folds the NAME ONLY and continues. Subdirs are returned (not recursed
    into) for the main thread to dedupe and enqueue.
    """
    local = bytearray()
    local += b"\x00d" + dir_st.st_mtime_ns.to_bytes(8, "little", signed=True)
    with os.scandir(path) as it:
        entries = [e for e in it if not e.name.startswith(".")]
    entries.sort(key=lambda e: e.name)
    children: list[tuple[str, os.stat_result]] = []
    for entry in entries:
        name = entry.name
        local += b"\x00n" + name.encode("utf-8", "surrogateescape")
        try:
            if entry.is_symlink():
                # DT_LNK: d_type alone cannot classify — one follow-stat.
                try:
                    st = entry.stat(follow_symlinks=True)
                except OSError:
                    continue  # dangling symlink — name already hashed.
            elif entry.is_dir(follow_symlinks=False):
                # Cached d_type, no syscall: stat once for mtime/ino identity.
                try:
                    st = entry.stat()
                except OSError:
                    continue  # name already hashed.
                children.append((name, st))
                continue
            elif entry.is_file(follow_symlinks=False):
                # Cached d_type, no syscall: stat ONLY term-bearing files.
                if name == _TOML_NAME:
                    tag = b"\x00t"
                elif Path(name).suffix.lower() in ARCHIVE_EXTENSIONS or Path(name).suffix.lower() in PDF_EXTENSIONS:
                    tag = b"\x00f"
                else:
                    continue  # plain image etc: name only, zero stat calls.
                try:
                    st = entry.stat()
                except OSError:
                    continue  # name already hashed.
                local += tag + st.st_mtime_ns.to_bytes(8, "little", signed=True)
                local += st.st_size.to_bytes(8, "little", signed=True)
                continue
            else:
                continue  # exotic/DT_UNKNOWN-unresolvable — name already hashed.
        except OSError:
            continue  # d_type probe itself failed — name only, keep walking.
        if statmod.S_ISDIR(st.st_mode):
            children.append((name, st))
        elif statmod.S_ISREG(st.st_mode):
            if name == _TOML_NAME:
                tag = b"\x00t"
            elif Path(name).suffix.lower() in ARCHIVE_EXTENSIONS or Path(name).suffix.lower() in PDF_EXTENSIONS:
                tag = b"\x00f"
            else:
                continue  # symlink to a plain image: name only, as before.
            local += tag + st.st_mtime_ns.to_bytes(8, "little", signed=True)
            local += st.st_size.to_bytes(8, "little", signed=True)
        # else: exotic survivor type — name already hashed.
    return bytes(local), children


try:
    import bookq_rs as _rs
except ImportError:
    _rs = None
if _rs is not None and os.environ.get("BOOKQ_RUST", "1") == "0":
    _rs = None


def compute_signature(root: Path) -> str | None:
    """Return a stable sha256 hexdigest of the tree under *root*.

    Native path (issue #163 W3): ``bookq_rs.tree_signature_rs`` reproduces
    the BFS/local-terms/bottom-up fold byte-for-byte and returns ``None``
    on any error — which is also the reference contract's answer for a
    missing or unreadable root, so the wrapper re-runs the Python walk on
    ``None`` (correct either way; the miss case is rare).
    """
    if _rs is not None:
        try:
            got = _rs.tree_signature_rs(str(root))
        except Exception:
            got = None
        if got is not None:
            return got
    return _py_compute_signature(root)


def _py_compute_signature(root: Path) -> str | None:
    """Reference implementation of :func:`compute_signature`.

    Level-synchronous parallel BFS over a single per-call pool; bottom-up
    combination in reverse BFS order (pure CPU, schedule-independent).
    Returns ``None`` on any scandir/root-stat ``OSError`` or when *root* is
    missing or not a directory.
    """
    try:
        root_st = root.stat()
    except OSError:
        return None
    if not statmod.S_ISDIR(root_st.st_mode):
        return None
    visited = {(root_st.st_dev, root_st.st_ino)}
    # BFS levels in visit order; parallel index aligned with `levels`.
    levels: list[list[tuple[Path, os.stat_result]]] = [[(root, root_st)]]
    # Per-directory data keyed by (st_dev, st_ino): local terms + children.
    locals: dict[tuple[int, int], bytes] = {}
    kids: dict[tuple[int, int], list[tuple[str, tuple[int, int]]]] = {}
    try:
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            level_idx = 0
            while level_idx < len(levels):
                level = levels[level_idx]
                futs = [pool.submit(_scan_dir_local, path, st) for path, st in level]
                next_level: list[tuple[Path, os.stat_result]] = []
                for (path, _st), fut in zip(level, futs):
                    local_terms, children = fut.result()
                    ident = (_st.st_dev, _st.st_ino)
                    locals[ident] = local_terms
                    ordered: list[tuple[str, tuple[int, int]]] = []
                    for child_name, child_st in children:
                        child_ident = (child_st.st_dev, child_st.st_ino)
                        if child_ident in visited:
                            continue
                        visited.add(child_ident)
                        ordered.append((child_name, child_ident))
                        next_level.append((path / child_name, child_st))
                    kids[ident] = ordered
                if next_level:
                    levels.append(next_level)
                level_idx += 1
    except OSError:
        return None
    # Bottom-up combination in REVERSE BFS order: children always combine
    # before their parent (a parent sits exactly one level above its child).
    digests: dict[tuple[int, int], bytes] = {}
    for level in reversed(levels):
        for _path, st in level:
            ident = (st.st_dev, st.st_ino)
            h = hashlib.sha256()
            h.update(locals[ident])
            for child_name, child_ident in sorted(kids[ident], key=lambda t: t[0]):
                h.update(child_name.encode("utf-8", "surrogateescape"))
                h.update(digests[child_ident])
            digests[ident] = h.digest()
    root_ident = (root_st.st_dev, root_st.st_ino)
    return digests[root_ident].hex()
