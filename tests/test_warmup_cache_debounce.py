"""Debounced persistence for the warmup disk cache (plan warmup-cache-batch).

``app/warmup_cache._store_entry`` used to rewrite the whole JSON file on
every stored entry (load-modify-save + atomic replace, O(entries x
filesize)). Stores now buffer into ``_PENDING`` and flush when the debounce
window closes (``_FLUSH_MIN_INTERVAL_SECONDS`` since the last flush), when
``_FLUSH_DIRTY_THRESHOLD`` entries are pending, when the trailing daemon
timer fires, or on an explicit ``flush()``.

Coverage:
  (a) a burst of N stores produces far fewer than N file writes (spy on
      ``os.replace`` — also proving the atomic tmp+replace path is kept);
  (b) ``flush()`` persists every pending entry;
  (c) the trailing timer persists the tail of a burst with no further call;
  (d) writes go through ``os.replace`` with a ``.tmp-<pid>`` source;
  (e) a debounced (not yet flushed) store is visible to lookups — the
      pending map is authoritative, so store-then-get still hits.

Isolation: each test repoints ``BOOKQ_CACHE_DIR`` at ``tmp_path`` and calls
``_reset_debounce()`` so pending entries/armed timers never leak across
tests. Intervals are monkeypatched module constants (read at call time),
never real 5s waits.

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright \
        --with-requirements requirements-dev.txt pytest tests/test_warmup_cache_debounce.py -q
"""

import json
import os
import sys
import time
from pathlib import Path

os.environ["BOOKQ_TEST_MODE"] = "true"

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from app import warmup_cache  # noqa: E402


def _isolate(tmp_path, monkeypatch):
    """Point the disk cache at tmp_path and reset mirror + debounce state."""
    monkeypatch.setenv("BOOKQ_CACHE_DIR", str(tmp_path / "cache"))
    warmup_cache._MEM_CACHE = None
    warmup_cache._MEM_SIG = None
    warmup_cache._reset_debounce()


def _read_disk() -> dict:
    path = Path(warmup_cache.cache_file_path())
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _replace_spy(monkeypatch) -> list:
    """Count ``os.replace`` calls (delegates to the real one)."""
    real = os.replace
    calls: list = []

    def spy(src, dst):
        calls.append((os.fspath(src), os.fspath(dst)))
        return real(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    return calls


# (a) N stores → far fewer than N file writes (immediate first store +
# threshold flushes only; the debounce window is monkeypatched huge so the
# timer can never fire mid-test).
def test_burst_of_stores_writes_the_file_far_fewer_times(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(warmup_cache, "_FLUSH_MIN_INTERVAL_SECONDS", 3600.0)
    calls = _replace_spy(monkeypatch)

    n = 64
    for i in range(n):
        warmup_cache.put_pdf_count(f"burst-{i:03d}", i)

    # Expect ~2-3 writes (first store writes through immediately; the 33rd
    # store hits the 32-entry pending threshold) — never one per store.
    assert 0 < len(calls) <= 4, (
        f"{n} debounced stores must batch into a few writes, got {len(calls)}"
    )
    warmup_cache.flush()
    warmup_cache._reset_debounce()


# (b) flush() persists everything pending.
def test_flush_persists_pending_entries(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(warmup_cache, "_FLUSH_MIN_INTERVAL_SECONDS", 3600.0)

    warmup_cache.put_pdf_count("first", 1)  # writes through (never flushed)
    warmup_cache.put_pdf_count("second", 2)  # debounced → pending only
    disk = _read_disk()
    assert "first" in disk, "first store must have been written through"
    assert "second" not in disk, "precondition: second store still pending"

    warmup_cache.flush()
    disk = _read_disk()
    assert disk["first"]["count"] == 1
    assert disk["second"]["count"] == 2
    warmup_cache._reset_debounce()


# (c) The trailing daemon timer lands the tail of a burst on its own.
def test_trailing_timer_persists_without_further_calls(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(warmup_cache, "_FLUSH_MIN_INTERVAL_SECONDS", 0.2)

    warmup_cache.put_pdf_count("t-first", 1)  # immediate write-through
    warmup_cache.put_pdf_count("t-tail", 2)  # debounced → timer armed ~0.2s

    deadline = time.time() + 5.0
    while "t-tail" not in _read_disk() and time.time() < deadline:
        time.sleep(0.05)
    assert "t-tail" in _read_disk(), (
        "trailing timer must persist the pending tail without a further call"
    )
    warmup_cache._reset_debounce()


# (d) The write path still goes through tmp + os.replace (atomic, no
# half-written file visible to readers).
def test_flush_writes_via_atomic_tmp_and_os_replace(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    calls = _replace_spy(monkeypatch)

    warmup_cache.put_pdf_count("atomic-key", 9)
    warmup_cache.flush()

    target = str(warmup_cache.cache_file_path())
    assert calls, "persistence must go through os.replace"
    for src, dst in calls:
        assert dst == target, f"replace target must be the cache file: {dst}"
        assert src.startswith(target + ".tmp-"), (
            f"replace source must be a sibling tmp file: {src}"
        )
        assert not Path(src).exists(), f"tmp file must be consumed: {src}"
    assert _read_disk()["atomic-key"]["count"] == 9
    warmup_cache._reset_debounce()


# (e) Pending entries are authoritative: a debounced store hits lookups even
# before it reaches the file.
def test_pending_store_is_visible_to_lookups_before_flush(
    tmp_path, monkeypatch
):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(warmup_cache, "_FLUSH_MIN_INTERVAL_SECONDS", 3600.0)

    warmup_cache.put_pdf_count("seed", 1)  # immediate (establishes the file)
    warmup_cache.put_archive_entries("pend", ["a.jpg", "b.jpg"])  # pending
    assert "pend" not in _read_disk(), "precondition: still debounced"

    assert warmup_cache.get_archive_entries("pend") == ["a.jpg", "b.jpg"]
    assert warmup_cache.get_pdf_count("seed") == 1
    warmup_cache._reset_debounce()
