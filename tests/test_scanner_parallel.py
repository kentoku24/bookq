"""Parallel-walk parity tests for scan_tree (issue #242).

``scan_tree`` walks the filesystem with a level-synchronous parallel BFS
(one ``ThreadPoolExecutor`` per call, per-directory scandir+stat+sort in
``_walk_dir``) and then assembles nodes recursively on the collected
records. ``_scan_tree_serial`` keeps the pre-#242 serial implementation
verbatim as the byte-parity reference. These tests pin that the two emit
the IDENTICAL tree — node order, single-child flattening, thumb/thumbs,
archive/PDF virtual folders, the ``_visited`` postcondition — including
the case where BFS and DFS claim order genuinely diverge (a symlink alias
reached at different depths), which the deferred ``emitted`` claiming in
the assembly pass exists to preserve.

Run:
    uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_scanner_parallel.py -q
"""

import json
import sys
from pathlib import Path

import pytest

# Ensure the repo root is on sys.path so "app" package is importable
# (same convention as tests/test_scanner.py)
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from app.scanner import _scan_tree_serial, scan_tree  # noqa: E402
from tests.test_scanner_perf_todo6 import _make_pdf, _make_png, _make_zip  # noqa: E402


def _build_rich(root: Path) -> None:
    """Fixture tree exercising every scan_tree behavior at once."""
    # Natural-sort directory order (ch-2 < ch-10).
    for name in ["ch-1", "ch-2", "ch-10"]:
        d = root / name
        d.mkdir()
        _make_png(d / "p1.png")
    # Deep single-child chain → full flatten to top-level image leaves.
    leaf = root / "chain" / "solo" / "leaf"
    leaf.mkdir(parents=True)
    _make_png(leaf / "1.png")
    _make_png(leaf / "2.png")
    # Flatten-over-leaf: group/episodes stays a folder row, episodes is hoisted.
    for ch in ("ch-1", "ch-2"):
        d = root / "group" / "episodes" / ch
        d.mkdir(parents=True)
        _make_png(d / "p1.png")
    other = root / "group" / "other"
    other.mkdir()
    _make_png(other / "1.png")
    # Folder holding BOTH an image and a subfolder.
    (root / "mixed" / "extra").mkdir(parents=True)
    _make_png(root / "mixed" / "cover.png")
    _make_png(root / "mixed" / "extra" / "1.png")
    # Empty directory node.
    (root / "empty").mkdir()
    # Dot entries are skipped at every level.
    (root / ".hidden").mkdir()
    _make_png(root / ".hidden" / "1.png")
    _make_png(root / ".dot.png")
    # Archive + PDF virtual folders (real enumeration, not opaque nodes).
    _make_zip(root / "book.zip", ["ch1/01.jpg", "ch1/02.jpg", "cover.jpg"])
    _make_pdf(root / "doc.pdf", pages=2)
    # Kanji-numeral natural sort + unicode names.
    for part in ("【第二部】", "【第一部】"):
        d = root / "kanji" / "s1" / part
        d.mkdir(parents=True)
        _make_png(d / "p1.png")
    keep = root / "kanji" / "s2"
    keep.mkdir()
    _make_png(keep / "1.png")
    # Symlink cycle: cyc/a/b/c points back at cyc — must terminate.
    cyc = root / "cyc" / "a" / "b"
    cyc.mkdir(parents=True)
    (cyc / "c").symlink_to(root / "cyc", target_is_directory=True)
    _make_png(cyc / "1.png")
    # Cross-depth inode alias: zB/c is reachable at depth 2 (zB/c) and at
    # depth 3 through zA/deep/link. Serial DFS claims it under zA (zA sorts
    # first and its whole subtree is walked before zB's classification), so
    # the content lands under zA/deep/link and zB stays childless. A naive
    # BFS claim would invert this — the assembly pass must not.
    (root / "zA" / "deep").mkdir(parents=True)
    (root / "zB" / "c").mkdir(parents=True)
    _make_png(root / "zB" / "c" / "img.png")
    (root / "zA" / "deep" / "link").symlink_to(
        root / "zB" / "c", target_is_directory=True
    )


def test_parallel_tree_is_identical_to_serial(tmp_path):
    _build_rich(tmp_path)
    parallel = scan_tree(tmp_path)
    serial = _scan_tree_serial(tmp_path)
    assert parallel == serial
    # json.dumps preserves dict insertion order — byte-level equality of the
    # serialized tree, catching even key-order drift inside node dicts.
    assert json.dumps(parallel) == json.dumps(serial)


def test_parallel_tree_is_identical_to_serial_shallow(tmp_path):
    """list_archives=False opaque-node mode stays byte-identical too."""
    _build_rich(tmp_path)
    parallel = scan_tree(tmp_path, list_archives=False)
    serial = _scan_tree_serial(tmp_path, list_archives=False)
    assert parallel == serial
    assert json.dumps(parallel) == json.dumps(serial)


def test_parallel_cross_depth_alias_keeps_serial_winner(tmp_path):
    """The aliased inode lands where DFS claimed it, not where BFS saw it first."""
    _build_rich(tmp_path)
    by_name = {n["name"]: n for n in scan_tree(tmp_path)}
    # zA's single child "deep" flattens; the aliased dir's image is hoisted
    # to "deep/link/img.png" — identical to the serial walk's placement.
    assert by_name["zA"]["children"] == [
        {"name": "deep/link/img.png", "type": "image", "children": None}
    ]
    assert by_name["zA"]["thumb"] == "deep/link/img.png"
    # zB/c was already claimed via zA's subtree → zB is left childless.
    assert by_name["zB"]["children"] == []
    assert by_name["zB"]["thumb"] is None


def test_parallel_visited_postcondition_matches_serial(tmp_path):
    """A caller-supplied _visited ends with the same inode set either way."""
    _build_rich(tmp_path)
    v_parallel: set = set()
    v_serial: set = set()
    scan_tree(tmp_path, _visited=v_parallel)
    _scan_tree_serial(tmp_path, _visited=v_serial)
    assert v_parallel == v_serial


def test_parallel_missing_and_empty_roots(tmp_path):
    missing = tmp_path / "nope"
    assert scan_tree(missing) == _scan_tree_serial(missing) == []
    empty = tmp_path / "empty"
    empty.mkdir()
    assert scan_tree(empty) == _scan_tree_serial(empty) == []


def test_parallel_file_root_raises_like_serial(tmp_path):
    """A file as *base*: exists() passes, iterdir/scandir raises ENOTDIR."""
    f = tmp_path / "afile.png"
    _make_png(f)
    with pytest.raises(NotADirectoryError):
        scan_tree(f)
    with pytest.raises(NotADirectoryError):
        _scan_tree_serial(f)


def test_parallel_phases_accumulator(tmp_path):
    """The _phases dict keeps all three counters; archive/pdf spans are real."""
    _build_rich(tmp_path)
    phases: dict[str, float] = {"walk_ms": 0.0, "archive_ms": 0.0, "pdf_ms": 0.0}
    tree = scan_tree(tmp_path, _phases=phases)
    assert tree, "fixture tree must be non-empty"
    assert phases["walk_ms"] >= 0
    assert phases["archive_ms"] > 0  # book.zip was enumerated
    assert phases["pdf_ms"] > 0  # doc.pdf was enumerated


def test_parallel_identical_on_conftest_tree(downloads_tree):
    """Parity on the full synthetic fixture used by the live-server tests."""
    assert scan_tree(downloads_tree) == _scan_tree_serial(downloads_tree)
