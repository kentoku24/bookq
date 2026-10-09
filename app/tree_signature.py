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

Dot-names are skipped. Directories are visited once by ``(st_dev, st_ino)``
identity — ``stat()`` follows symlinks (never ``lstat``), mirroring
``scan_tree``, so a symlink cycle terminates. Fail-open: any walk-time
``OSError`` (or a missing / non-directory root) yields ``None`` so the
caller rescans instead of serving stale data.

Never opens or extracts archives; non-archive non-toml files contribute
their names only (no per-file stat) to keep the walk cheap. No ``os.walk``.
"""

import hashlib
import os
import stat as statmod
from pathlib import Path

from app.archive import ARCHIVE_EXTENSIONS
from app.scanner import PDF_EXTENSIONS

_TOML_NAME = "metadata.toml"


def _feed_stat_terms(digest: "hashlib._Hash", tag: bytes, st: os.stat_result) -> None:
    digest.update(tag + st.st_mtime_ns.to_bytes(8, "little", signed=True))
    digest.update(st.st_size.to_bytes(8, "little", signed=True))


def compute_signature(root: Path) -> str | None:
    """Return a stable sha256 hexdigest of the tree under *root*.

    Entries of each directory are processed in sorted name order. Returns
    ``None`` on any walk-time ``OSError`` or when *root* is missing or not
    a directory.
    """
    try:
        root_st = root.stat()
    except OSError:
        return None
    if not statmod.S_ISDIR(root_st.st_mode):
        return None
    digest = hashlib.sha256()
    visited = {(root_st.st_dev, root_st.st_ino)}
    try:
        stack: list[tuple[Path, os.stat_result]] = [(root, root_st)]
        while stack:
            current, current_st = stack.pop()
            digest.update(b"\x00d" + current_st.st_mtime_ns.to_bytes(8, "little", signed=True))
            with os.scandir(current) as it:
                entries = [e for e in it if not e.name.startswith(".")]
            entries.sort(key=lambda e: e.name)
            pending: list[tuple[Path, os.stat_result]] = []
            for entry in entries:
                digest.update(b"\x00n" + entry.name.encode("utf-8", "surrogateescape"))
                if entry.is_dir(follow_symlinks=True):
                    st = entry.stat(follow_symlinks=True)
                    ident = (st.st_dev, st.st_ino)
                    if ident in visited:
                        continue
                    visited.add(ident)
                    pending.append((Path(entry.path), st))
                elif entry.is_file(follow_symlinks=True):
                    if entry.name == _TOML_NAME:
                        _feed_stat_terms(digest, b"\x00t", entry.stat(follow_symlinks=True))
                    else:
                        suffix = Path(entry.name).suffix.lower()
                        if suffix in ARCHIVE_EXTENSIONS or suffix in PDF_EXTENSIONS:
                            _feed_stat_terms(digest, b"\x00f", entry.stat(follow_symlinks=True))
                # else: broken symlink / exotic type — name already hashed.
            stack.extend(reversed(pending))
    except OSError:
        return None
    return digest.hexdigest()
