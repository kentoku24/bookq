"""Contention test for the warmup disk-cache lost-update race (follow-up fix).

``app/warmup_cache._store_entry`` is an unlocked load-modify-save of the
whole JSON file, while ``scan_tree`` fans archive/pdf enumeration across a
``ThreadPoolExecutor`` (``app/scanner.py``). Concurrent puts clobber each
other (last-writer-wins): each thread loads a stale snapshot, adds one
entry, and ``os.replace``\\ s the whole file. Observed in production as
6/85 then 29/85 entries with a previously-stored entry destroyed
(``.omo/evidence/warmup-startup-speedup/lane-b-diagnosis.md`` §4).

N=32 threads × ``put_archive_entries``/``put_pdf_count`` with distinct keys
against a tmp ``BOOKQ_CACHE_DIR``, then assert ALL N entries present.
Pre-fix this is RED (entries lost); post-fix (module-level lock) GREEN.

Calibration: each thread loops its put 3× and all threads rendezvous on a
barrier first, widening the race window so the RED is deterministic even
though the cache file is small.
"""

import json
import os
import sys
import threading
from pathlib import Path

os.environ["BOOKQ_TEST_MODE"] = "true"

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from app import warmup_cache  # noqa: E402

_N_THREADS = 32
_REPEATS = 3  # puts per thread: widens the load-modify-save race window


def test_parallel_puts_lose_no_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOKQ_CACHE_DIR", str(tmp_path / "cache"))
    barrier = threading.Barrier(_N_THREADS)
    errors: list = []

    def worker(i: int):
        archive_key = f"race-archive-{i:03d}"
        pdf_key = f"race-pdf-{i:03d}"
        try:
            barrier.wait(timeout=30)
            for _ in range(_REPEATS):
                warmup_cache.put_archive_entries(
                    archive_key, [f"p{i:03d}.jpg"]
                )
                warmup_cache.put_pdf_count(pdf_key, i)
        except Exception as exc:  # pragma: no cover - barrier/put must not raise
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(i,)) for i in range(_N_THREADS)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "worker threads hung"
    assert not errors, f"workers raised: {errors!r}"

    with open(warmup_cache.cache_file_path(), encoding="utf-8") as fh:
        data = json.load(fh)
    missing = [
        k
        for i in range(_N_THREADS)
        for k in (f"race-archive-{i:03d}", f"race-pdf-{i:03d}")
        if k not in data
    ]
    assert not missing, (
        f"lost update: {len(missing)}/{2 * _N_THREADS} entries missing "
        f"after parallel puts (e.g. {missing[:5]})"
    )
