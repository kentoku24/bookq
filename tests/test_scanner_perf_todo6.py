"""Todo6 characterization + failing-first perf tests (plan root-folder-perf-under-5s §6).

Part A — characterization (pins CURRENT output; passes before AND after):
  flatten-over-leaf (group/episodes style), archive, PDF, symlink cycle,
  corrupt-archive / corrupt-PDF / rar-unsupported never crash the scan.

Part B — failing-first (RED on unchanged code, GREEN after the refactor):
  B1: no per-folder ``_find_first_image`` re-DFS during ``scan_tree``.
  B2: ``pdf_page_count`` (mtime_ns, size)-keyed process-internal memo with
      hit stats; count-0 cached; key mismatch re-fetches; ``children``
      construction never skipped.
  B3: ``_facets_for`` generation-scoped per-path memo (identity-stable
      within a generation, recomputed for a new comics list), sorted order
      unchanged.

Run:
    uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_scanner_perf_todo6.py -q
"""

import os
import struct
import sys
import zipfile
import zlib
from pathlib import Path

os.environ["BOOKQ_TEST_MODE"] = "true"

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from app.metadata.models import ComicMetadata  # noqa: E402
from app.scanner import scan_tree  # noqa: E402


def _make_png(path: Path) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 40, 60, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes((200, 60, 60)) * 40
    idat = zlib.compress(row * 60)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
    )


def _make_pdf(path: Path, pages: int = 2) -> None:
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


def _make_zip(path: Path, names: list[str]) -> None:
    _make_png(path.parent / "_todo6_src.png")
    data = (path.parent / "_todo6_src.png").read_bytes()
    (path.parent / "_todo6_src.png").unlink()
    with zipfile.ZipFile(path, "w") as zf:
        for name in names:
            zf.writestr(name, data)


def _build_combo(root: Path) -> None:
    """One fixture tree covering all four acceptance cases."""
    # 1. group/episodes style: nested folder with chapter subfolders (uiux_tree shape).
    for ch in ("ch-1", "ch-2"):
        d = root / "group" / "episodes" / ch
        d.mkdir(parents=True)
        _make_png(d / "p1.png")
    # 2. archive with nested dirs.
    _make_zip(root / "book.zip", ["ch1/01.jpg", "ch1/02.jpg", "ch2/01.jpg", "cover.jpg"])
    # 3. PDF virtual folder.
    _make_pdf(root / "doc.pdf", pages=2)
    # 4. symlink cycle elsewhere (must not disturb siblings).
    cyc = root / "cyc" / "a" / "b"
    cyc.mkdir(parents=True)
    (cyc / "c").symlink_to(root / "cyc", target_is_directory=True)
    _make_png(cyc / "1.png")


# ── Part A: characterization (green before AND after) ─────────────────────────


def test_todo6_flatten_over_leaf_group_episodes(tmp_path):
    _build_combo(tmp_path)
    by_name = {n["name"]: n for n in scan_tree(tmp_path)}
    group = by_name["group"]
    assert group["type"] == "folder"
    assert [c["name"] for c in group["children"]] == ["episodes/ch-1", "episodes/ch-2"]
    for kid in group["children"]:
        assert kid["type"] == "folder"
        assert kid["thumb"] == "p1.png"
    assert group["thumb"] == "episodes/ch-1/p1.png"
    assert group["thumbs"] == ["episodes/ch-1/p1.png", "episodes/ch-2/p1.png"]
    # Pure single-child chain hoists leaves with the full prefix (name-only rewrite).
    chain = tmp_path / "chainroot"
    leaf = chain / "solo" / "a" / "b"
    leaf.mkdir(parents=True)
    for i in ("1.png", "2.png"):
        _make_png(leaf / i)
    hoisted = scan_tree(chain)
    assert [n["name"] for n in hoisted] == ["solo/a/b/1.png", "solo/a/b/2.png"]
    assert all(n["type"] == "image" for n in hoisted)


def test_todo6_archive_shape_pinned(tmp_path):
    _build_combo(tmp_path)
    node = next(n for n in scan_tree(tmp_path) if n["name"] == "book.zip")
    assert node["type"] == "folder"
    assert node["archive"] is True
    by_name = {c["name"]: c for c in node["children"]}
    assert set(by_name) == {"ch1", "ch2", "cover.jpg"}
    assert by_name["ch1"]["thumb"] == "01.jpg"
    # #226 split_archive_entries: a loose cover leads, so the archive cover is
    # the loose file (the old lane put ch1/01.jpg first). Nested archive roots
    # carry a multi-thumb grid when 2+ sub-folders bear images.
    assert node["thumb"] == "cover.jpg"
    assert node["thumbs"] == ["ch1/01.jpg", "ch2/01.jpg"]


def test_todo6_pdf_virtual_folder_pinned(tmp_path):
    _build_combo(tmp_path)
    node = next(n for n in scan_tree(tmp_path) if n["name"] == "doc.pdf")
    assert node["type"] == "folder"
    assert node["archive"] is True
    assert node["pdf"] is True
    assert [c["name"] for c in node["children"]] == ["p001.png", "p002.png"]
    assert node["thumb"] == "p001.png"


def test_todo6_symlink_cycle_does_not_break_siblings(tmp_path):
    _build_combo(tmp_path)
    by_name = {n["name"]: n for n in scan_tree(tmp_path)}
    assert set(by_name) == {"group", "book.zip", "doc.pdf", "cyc"}
    cyc = by_name["cyc"]
    assert [c["name"] for c in cyc["children"]] == ["a/b/1.png"]
    assert cyc["children"][0]["type"] == "image"


def test_todo6_corrupt_inputs_never_crash_scan(tmp_path):
    (tmp_path / "broken.zip").write_bytes(b"not a zip at all" * 16)
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.4 garbage \x00\xff" * 8)
    (tmp_path / "noextract.rar").write_bytes(b"Rar!\x1a\x07\x00" + b"\x00" * 64)
    (tmp_path / "ok").mkdir()
    _make_png(tmp_path / "ok" / "1.png")
    by_name = {n["name"]: n for n in scan_tree(tmp_path)}
    for bad in ("broken.zip", "broken.pdf", "noextract.rar"):
        assert by_name[bad]["type"] == "folder"
        assert by_name[bad]["children"] == []
        assert by_name[bad]["thumb"] is None
    assert by_name["ok"]["thumb"] == "1.png"


# ── Part B: failing-first (RED before, GREEN after) ───────────────────────────


def test_todo6_no_per_folder_find_first_image_dfs(tmp_path):
    """scan_tree must not re-DFS every folder for its first image (N+1)."""
    import app.scanner as scanner

    (tmp_path / "s" / "vol1").mkdir(parents=True)
    (tmp_path / "s" / "vol2").mkdir(parents=True)
    _make_png(tmp_path / "s" / "vol1" / "1.png")
    _make_png(tmp_path / "s" / "vol2" / "1.png")
    (tmp_path / "t").mkdir()
    _make_png(tmp_path / "t" / "1.png")
    if not hasattr(scanner, "_find_first_image"):
        return  # N+1 entry point removed — optimal, nothing to count.
    calls: list[int] = []
    orig = scanner._find_first_image

    def counting(nodes):
        calls.append(1)
        return orig(nodes)

    import unittest.mock as mock

    with mock.patch.object(scanner, "_find_first_image", counting):
        scan_tree(tmp_path)
    assert calls == [], f"_find_first_image re-DFS called {len(calls)}x (N+1 still present)"


def test_todo6_pdf_page_count_memoized(tmp_path):
    """(path, mtime_ns, size) memo: 2nd identical call is a hit; children intact."""
    from app import archive_pdf

    assert hasattr(archive_pdf, "_PDF_COUNT_MEMO"), "no process-internal PDF memo"
    archive_pdf.clear_pdf_page_count_memo()
    pdf = tmp_path / "doc.pdf"
    _make_pdf(pdf, pages=2)
    assert archive_pdf.pdf_page_count(pdf) == 2
    misses_before = archive_pdf.pdf_memo_stats()["misses"]
    assert archive_pdf.pdf_page_count(pdf) == 2
    stats = archive_pdf.pdf_memo_stats()
    assert stats["hits"] >= 1, f"2nd identical pdf_page_count was not a memo hit: {stats}"
    assert stats["misses"] == misses_before
    # Memo must never skip children construction.
    node = next(n for n in scan_tree(tmp_path) if n["name"] == "doc.pdf")
    assert [c["name"] for c in node["children"]] == ["p001.png", "p002.png"]
    assert node["thumb"] == "p001.png"


def test_todo6_pdf_memo_count_zero_and_remismatch(tmp_path):
    """count-0 is cached; mtime/size change re-fetches (grown pages visible)."""
    import os as _os

    from app import archive_pdf

    assert hasattr(archive_pdf, "_PDF_COUNT_MEMO"), "no process-internal PDF memo"
    archive_pdf.clear_pdf_page_count_memo()
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"garbage, not a pdf")
    assert archive_pdf.pdf_page_count(bad) == 0
    assert archive_pdf.pdf_page_count(bad) == 0
    assert archive_pdf.pdf_memo_stats()["hits"] >= 1
    grown = tmp_path / "grown.pdf"
    _make_pdf(grown, pages=1)
    assert archive_pdf.pdf_page_count(grown) == 1
    hits_before = archive_pdf.pdf_memo_stats()["hits"]
    _make_pdf(grown, pages=3)  # same path, new size+mtime → key mismatch
    _os.utime(grown, ns=(0, 0))  # force mtime change even on coarse filesystems
    assert archive_pdf.pdf_page_count(grown) == 3
    assert archive_pdf.pdf_memo_stats()["hits"] == hits_before


def test_todo6_subtree_facets_memoized_per_generation():
    """Same comics list + path → identical memoized object; new list → recompute."""
    from app.metadata import facets as facets_mod

    comics = [
        ComicMetadata(folder_path="series/a", folder_name="a",
                      artist=["A1"], tags=["t1"], has_metadata=True),
        ComicMetadata(folder_path="series/b", folder_name="b",
                      artist=["A2"], tags=["t2"], has_metadata=True),
        ComicMetadata(folder_path="other", folder_name="other",
                      artist=["A9"], tags=["t9"], has_metadata=True),
    ]
    whole = facets_mod._build_facets(comics)
    first = facets_mod._facets_for(comics, "series", whole)
    second = facets_mod._facets_for(comics, "series", whole)
    assert second is first, "per-path facets were recomputed within one generation"
    assert first == {"artists": ["A1", "A2"], "tags": ["t1", "t2"]}
    # A new generation (rebuilt comics list) must not reuse the old entry.
    gen2 = [ComicMetadata(folder_path="series/a", folder_name="a",
                          artist=["ZZ"], tags=[], has_metadata=True)]
    whole2 = facets_mod._build_facets(gen2)
    third = facets_mod._facets_for(gen2, "series", whole2)
    assert third is not first
    assert third == {"artists": ["ZZ"], "tags": []}
