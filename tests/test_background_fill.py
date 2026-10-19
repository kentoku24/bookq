"""Background fill — materialized archive covers reach the parent folder tile.

The startup warmup scans with ``list_archives=False`` (opaque archives:
``children == []``, ``thumb is None``), so a folder whose images live ONLY
inside archives (``rawmanga/<work>``-shaped works: many volumes, no loose
images) has no cover of its own to compute. ``_background_fill``
materializes those archives one by one; without recomputing the ancestors'
``thumb``/``thumbs`` from the enriched children, the folder keeps
``thumb None`` for the whole process lifetime — its browse tile renders the
folder placeholder instead of a cover — because the TTL/signature refresh
re-walks an *unchanged* filesystem and skips the rescan that would fix it.

Failing-first: ``test_fill_propagates_archive_cover_to_parent_folder`` fails
on today's code (parent ``thumb`` stays None). The companion property test
pins the stronger contract the fix must satisfy: shallow scan + fill must end
identical to a plain full ``scan_tree``.

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_background_fill.py -q
"""

import os
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

os.environ["BOOKQ_TEST_MODE"] = "true"

from app.config import Settings  # noqa: E402
from app.routes import _background_fill  # noqa: E402
from app.scanner import scan_tree  # noqa: E402

from tests.helpers import drive, make_pdf  # noqa: E402


def _make_zip(path: Path, names: list[str]) -> None:
    """Zip holding *names* as placeholder PNG bytes (never decoded here)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name in names:
            zf.writestr(name, b"\x89PNG\r\n\x1a\n")


def _shallow_state(downloads: Path):
    """Fake app state carrying a shallow (archive-opaque) tree, like startup."""
    from app.metadata import MetadataIndex

    tree = scan_tree(downloads, list_archives=False)
    return SimpleNamespace(
        settings=Settings(downloads_dir=str(downloads)),
        metadata_index_cache={
            "tree": tree,
            "index": MetadataIndex(),
            "timestamp": time.time(),
            "generation": 1,
        },
        last_request_at=0.0,
    )


def _fast_fill(monkeypatch):
    """Let the fill run without idle waits or per-item sleeps."""
    import app.routes as routes_mod

    monkeypatch.setattr(routes_mod, "_FILL_IDLE_SECONDS", 0.0)
    monkeypatch.setattr(routes_mod, "_FILL_STEP_SECONDS", 0.0)
    return routes_mod


def _find(nodes, path, prefix=""):
    """Locate a node by its full path (composed parent/name, never split)."""
    for node in nodes:
        full = f"{prefix}/{node['name']}" if prefix else node["name"]
        if full == path:
            return node
        found = _find(node.get("children") or [], path, full)
        if found is not None:
            return found
    return None


def test_fill_propagates_archive_cover_to_parent_folder(tmp_path, monkeypatch):
    """A folder holding only archives must gain the first volume's cover."""
    _fast_fill(monkeypatch)
    downloads = tmp_path / "downloads"
    _make_zip(downloads / "series" / "vol-01.zip", ["01.png", "02.png"])
    _make_zip(downloads / "series" / "vol-02.zip", ["01.png"])
    # A sibling top-level dir keeps "series" a node of its own (no flattening).
    (downloads / "plain").mkdir(parents=True)
    state = _shallow_state(downloads)
    assert _find(state.metadata_index_cache["tree"], "series")["thumb"] is None, (
        "fixture must start from the shallow (cover-less) shape"
    )

    async def main():
        await _background_fill(state)
        return True

    assert drive(main())
    series = _find(state.metadata_index_cache["tree"], "series")
    assert series["thumb"] == "vol-01.zip/01.png", (
        f"parent folder cover not propagated after fill: {series['thumb']!r}"
    )
    assert series["thumbs"] == ["vol-01.zip/01.png", "vol-02.zip/01.png"], (
        f"multi-thumb grid not propagated after fill: {series.get('thumbs')!r}"
    )


def test_fill_propagates_through_every_ancestor(tmp_path, monkeypatch):
    """Nested archive-only folders: every ancestor up to the root gets a cover."""
    _fast_fill(monkeypatch)
    downloads = tmp_path / "downloads"
    _make_zip(downloads / "outer" / "inner" / "vol-01.zip", ["cover.png"])
    # A second entry in outer (no images) keeps the node from being flattened.
    _make_zip(downloads / "outer" / "extra.zip", ["readme.txt"])
    (downloads / "plain").mkdir(parents=True)
    state = _shallow_state(downloads)

    async def main():
        await _background_fill(state)
        return True

    assert drive(main())
    tree = state.metadata_index_cache["tree"]
    assert _find(tree, "outer/inner/vol-01.zip")["thumb"] == "cover.png"
    assert _find(tree, "outer/inner")["thumb"] == "vol-01.zip/cover.png"
    assert _find(tree, "outer")["thumb"] == "inner/vol-01.zip/cover.png", (
        f"ancestor above the direct parent kept no cover: {_find(tree, 'outer')['thumb']!r}"
    )


def test_filled_shallow_tree_equals_full_scan(tmp_path, monkeypatch):
    """shallow scan + fill ends identical to the plain full scan."""
    _fast_fill(monkeypatch)
    downloads = tmp_path / "downloads"
    _make_zip(downloads / "only-archives" / "c1.zip", ["p1.png", "p2.png"])
    _make_zip(downloads / "only-archives" / "c2.zip", ["p1.png"])
    _make_zip(downloads / "mixed" / "book.zip", ["ch1/01.png", "cover.png"])
    (downloads / "mixed" / "loose").mkdir(parents=True)
    (downloads / "mixed" / "loose" / "01.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    _make_zip(downloads / "deep" / "wrap" / "v1.zip", ["01.png", "02.png"])
    # The fill's other materialize branches under flattened names:
    # _scan_pdf_node (pdf flag) and the EPUB early-return path.
    (downloads / "pdfs" / "wrap").mkdir(parents=True)
    make_pdf(downloads / "pdfs" / "wrap" / "doc.pdf", pages=2)
    _make_zip(downloads / "epubs" / "wrap" / "book.epub", ["OEBPS/Images/p01.png"])
    state = _shallow_state(downloads)

    async def main():
        await _background_fill(state)
        return True

    assert drive(main())
    assert state.metadata_index_cache["tree"] == scan_tree(downloads)


def test_api_folders_reports_cover_for_archive_only_folder(tmp_path, monkeypatch):
    """End-to-end: the served tree gains the cover once the startup fill runs.

    The reported symptom at the API boundary — a work whose content is only
    archives served ``thumb: null`` (the client then paints the folder
    placeholder). Runs the real lifespan (shallow warmup + background fill).
    """
    _fast_fill(monkeypatch)
    downloads = tmp_path / "downloads"
    _make_zip(downloads / "series" / "vol-01.zip", ["01.png", "02.png"])
    _make_zip(downloads / "series" / "vol-02.zip", ["01.png"])
    (downloads / "plain").mkdir(parents=True)
    monkeypatch.setenv("DOWNLOADS_DIR", str(downloads))
    from app.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as client:
        deadline = time.monotonic() + 10
        series = None
        while time.monotonic() < deadline:
            body = client.get("/api/folders").json()
            series = _find(body, "series")
            # The fill materializes archives one at a time: thumb lands after
            # vol-01, thumbs only once vol-02 is filled too — wait for both.
            if series and series.get("thumb") and series.get("thumbs"):
                break
            time.sleep(0.1)

    assert series is not None, "fixture folder missing from the served tree"
    assert series["thumb"] == "vol-01.zip/01.png", (
        f"archive-only work served without a cover: {series['thumb']!r}"
    )
    assert series["thumbs"] == ["vol-01.zip/01.png", "vol-02.zip/01.png"]


def test_fill_never_invents_a_cover_for_imageless_archives(tmp_path, monkeypatch):
    """An archive with no images leaves its parent's cover empty."""
    _fast_fill(monkeypatch)
    downloads = tmp_path / "downloads"
    (downloads / "series").mkdir(parents=True)
    with zipfile.ZipFile(downloads / "series" / "empty.zip", "w") as zf:
        zf.writestr("readme.txt", b"no images here")
    # A sibling top-level dir keeps "series" a node of its own (no flattening).
    (downloads / "plain").mkdir(parents=True)
    state = _shallow_state(downloads)

    async def main():
        await _background_fill(state)
        return True

    assert drive(main())
    series = _find(state.metadata_index_cache["tree"], "series")
    assert series["thumb"] is None
    assert "thumbs" not in series
