"""T3 warmup baseline measurement on a fixed-scale synthetic tree (slow).

Fixed scale (no executor discretion — plan T3):
  zip x30 (4 images each; 5 nested with ch1/ch2) / epub x10 (3 spine pages
  each, minimal valid EPUBs: mimetype + META-INF/container.xml + OPF +
  3 XHTML pages with img refs) / pdf x10 (2 pages each) / plain folders x50
  (2 images each, 10 with metadata.toml) / rar x0 (binary generation
  impossible — recorded as exclusion, rars=0 asserted).

Drives ``warmup_metadata_index`` (phase 1: scan_ms/rebuild_ms + species
counts) then ``lifespan`` on a fresh state (phase 2: elapsed_ms/import_ms).
Each phase uses its own FRESH EMPTY ``BOOKQ_CACHE_DIR`` so T7's disk
cache cannot turn the second scan into a warm hit — both phases are cold.

The test itself asserts STRUCTURAL facts only (species counts, toml hits);
never timing thresholds. Timing numbers are the evidence: run with
``--log-cli-level=INFO`` and the ``metadata index rebuilt`` /
``内部ウォームアップ完了`` lines below are printed verbatim.

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_warmup_baseline.py -q --log-cli-level=INFO
"""

import asyncio
import logging
import os
import sys
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ["BOOKQ_TEST_MODE"] = "true"

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from tests.test_scanner_perf_todo6 import _make_pdf, _make_png, _make_zip  # noqa: E402

from app import main as app_main  # noqa: E402
from app.config import Settings  # noqa: E402
from app.routes import warmup_metadata_index  # noqa: E402

pytestmark = pytest.mark.slow

# Fixed T3 scale.
N_ZIP = 30
N_ZIP_NESTED = 5
N_EPUB = 10
N_EPUB_PAGES = 3
N_PDF = 10
N_PDF_PAGES = 2
N_PLAIN = 50
N_TOML = 10


def _make_epub(path: Path, pages: int = N_EPUB_PAGES) -> None:
    """Minimal valid EPUB with *pages* spine XHTML pages, each with one img ref.

    Matches ``app/archive.py:256-297`` spine expectations: container.xml with
    a rootfile full-path, OPF manifest + spine, XHTML pages carrying
    ``<img src>`` tags. Image payloads reuse the ``_make_png`` bytes under
    ``.jpg`` names (the scanner lists entry names; it never decodes them).
    """
    _make_png(path.parent / "_epub_src.png")
    data = (path.parent / "_epub_src.png").read_bytes()
    (path.parent / "_epub_src.png").unlink()

    container_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        '  <rootfiles>\n'
        '    <rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/>\n'
        "  </rootfiles>\n"
        "</container>\n"
    )
    manifest_items = []
    spine_refs = []
    for i in range(pages):
        manifest_items.append(
            f'    <item id="p{i}" href="page{i}.xhtml" '
            'media-type="application/xhtml+xml"/>'
        )
        manifest_items.append(
            f'    <item id="img{i}" href="img{i}.jpg" media-type="image/jpeg"/>'
        )
        spine_refs.append(f'    <itemref idref="p{i}"/>')
    opf = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<package version="3.0" xmlns="http://www.idpf.org/2007/opf" '
        'unique-identifier="uid">\n'
        "  <metadata/>\n"
        "  <manifest>\n" + "\n".join(manifest_items) + "\n  </manifest>\n"
        "  <spine>\n" + "\n".join(spine_refs) + "\n  </spine>\n"
        "</package>\n"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", container_xml)
        zf.writestr("OEBPS/content.opf", opf)
        for i in range(pages):
            zf.writestr(
                f"OEBPS/page{i}.xhtml",
                '<html xmlns="http://www.w3.org/1999/xhtml">'
                f"<body><img src=\"img{i}.jpg\"/></body></html>",
            )
            zf.writestr(f"OEBPS/img{i}.jpg", data)


def _build_baseline(root: Path) -> None:
    """Build the exact T3 fixed-scale tree under *root*."""
    for i in range(N_ZIP):
        if i < N_ZIP_NESTED:
            names = ["ch1/01.jpg", "ch1/02.jpg", "ch2/01.jpg", "ch2/02.jpg"]
        else:
            names = [f"p{j}.jpg" for j in range(4)]
        _make_zip(root / f"zip{i:02d}.zip", names)
    for i in range(N_EPUB):
        _make_epub(root / f"book{i:02d}.epub", pages=N_EPUB_PAGES)
    for i in range(N_PDF):
        _make_pdf(root / f"doc{i:02d}.pdf", pages=N_PDF_PAGES)
    for i in range(N_PLAIN):
        d = root / f"folder{i:02d}"
        d.mkdir(parents=True)
        _make_png(d / "p1.png")
        _make_png(d / "p2.png")
        if i < N_TOML:
            (d / "metadata.toml").write_text(
                f'[comic]\ntitle_en = "baseline {i}"\n'
                '[tags]\nartist = ["baseline-artist"]\n'
            )


def _drive(coro):
    """Run *coro* on a fresh event loop in a helper thread.

    Same constraint as test_warmup_breakdown: Playwright's sync API leaves a
    running loop in the main thread, so asyncio.run() there fails.
    """
    outcome = {}

    def target():
        outcome["value"] = asyncio.run(coro)

    worker = threading.Thread(target=target)
    worker.start()
    worker.join()
    return outcome["value"]


def _state(downloads_path):
    return SimpleNamespace(
        settings=Settings(downloads_dir=str(downloads_path)),
        metadata_index_cache=None,
    )


def test_warmup_baseline_fixed_scale(tmp_path, caplog):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    _build_baseline(downloads)

    # Phase 1 (cold): direct warmup → scan_ms/rebuild_ms + species counts.
    # Fresh empty cache dir so T7's disk cache cannot warm-hit.
    cache_a = tmp_path / "cache-a"
    cache_a.mkdir()
    os.environ["DOWNLOADS_DIR"] = str(downloads)
    os.environ["BOOKQ_CACHE_DIR"] = str(cache_a)
    state_a = _state(downloads)
    with caplog.at_level(logging.INFO, logger="app.routes"):
        folders_a, images_a, _elapsed_a = _drive(warmup_metadata_index(state_a))
    rebuild_records = [
        r for r in caplog.records if r.msg.startswith("metadata index rebuilt")
    ]
    assert len(rebuild_records) == 1, [r.getMessage() for r in caplog.records]
    rebuild_msg = rebuild_records[0].getMessage()
    print(f"[baseline] {rebuild_msg}")

    # Structural assertions: species counts match the built fixture.
    # rar x0: binary generation impossible → exclusion recorded, rars=0.
    assert "archives=30" in rebuild_msg, rebuild_msg
    assert "epubs=10" in rebuild_msg, rebuild_msg
    assert "rars=0" in rebuild_msg, rebuild_msg
    assert "pdfs=10" in rebuild_msg, rebuild_msg
    assert "scan_ms=" in rebuild_msg, rebuild_msg
    assert "rebuild_ms=" in rebuild_msg, rebuild_msg

    # metadata.toml hit-rate input for T8: toml-bearing prefixes / scanned prefixes.
    index = state_a.metadata_index_cache["index"]
    toml_hits = sum(1 for c in index.comics if c.has_metadata)
    assert toml_hits == N_TOML, toml_hits
    toml_rate = toml_hits / folders_a if folders_a else 0.0
    print(
        f"[baseline] metadata.toml hits={toml_hits} "
        f"scanned_prefixes(folders)={folders_a} hit_rate={toml_rate:.3f}"
    )

    # Phase 2 (cold): full lifespan boot on a FRESH state + FRESH empty cache
    # dir → elapsed_ms/import_ms. Fresh state => cache-miss rescan; fresh
    # cache dir + memo clear => disk/in-process caches cannot warm-hit.
    from app import archive_pdf

    archive_pdf.clear_pdf_page_count_memo()
    cache_b = tmp_path / "cache-b"
    cache_b.mkdir()
    os.environ["BOOKQ_CACHE_DIR"] = str(cache_b)
    caplog.clear()
    state_b = _state(downloads)

    async def boot():
        async with app_main.lifespan(SimpleNamespace(state=state_b)):
            return state_b.metadata_index_cache

    with caplog.at_level(logging.INFO, logger="app.main"):
        with caplog.at_level(logging.INFO, logger="app.routes"):
            _drive(boot())
    done_records = [
        r for r in caplog.records if r.msg.startswith("内部ウォームアップ完了")
    ]
    assert len(done_records) == 1, [r.getMessage() for r in caplog.records]
    done_msg = done_records[0].getMessage()
    print(f"[baseline] {done_msg}")
    assert "elapsed_ms=" in done_msg, done_msg
    assert "import_ms=" in done_msg, done_msg
    # Lifespan rescan sees the same fixture species on its own rebuild line.
    lifespan_rebuilds = [
        r.getMessage()
        for r in caplog.records
        if r.msg.startswith("metadata index rebuilt")
    ]
    assert len(lifespan_rebuilds) == 1, lifespan_rebuilds
    print(f"[baseline] {lifespan_rebuilds[0]}")
    assert "archives=30" in lifespan_rebuilds[0], lifespan_rebuilds[0]
    assert "images=" in done_msg and images_a > 0
