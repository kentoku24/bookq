"""Scan phase timings: walk_ms/archive_ms/pdf_ms in the shared rebuild log line.

Lane A (warmup-startup-speedup): ``scan_tree`` accumulates wall-time per
phase into an explicit ``_phases`` dict, and ``_scan_and_rebuild``
(app/routes.py) appends the three fields AFTER the existing fields of the
single "metadata index rebuilt" log line. Both the warmup and request
paths share that line, so the identical-template pin keeps passing.

Asserts field presence/shape only — never durations (no flaky thresholds).

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_scan_phase_timings.py -q
"""

import asyncio
import logging
import os
import re
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

os.environ["BOOKQ_TEST_MODE"] = "true"

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from app.config import Settings  # noqa: E402
from app.routes import warmup_metadata_index  # noqa: E402
from app.scanner import scan_tree  # noqa: E402
from tests.test_scanner_perf_todo6 import _make_pdf, _make_png, _make_zip  # noqa: E402
from tests.helpers import drive  # noqa: E402

_PHASE_RE = re.compile(r"(walk_ms|archive_ms|pdf_ms)=(\S+)")


def _state(downloads_path):
    return SimpleNamespace(
        settings=Settings(downloads_dir=str(downloads_path)),
        metadata_index_cache=None,
    )


def _build_phases_tree(root: Path) -> None:
    """Small tree exercising all three phases: walk + archive + pdf."""
    img_dir = root / "pics"
    img_dir.mkdir(parents=True)
    _make_png(img_dir / "01.png")
    _make_zip(root / "book.zip", ["ch1/01.jpg", "cover.jpg"])
    _make_pdf(root / "doc.pdf", pages=2)


def _assert_phases_shape(message: str) -> None:
    found = dict(_PHASE_RE.findall(message))
    for field in ("walk_ms", "archive_ms", "pdf_ms"):
        assert field in found, f"{field} missing in {message!r}"
        value = float(found[field])
        assert value >= 0, f"{field}={value} negative in {message!r}"


def test_rebuild_log_contains_phase_timings(tmp_path, caplog):
    _build_phases_tree(tmp_path)
    state = _state(tmp_path)

    async def main():
        await warmup_metadata_index(state)

    with caplog.at_level(logging.INFO, logger="app.routes"):
        drive(main())

    records = [r for r in caplog.records if r.msg.startswith("metadata index rebuilt")]
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    message = records[0].getMessage()
    _assert_phases_shape(message)


def test_scan_tree_phases_accumulator_is_non_negative(tmp_path):
    _build_phases_tree(tmp_path)
    phases: dict[str, float] = {"walk_ms": 0.0, "archive_ms": 0.0, "pdf_ms": 0.0}
    tree = scan_tree(tmp_path, _phases=phases)
    assert tree, "fixture tree must be non-empty"
    for field in ("walk_ms", "archive_ms", "pdf_ms"):
        assert field in phases, phases
        assert phases[field] >= 0, phases
