"""Restart-spanning persistent warmup cache (plan: warmup-startup-speedup T7).

Stores PDF page counts and archive entry listings in a single JSON file
*outside* ``DOWNLOADS_DIR`` (the dev mount is read-only, so nothing may be
written under the downloads tree)::

    os.getenv("BOOKQ_CACHE_DIR", tempfile.gettempdir() + "/bookq-cache")
        / warmup-cache-v1.json

Both ``BOOKQ_CACHE_DIR`` and ``DOWNLOADS_DIR`` are read from the
environment **at call time**, so tests can repoint them with monkeypatch.

Persistence horizon: the tmpdir default only survives same-container
restarts (process restarts on one machine/container). A container
re-creation wipes the default tmpdir — point ``BOOKQ_CACHE_DIR`` at a
persistent volume for a cross-container effect.

Keys are the DOWNLOADS-relative path plus ``(mtime_ns, size)`` — never an
abspath, because container re-mounts change abspaths. When the target path
does not sit under ``DOWNLOADS_DIR`` (or ``DOWNLOADS_DIR`` is unset) the key
falls back to the abspath plus ``(mtime_ns, size)``; such fallback entries
simply stop hitting after a re-mount, which is fail-safe.

Invalidation: an ``mtime_ns``/``size`` mismatch is the primary invalidator
(the stat tuple is part of the key, so a changed file is an automatic
miss). The 7-day ``PERSISTENT_CACHE_TTL`` is only a corruption safety
valve against stale-but-stat-identical values — it is deliberately split
from the 300s in-process horizons.

Concurrency: writes go through a tmp file + ``os.replace`` (atomic), so a
reader never sees a half-written file. Reads validate every entry
individually (a corrupt entry is ignored, whole-file corruption falls back
to a full discard + rebuild on the next store). Concurrent writers are
serialised by the module-level ``_STORE_LOCK`` around the whole
load-modify-save in :func:`_store_entry` — without it, ``scan_tree``'s
``ThreadPoolExecutor`` fan-out races (each thread loads a stale snapshot,
adds one entry, and replaces the whole file: last-writer-wins, entries are
permanently lost, observed as 6/85 then 29/85 with a stored entry
destroyed). Fail-open is preserved: the lock only serialises writers, and
every helper still swallows its own exceptions.

Failure semantics: every public helper swallows all of its own exceptions
and callers additionally guard with try/except, so the cache is strictly
fail-open — a broken cache only costs I/O, never correctness.
"""

import json
import os
import tempfile
import threading
import time
from pathlib import Path

# 7 days: corruption safety valve only; mtime/size mismatch is the real
# invalidator. Read at check time (never captured into a default arg) so a
# monkeypatched TTL takes effect immediately.
PERSISTENT_CACHE_TTL = 7 * 24 * 3600

_CACHE_FILENAME = "warmup-cache-v1.json"

# Whole-file load-modify-saves must not interleave: scan_tree fans puts
# across a ThreadPoolExecutor, so unlocked replaces lose entries.
_STORE_LOCK = threading.Lock()

# File-bloat guard: never keep more than this many entries; beyond it the
# oldest ``saved_at`` entries are evicted first.
_MAX_DISK_ENTRIES = 5000


def cache_file_path() -> Path:
    """Return the JSON cache file path, reading the env var at call time."""
    base = os.getenv(
        "BOOKQ_CACHE_DIR", os.path.join(tempfile.gettempdir(), "bookq-cache")
    )
    return Path(base) / _CACHE_FILENAME


def cache_key(downloads_root, path):
    """Return the cache key for *path* given *downloads_root* (or None).

    The key is the DOWNLOADS-relative POSIX path plus ``(mtime_ns, size)``,
    so two different mount roots holding the same relative path + stat
    produce equal keys and the key repr never embeds either abspath. When
    *path* is not under *downloads_root*, fall back to keying by abspath +
    ``(mtime_ns, size)``. Returns None when *path* cannot be stated.
    """
    try:
        st = Path(path).stat()
    except OSError:
        return None
    mtime_ns, size = st.st_mtime_ns, st.st_size
    rel = _relative_posix(downloads_root, path)
    if rel is not None:
        return f"r:{rel}\x00{mtime_ns}\x00{size}"
    return f"a:{os.fspath(path)}\x00{mtime_ns}\x00{size}"


def cache_key_for_path(path):
    """Derive the cache key for *path* using ``DOWNLOADS_DIR`` (call time).

    Resolves the root from the ``DOWNLOADS_DIR`` env var when the target
    sits under it; otherwise falls back to abspath keying (see
    :func:`cache_key`). Returns None when *path* cannot be stated.
    """
    return cache_key(os.getenv("DOWNLOADS_DIR", ""), path)


def _relative_posix(root, path):
    """Return the POSIX relative path, or None when not under *root*."""
    try:
        if not root:
            return None
        return Path(path).relative_to(root).as_posix()
    except (ValueError, OSError):
        pass
    # Same comparison on resolved paths (covers symlinked tmpdirs).
    try:
        return Path(os.path.realpath(path)).relative_to(
            os.path.realpath(root)
        ).as_posix()
    except (ValueError, OSError):
        return None


def _load_raw():
    """Load the raw JSON mapping, or {} when missing/corrupt (fail-open)."""
    try:
        with open(cache_file_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _store_raw(data):
    """Atomically persist *data* (tmp file + os.replace); swallow errors."""
    try:
        target = cache_file_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = str(target) + f".tmp-{os.getpid()}"
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp_path, target)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception:
        pass


def _evict_oldest(data):
    """Drop oldest-``saved_at`` entries until within the size cap."""
    while len(data) > _MAX_DISK_ENTRIES:
        try:
            oldest = min(
                data,
                key=lambda k: (
                    data[k].get("saved_at", 0)
                    if isinstance(data.get(k), dict)
                    else 0
                ),
            )
        except Exception:
            break
        try:
            del data[oldest]
        except KeyError:
            break


def _store_entry(key, value):
    """Store one entry (load-modify-save; whole-file corruption rebuilds).

    The load-modify-save is serialised by ``_STORE_LOCK``: scan_tree fans
    puts across a ThreadPoolExecutor and unlocked whole-file replaces lose
    entries (last-writer-wins). Fail-open: all errors are swallowed.
    """
    try:
        with _STORE_LOCK:
            data = _load_raw()
            data[key] = value
            _evict_oldest(data)
            _store_raw(data)
    except Exception:
        pass


def _lookup_entry(key, kind):
    """Return the validated entry for (*key*, *kind*), or None on any miss."""
    try:
        entry = _load_raw().get(key)
        if not isinstance(entry, dict):
            return None
        if entry.get("kind") != kind:
            return None
        saved_at = entry.get("saved_at")
        if not isinstance(saved_at, (int, float)):
            return None
        if time.time() - saved_at > PERSISTENT_CACHE_TTL:
            return None
        return entry
    except Exception:
        return None


def get_pdf_count(key):
    """Return the cached PDF page count for *key*, or None on miss/expiry."""
    try:
        entry = _lookup_entry(key, "pdf")
        if entry is None:
            return None
        count = entry.get("count")
        return count if isinstance(count, int) and count >= 0 else None
    except Exception:
        return None


def put_pdf_count(key, count):
    """Cache PDF page *count* under *key* (fail-open)."""
    try:
        if not isinstance(count, int) or count < 0:
            return
        _store_entry(
            key, {"kind": "pdf", "count": count, "saved_at": time.time()}
        )
    except Exception:
        pass


def get_archive_entries(key):
    """Return the cached archive entry list for *key*, or None on miss.

    A hit may carry ``truncated: True`` (see :func:`put_archive_entries`)
    when the stored listing was capped at the reader's entry limit — the
    flag is informational only; the stored bytes are exactly what a live
    capped read serves, so callers use the list as-is.
    """
    try:
        entry = _lookup_entry(key, "archive")
        if entry is None:
            return None
        entries = entry.get("entries")
        if not isinstance(entries, list) or any(
            not isinstance(e, str) for e in entries
        ):
            return None
        return list(entries)
    except Exception:
        return None


def is_archive_truncated(key):
    """Return the stored ``truncated`` flag for *key*, or None on miss.

    ``True`` means the cached listing was cut at the entry cap (an
    oversized archive); ``False`` (including pre-flag entries, which
    predate the flag) means a complete listing. Informational only —
    both cases serve the stored bytes verbatim.
    """
    try:
        entry = _lookup_entry(key, "archive")
        if entry is None:
            return None
        return bool(entry.get("truncated", False))
    except Exception:
        return None


def put_archive_entries(key, entries, *, truncated=False):
    """Cache archive *entries* under *key* (fail-open).

    *truncated* records that the listing was cut at the reader's entry
    cap, so oversized archives stay cacheable across restarts instead of
    re-running the rar/zip tool every time. The stored list itself is
    already capped (the readers never emit more), so a hit serves bytes
    identical to a live capped read.
    """
    try:
        items = list(entries)
        if any(not isinstance(e, str) for e in items):
            return
        _store_entry(
            key,
            {
                "kind": "archive",
                "entries": items,
                "truncated": bool(truncated),
                "saved_at": time.time(),
            },
        )
    except Exception:
        pass
