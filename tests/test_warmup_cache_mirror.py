"""Memory mirror for the warmup persistent cache (Todo 2).

Consecutive lookups must parse the cache JSON at most once (in-memory
mirror keyed by the file's ``(st_mtime_ns, st_size)`` sig), an external
rewrite of the file must be picked up on the next lookup, and the mirror
must work when ``BOOKQ_CACHE_DIR`` is repointed at a tmp path.

Isolation: each test repoints ``BOOKQ_CACHE_DIR`` at ``tmp_path`` via
monkeypatch and resets the module-level mirror, so the real tmpdir is
never touched and tests never share mirror state.

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright \\
        --with-requirements requirements-dev.txt pytest tests/test_warmup_cache_mirror.py -q
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
    # Drop pending entries/armed timers left over from earlier tests —
    # they belong to a different cache dir (plan warmup-cache-batch).
    warmup_cache._reset_debounce()


def test_consecutive_lookups_parse_the_file_at_most_once(
    tmp_path, monkeypatch
):
    _isolate(tmp_path, monkeypatch)
    warmup_cache.put_pdf_count("mirror-key", 7)
    assert warmup_cache.get_pdf_count("mirror-key") == 7  # warms the mirror

    real_load = json.load
    calls: list = []

    def spy(*args, **kwargs):
        calls.append(True)
        return real_load(*args, **kwargs)

    monkeypatch.setattr(warmup_cache.json, "load", spy)
    for _ in range(100):
        assert warmup_cache.get_pdf_count("mirror-key") == 7
    assert len(calls) <= 1, (
        f"100 lookups must parse the file at most once (json.load calls: {len(calls)})"
    )


def test_external_rewrite_is_picked_up_on_next_lookup(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    warmup_cache.put_pdf_count("ext-key", 1)
    warmup_cache.flush()  # persist now — a pending entry would shadow the rewrite
    assert warmup_cache.get_pdf_count("ext-key") == 1  # warms the mirror

    # External (cross-process) rewrite: brand-new JSON, so content and
    # mtime/size change; the next lookup must see it via sig mismatch.
    target = warmup_cache.cache_file_path()
    time.sleep(0.01)
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "ext-key": {
                    "kind": "pdf",
                    "count": 42,
                    "saved_at": time.time(),
                }
            },
            fh,
        )

    assert warmup_cache.get_pdf_count("ext-key") == 42


def test_mirror_works_with_monkeypatched_cache_dir(tmp_path, monkeypatch):
    first = tmp_path / "cache-a"
    monkeypatch.setenv("BOOKQ_CACHE_DIR", str(first))
    warmup_cache._MEM_CACHE = None
    warmup_cache._MEM_SIG = None
    warmup_cache._reset_debounce()
    warmup_cache.put_archive_entries("a-key", ["a.jpg"])
    assert warmup_cache.get_archive_entries("a-key") == ["a.jpg"]

    # Repoint at a fresh dir: the stale mirror (other file's sig/entry)
    # must not leak across; the new dir starts empty, then mirrors itself.
    # Flush FIRST so pending "a-key" lands in cache-a — pending entries are
    # global and would otherwise shadow the miss below (plan warmup-cache-batch).
    warmup_cache.flush()
    second = tmp_path / "cache-b"
    monkeypatch.setenv("BOOKQ_CACHE_DIR", str(second))
    assert warmup_cache.get_archive_entries("a-key") is None
    warmup_cache.put_archive_entries("b-key", ["b.jpg"])
    assert warmup_cache.get_archive_entries("b-key") == ["b.jpg"]

    real_load = json.load
    calls: list = []

    def spy(*args, **kwargs):
        calls.append(True)
        return real_load(*args, **kwargs)

    monkeypatch.setattr(warmup_cache.json, "load", spy)
    for _ in range(20):
        assert warmup_cache.get_archive_entries("b-key") == ["b.jpg"]
    assert len(calls) <= 1, (
        f"repointed dir must mirror too (json.load calls: {len(calls)})"
    )
