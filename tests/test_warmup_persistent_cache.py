"""Failing-first tests for the restart-spanning warmup persistent cache (T6).

T7 will add ``app/warmup_cache.py`` (on-disk JSON cache under
``BOOKQ_CACHE_DIR``/``warmup-cache-v1.json``, keys are the
DOWNLOADS-relative path plus ``(mtime_ns, size)``). Until then every test
below must FAIL (RED):

- (a)/(b)/(c): the second lookup on an unchanged file still performs real
  I/O, so the zero-I/O assertions fail.
- (d)/(e)/(f): they import the not-yet-existing ``app.warmup_cache`` seam,
  so they fail with ImportError.

Isolation: each test repoints ``BOOKQ_CACHE_DIR`` at ``tmp_path`` via
monkeypatch, so the real tmpdir is never touched. mtime changes use
``os.utime(..., ns=(0, 0))`` (deterministic on coarse filesystems — never
sleep-based). TTL expiry uses a monkeypatched TTL constant (never wall-clock
waits).

Run (expect RED until T7 lands):
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright \\
        --with-requirements requirements-dev.txt pytest tests/test_warmup_persistent_cache.py -q
"""

import os
import sys
import zipfile
from pathlib import Path

os.environ["BOOKQ_TEST_MODE"] = "true"

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from app import archive as app_archive  # noqa: E402
from app import archive_pdf as app_archive_pdf  # noqa: E402
from tests.test_scanner_perf_todo6 import _make_pdf, _make_zip  # noqa: E402

_ZIP_NAMES = ["ch1/01.jpg", "ch1/02.jpg", "ch2/01.jpg", "cover.jpg"]


def _isolate(tmp_path, monkeypatch):
    """Point the (future) disk cache at tmp_path; never the real tmpdir."""
    monkeypatch.setenv("BOOKQ_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("DOWNLOADS_DIR", str(tmp_path / "downloads"))


def _counting_uncached(monkeypatch):
    """Wrap ``_uncached_pdf_page_count`` with a call counter (delegates)."""
    real = app_archive_pdf._uncached_pdf_page_count
    calls: list = []

    def spy(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(app_archive_pdf, "_uncached_pdf_page_count", spy)
    return calls


def _counting_zipfile(monkeypatch):
    """Wrap ``archive._zip_names`` with an open counter (delegates).

    Counts at the dispatch seam rather than ``zipfile.ZipFile`` itself: the
    native reader (issue #163) lists archives without touching ``zipfile``,
    but both implementations still funnel through this function exactly once
    per real re-read.
    """
    real = app_archive._zip_names
    calls: list = []

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(app_archive, "_zip_names", spy)
    return calls


# (a) PDF: restart-equivalent (in-process memo cleared) answers from disk
# with ZERO real I/O on an unchanged file.
def test_pdf_page_count_survives_restart_without_io(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    downloads = Path(os.environ["DOWNLOADS_DIR"])
    downloads.mkdir(parents=True)
    pdf = downloads / "doc.pdf"
    _make_pdf(pdf, pages=2)

    calls = _counting_uncached(monkeypatch)
    assert app_archive_pdf.pdf_page_count(pdf) == 2
    assert len(calls) >= 1  # warm: real I/O populates the caches

    # Process-restart equivalent: in-process memo is gone (archive.py has no
    # in-process memo at all, so the archive side is pure I/O counting).
    app_archive_pdf.clear_pdf_page_count_memo()

    before = len(calls)
    assert app_archive_pdf.pdf_page_count(pdf) == 2
    assert len(calls) == before, (
        f"unchanged PDF must answer from disk cache with zero real I/O "
        f"(extra _uncached_pdf_page_count calls: {len(calls) - before})"
    )


# (b) Archive: unchanged zip answers with ZERO ZipFile opens.
def test_list_archive_entries_survives_restart_without_io(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    downloads = Path(os.environ["DOWNLOADS_DIR"])
    downloads.mkdir(parents=True)
    zpath = downloads / "book.zip"
    _make_zip(zpath, _ZIP_NAMES)
    expected = app_archive.list_archive_entries(zpath)
    assert len(expected) == 4

    calls = _counting_zipfile(monkeypatch)
    assert app_archive.list_archive_entries(zpath) == expected
    assert len(calls) == 0, (
        f"unchanged zip must answer from disk cache with zero ZipFile opens "
        f"(opens: {len(calls)})"
    )


# (c) mtime/size change forces a re-read (both kinds).
def test_mtime_or_size_change_forces_reread(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    downloads = Path(os.environ["DOWNLOADS_DIR"])
    downloads.mkdir(parents=True)

    # PDF side: warm, prove the disk hit, then force mtime drift.
    pdf = downloads / "doc.pdf"
    _make_pdf(pdf, pages=2)
    calls = _counting_uncached(monkeypatch)
    assert app_archive_pdf.pdf_page_count(pdf) == 2
    app_archive_pdf.clear_pdf_page_count_memo()
    before = len(calls)
    assert app_archive_pdf.pdf_page_count(pdf) == 2
    assert len(calls) == before, "precondition: unchanged PDF is a disk hit"

    os.utime(pdf, ns=(0, 0))  # deterministic mtime change, no sleep
    app_archive_pdf.clear_pdf_page_count_memo()
    assert app_archive_pdf.pdf_page_count(pdf) == 2
    assert len(calls) > before, "mtime change must force a real re-read"

    # Size/content change yields a fresh value, not the stale cached one.
    _make_pdf(pdf, pages=5)
    app_archive_pdf.clear_pdf_page_count_memo()
    assert app_archive_pdf.pdf_page_count(pdf) == 5

    # Archive side: mtime drift forces a real ZipFile re-open.
    zpath = downloads / "book.zip"
    _make_zip(zpath, _ZIP_NAMES)
    expected = app_archive.list_archive_entries(zpath)
    zcalls = _counting_zipfile(monkeypatch)
    assert app_archive.list_archive_entries(zpath) == expected
    assert len(zcalls) == 0, "precondition: unchanged zip is a disk hit"
    os.utime(zpath, ns=(0, 0))
    assert app_archive.list_archive_entries(zpath) == expected
    assert len(zcalls) > 0, "mtime change must force a real ZipFile re-open"


# (d) TTL expiry forces a re-read (monkeypatched TTL, never sleep).
def test_ttl_expiry_forces_reread(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    import app.warmup_cache as warmup_cache  # RED now: module does not exist

    downloads = Path(os.environ["DOWNLOADS_DIR"])
    downloads.mkdir(parents=True)
    pdf = downloads / "doc.pdf"
    _make_pdf(pdf, pages=2)

    calls = _counting_uncached(monkeypatch)
    assert app_archive_pdf.pdf_page_count(pdf) == 2
    app_archive_pdf.clear_pdf_page_count_memo()
    before = len(calls)
    assert app_archive_pdf.pdf_page_count(pdf) == 2
    assert len(calls) == before, "precondition: unchanged PDF is a disk hit"

    # Expire every disk entry without waiting: TTL already in the past.
    monkeypatch.setattr(warmup_cache, "PERSISTENT_CACHE_TTL", -1)
    app_archive_pdf.clear_pdf_page_count_memo()
    assert app_archive_pdf.pdf_page_count(pdf) == 2
    assert len(calls) > before, "TTL expiry must force a real re-read"


# (e) A corrupt cache file falls back to live values without raising.
def test_corrupt_cache_file_falls_back_without_raising(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    import app.warmup_cache as warmup_cache  # RED now: module does not exist

    downloads = Path(os.environ["DOWNLOADS_DIR"])
    downloads.mkdir(parents=True)
    pdf = downloads / "doc.pdf"
    _make_pdf(pdf, pages=2)
    zpath = downloads / "book.zip"
    _make_zip(zpath, _ZIP_NAMES)
    expected = app_archive.list_archive_entries(zpath)

    assert app_archive_pdf.pdf_page_count(pdf) == 2  # warm the disk cache

    cache_file = Path(warmup_cache.cache_file_path())
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_bytes(b"\x00\xff not-json {{{{{")

    app_archive_pdf.clear_pdf_page_count_memo()
    assert app_archive_pdf.pdf_page_count(pdf) == 2  # live re-read, no raise
    assert app_archive.list_archive_entries(zpath) == expected  # no raise


# (f) Cache keys use the DOWNLOADS-relative path + (mtime_ns, size) — never
# an abspath (container re-mounts change abspaths).
def test_cache_key_is_downloads_relative_not_abspath(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from app.warmup_cache import cache_key  # RED now: module does not exist

    root_a = tmp_path / "mnt-a" / "downloads"
    root_b = tmp_path / "mnt-b" / "downloads"
    for root in (root_a, root_b):
        (root / "sub").mkdir(parents=True)
        _make_pdf(root / "sub" / "doc.pdf", pages=2)
    # Same content, same stat tuple: force identical (mtime_ns, size).
    st_a = (root_a / "sub" / "doc.pdf").stat()
    os.utime(root_b / "sub" / "doc.pdf", ns=(st_a.st_mtime_ns, st_a.st_mtime_ns))
    st_b = (root_b / "sub" / "doc.pdf").stat()
    assert (st_a.st_mtime_ns, st_a.st_size) == (st_b.st_mtime_ns, st_b.st_size)

    key_a = cache_key(root_a, root_a / "sub" / "doc.pdf")
    key_b = cache_key(root_b, root_b / "sub" / "doc.pdf")
    assert key_a == key_b, "same relative path+stat must share a key across mounts"
    blob = repr(key_a)
    assert str(root_a) not in blob and str(root_b) not in blob, (
        f"cache key must never embed an abspath: {blob}"
    )
