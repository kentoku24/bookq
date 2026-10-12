"""Shared test helpers (deduplicated from per-module copies).

``drive`` consolidates the ``_drive`` copies in test_ensure_index,
test_stale_serve, test_cover_warmup, test_warmup_baseline,
test_scan_phase_timings, test_warmup_breakdown, test_thumb_sprite, and
test_warmup_lifespan; ``make_pdf`` consolidates the minimal multi-page PDF
builder copies in test_thumbs and test_cover_warmup. Import via
``from tests.helpers import drive, make_pdf`` (tests/ has ``__init__.py``).
"""

import asyncio
import threading
from pathlib import Path


def drive(coro):
    """Run *coro* to completion on a fresh event loop in a helper thread.

    Playwright's sync API (used by the browser tests in this suite) leaves a
    running event loop behind in the main thread, so plain asyncio.run() fails
    with "cannot be called from a running event loop" when this module runs
    after them. A dedicated thread always has a clean loop state. The
    module-level _index_lock is never contended here (strictly sequential
    awaits), so it never binds to any of these short-lived loops.

    Exceptions raised by *coro* propagate to the caller (needed by
    error-path tests such as test_thumb_sprite's 404 probes).
    """
    outcome = {}

    def target():
        try:
            outcome["value"] = asyncio.run(coro)
        except BaseException as exc:  # propagate to caller
            outcome["error"] = exc

    worker = threading.Thread(target=target)
    worker.start()
    worker.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def make_pdf(path: Path, pages: int = 2) -> None:
    """Minimal multi-page PDF (content stream per page, same bytes as before)."""
    objs: list[str] = ["1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"]
    kids = " ".join(f"{3 + i} 0 R" for i in range(pages))
    objs.append(f"2 0 obj\n<< /Type /Pages /Kids [{kids}] /Count {pages} >>\nendobj\n")
    for i in range(pages):
        objs.append(
            f"{3 + i} 0 obj\n<< /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 200 200] /Contents {3 + pages + i} 0 R >>\nendobj\n"
        )
    for i in range(pages):
        content = f"BT 10 90 Td (Page {i + 1}) Tj ET"
        objs.append(
            f"{3 + pages + i} 0 obj\n<< /Length {len(content)} >>\n"
            f"stream\n{content}\nendstream\nendobj\n"
        )
    path.write_bytes(
        b"%PDF-1.4\n" + "".join(objs).encode() + b"trailer\n<< /Root 1 0 R >>\n%%EOF"
    )
