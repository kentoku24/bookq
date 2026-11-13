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
to a full discard + rebuild on the next flush). Concurrent writers are
serialised by the module-level ``_STORE_LOCK``: ``scan_tree``'s
``ThreadPoolExecutor`` fan-out would otherwise race the whole-file
load-modify-save (each thread loads a stale snapshot, adds one entry, and
replaces the whole file: last-writer-wins, entries are permanently lost,
observed as 6/85 then 29/85 with a stored entry destroyed). Fail-open is
preserved: the lock only serialises writers, and every helper still
swallows its own exceptions.

Debounced persistence (plan warmup-cache-batch): :func:`_store_entry` no
longer rewrites the whole JSON file per entry — during background fill /
scan fan-out that was a multi-MB dump every ~1-3s, O(entries x filesize).
Stores now land in ``_PENDING`` (the in-memory authoritative map — lookups
consult it before the file, so a stored value hits immediately) and the
file is rewritten at most once per ``_FLUSH_MIN_INTERVAL_SECONDS`` while a
burst is in flight, early once ``_FLUSH_DIRTY_THRESHOLD`` entries are
pending, by a daemon trailing :class:`threading.Timer` so the tail of a
burst lands without a further call, and on clean shutdown via ``atexit``.
A crash can lose the pending batch — acceptable: it is a cache, the next
scan recomputes it. ``_LAST_FLUSH`` starts at ``-inf`` so a lone store
(the sparse path — single request-time archive miss) still writes through
immediately. ``BOOKQ_CACHE_DIR`` is read at call time even mid-burst: a
repoint while entries are pending flushes them into the *new* file (tests
call :func:`flush` / :func:`_reset_debounce` around repoints; production
never repoints).

Memory mirror: lookups no longer re-parse the whole JSON file on every
call. The module keeps ``_MEM_CACHE`` (the last parsed mapping) with
``_MEM_SIG`` (that file's ``(st_mtime_ns, st_size)``), guarded by
``_MEM_LOCK``. A read stats the file (cheap) and serves the memory copy
when the sig still matches, so consecutive lookups parse at most once.
Flushes are write-through (the mirror is updated from the just-written
file's stat right after the atomic replace); a cross-process rewrite is
detected by the sig mismatch on the next read and re-parsed — pending
entries survive it because they live in ``_PENDING``, not the mirror. A
stat failure, parse failure, or non-dict payload still falls back to
``{}`` (fail-open exactly as before).

Failure semantics: every public helper swallows all of its own exceptions
and callers additionally guard with try/except, so the cache is strictly
fail-open — a broken cache only costs I/O, never correctness.
"""

import atexit
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

# In-memory mirror of the cache file: _MEM_CACHE is the last parsed
# mapping (or None when nothing has been parsed yet), _MEM_SIG is that
# file's (st_mtime_ns, st_size) sig. Guarded by _MEM_LOCK; always taken
# leaf-most (never hold _MEM_LOCK while acquiring _STORE_LOCK) so the
# store path (_STORE_LOCK -> _MEM_LOCK) cannot deadlock with readers.
_MEM_CACHE: dict | None = None
_MEM_SIG: tuple[int, int] | None = None
_MEM_LOCK = threading.Lock()

# File-bloat guard: never keep more than this many entries; beyond it the
# oldest ``saved_at`` entries are evicted first.
_MAX_DISK_ENTRIES = 5000

# Debounced persistence (plan warmup-cache-batch) — values chosen so a burst
# of archive/pdf stores collapses to ~1 rewrite per window instead of 1 per
# entry, while unflushed exposure stays small:
#   _FLUSH_MIN_INTERVAL_SECONDS: lower bound between automatic flushes. 5s
#     matches the plan (~5s) — long enough to batch a whole fill burst, short
#     enough that a clean-shutdown-free crash loses at most a few seconds of
#     cache warming (recomputed on the next scan anyway). During continuous
#     fill (~1 store/1-3s observed) this alone cuts rewrites ~3-5x.
#   _FLUSH_DIRTY_THRESHOLD: force a flush once this many entries are pending.
#     32 bounds both pending memory (each entry is at most a capped archive
#     listing already resident in RAM) and the crash-loss window, and turns
#     a scan fan-out burst (tens of puts in <1s) into ceil(N/32) rewrites.
# Read at call time (module globals) so tests can monkeypatch them.
_FLUSH_MIN_INTERVAL_SECONDS = 5.0
_FLUSH_DIRTY_THRESHOLD = 32

# Pending (not yet flushed) entries — the authoritative in-memory store.
# Lookups check it before the file, so a store hits immediately even while
# debounced. Guarded by _STORE_LOCK for mutation; ``_lookup_entry`` reads it
# lock-free (dict.get is atomic under the GIL, and a pending entry is always
# the freshest value for its key — it is merged last over the disk mapping
# at flush time, so reads and writes agree on which side wins).
_PENDING: dict = {}
# time.monotonic() of the last flush; -inf ⇒ never flushed ⇒ the next store
# writes through immediately regardless of the interval (sparse-put path).
_LAST_FLUSH = float("-inf")
# Armed trailing-flush timer (daemon) or None; guarded by _STORE_LOCK.
_FLUSH_TIMER: "threading.Timer | None" = None


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


def _load_cached():
    """Load the raw JSON mapping via the in-memory mirror (fail-open).

    Stats the file (cheap) and returns the memory copy without parsing
    when the ``(st_mtime_ns, st_size)`` sig still matches; otherwise
    parses, validates, and refreshes the mirror. Stat failure, parse
    failure, or a non-dict payload yields ``{}``.
    """
    global _MEM_CACHE, _MEM_SIG
    try:
        st = Path(cache_file_path()).stat()
        sig = (st.st_mtime_ns, st.st_size)
    except Exception:
        return {}
    try:
        with _MEM_LOCK:
            if _MEM_CACHE is not None and _MEM_SIG == sig:
                return _MEM_CACHE
        with open(cache_file_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}
        with _MEM_LOCK:
            _MEM_CACHE = data
            _MEM_SIG = sig
        return data
    except Exception:
        return {}


def _load_raw():
    """Load the raw JSON mapping, or {} when missing/corrupt (fail-open)."""
    return _load_cached()


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


def _flush_locked() -> None:
    """Write pending entries to disk; caller must hold ``_STORE_LOCK``.

    Merges ``_PENDING`` over the last-parsed/on-disk mapping (an external
    rewrite is re-parsed through the sig check, so pending entries layer on
    top of it — consistent with pending-first reads), evicts to the size
    cap, and performs the same tmp+``os.replace`` atomic write as before.
    The mirror is refreshed from the written file's sig. Fail-open:
    ``_store_raw`` swallows its own errors; pending is cleared either way —
    a failed cache write only loses warm state a recompute recreates.
    """
    global _LAST_FLUSH, _FLUSH_TIMER, _MEM_CACHE, _MEM_SIG
    if _FLUSH_TIMER is not None:
        _FLUSH_TIMER.cancel()
        _FLUSH_TIMER = None
    if not _PENDING:
        return
    data = _load_cached()
    data.update(_PENDING)
    _evict_oldest(data)
    _store_raw(data)
    _PENDING.clear()
    _LAST_FLUSH = time.monotonic()
    try:
        st = Path(cache_file_path()).stat()
        with _MEM_LOCK:
            _MEM_CACHE = data
            _MEM_SIG = (st.st_mtime_ns, st.st_size)
    except Exception:
        pass


def _trailing_flush() -> None:
    """Timer callback: flush pending entries under the lock (fail-open)."""
    global _FLUSH_TIMER
    try:
        with _STORE_LOCK:
            # Clear before flushing so _flush_locked's cancel is a no-op on
            # the currently-executing timer (and a racing store re-arms).
            _FLUSH_TIMER = None
            _flush_locked()
    except Exception:
        pass


def _schedule_trailing_locked(now: float) -> None:
    """Arm the trailing flush timer; caller must hold ``_STORE_LOCK``.

    Fires when the current debounce window closes, so the tail of a burst
    persists without a further store. Daemon thread: never blocks
    interpreter exit (the ``atexit`` flush covers clean shutdowns).
    """
    global _FLUSH_TIMER
    if _FLUSH_TIMER is not None:
        return
    delay = _FLUSH_MIN_INTERVAL_SECONDS - (now - _LAST_FLUSH)
    delay = min(max(delay, 0.05), _FLUSH_MIN_INTERVAL_SECONDS)
    timer = threading.Timer(delay, _trailing_flush)
    timer.daemon = True
    _FLUSH_TIMER = timer
    timer.start()


def _store_entry(key, value):
    """Buffer one entry; the disk rewrite is debounced under ``_STORE_LOCK``.

    Entries land in ``_PENDING`` (the in-memory authoritative map — lookups
    consult it before the file, so a stored value hits immediately) and the
    JSON file is rewritten when the debounce window closes
    (``_FLUSH_MIN_INTERVAL_SECONDS`` since the last flush), when
    ``_FLUSH_DIRTY_THRESHOLD`` entries are pending, when the trailing timer
    fires, or when a caller invokes :func:`flush`. Whole-file corruption
    still rebuilds: the flush loads through ``_load_cached`` ({} on
    corrupt) before merging. ``_STORE_LOCK`` keeps the fan-out semantics of
    the old per-entry load-modify-save — pending updates and flushes stay
    serialised. Fail-open: all errors are swallowed.
    """
    try:
        with _STORE_LOCK:
            _PENDING[key] = value
            now = time.monotonic()
            if (
                len(_PENDING) >= _FLUSH_DIRTY_THRESHOLD
                or now - _LAST_FLUSH >= _FLUSH_MIN_INTERVAL_SECONDS
            ):
                _flush_locked()
            else:
                _schedule_trailing_locked(now)
    except Exception:
        pass


def flush() -> None:
    """Persist pending entries now; no-op when nothing is dirty (fail-open).

    Callers use it at natural boundaries — end of a scan/rebuild pass, end
    of the background fill — so a burst's tail does not wait for the
    trailing timer. Also registered with ``atexit``.
    """
    try:
        with _STORE_LOCK:
            _flush_locked()
    except Exception:
        pass


def _reset_debounce() -> None:
    """Drop debounce state WITHOUT writing — test isolation seam.

    Clears pending entries, cancels an armed trailing timer, and re-arms
    the "never flushed" sentinel. Pair with the ``_MEM_CACHE``/``_MEM_SIG``
    reset the existing tests already do when repointing ``BOOKQ_CACHE_DIR``.
    """
    global _LAST_FLUSH, _FLUSH_TIMER
    with _STORE_LOCK:
        _PENDING.clear()
        _LAST_FLUSH = float("-inf")
        if _FLUSH_TIMER is not None:
            _FLUSH_TIMER.cancel()
            _FLUSH_TIMER = None


def _lookup_entry(key, kind):
    """Return the validated entry for (*key*, *kind*), or None on any miss."""
    try:
        # Pending entries are the freshest (flush merges them last over the
        # disk mapping), so they shadow the file — also what makes a
        # debounced store hit immediately. dict.get is atomic under the GIL.
        entry = _PENDING.get(key)
        if entry is None:
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


# Land any unflushed tail on clean shutdown (the daemon trailing timer is
# not guaranteed to fire at interpreter exit). A crash can still lose the
# pending batch — a cache, recomputed on the next scan.
atexit.register(flush)
