"""W3+W4 parity harness (issue #163).

Covers the native FS-walk ports: ``scan_fs`` → ``scan_tree`` assembly,
``tree_signature_rs`` → ``compute_signature``, ``list_images_rs`` →
``list_images``, and ``meta_walk`` / ``parse_metadata_batch`` →
``MetadataIndex.rebuild`` / ``rebuild_from_tree``.

Every case diffs the production wrapper against the ``_py_*`` reference on
the same inputs — identical structure/digest/list, or an identical answer
after an expected fallback. Skipped when the wheel is absent.
"""

import os
import sys
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    __import__("importlib").util.find_spec("bookq_rs") is None,
    reason="bookq_rs wheel not installed",
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import app.metadata.index as MI  # noqa: E402
import app.scanner as S  # noqa: E402
import app.tree_signature as TS  # noqa: E402


def _mk_tree(base: Path) -> Path:
    """Mixed fixture: nested dirs, flatten chain, images, archive, pdf, toml."""
    (base / "a/第2話").mkdir(parents=True)
    (base / "a/第10話").mkdir(parents=True)
    (base / "a/第2話/1.png").write_bytes(b"x")
    (base / "a/第2話/02.png").write_bytes(b"x")
    (base / "a/第10話/1.png").write_bytes(b"x")
    (base / "solo/only/deep").mkdir(parents=True)
    (base / "solo/only/deep/p.png").write_bytes(b"x")
    (base / "empty_dir").mkdir()
    with zipfile.ZipFile(base / "a/book.cbz", "w") as zf:
        zf.writestr("i.png", b"\x89PNG")
    (base / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
    (base / ".hidden.png").write_bytes(b"x")
    (base / ".hidden_dir").mkdir()
    (base / "a/metadata.toml").write_text(
        '[comic]\ntitle_jp="表題"\ntitle_en="Title"\ntotal_pages=42\n'
        '[source]\nname="dl"\n'
        '[tags]\nartist=["a | b","solo"]\nmale=["m1"]\nfemale=["f1,f2"]\n'
        'tag=["t1","t1"]\n',
        encoding="utf-8",
    )
    (base / "solo/metadata.toml").write_text("[comic]\ntitle_jp=\"x\"\n")
    (base / "empty_dir/metadata.toml").write_text("not toml [[[\n")
    return base


def test_scan_tree_parity(tmp_path):
    base = _mk_tree(tmp_path / "lib")
    assert S.scan_tree(base) == S._py_scan_tree(base)
    # shallow mode
    assert S.scan_tree(base, list_archives=False) == S._py_scan_tree(
        base, list_archives=False)
    # missing root
    assert S.scan_tree(base / "nope") == S._py_scan_tree(base / "nope") == []


def test_scan_tree_symlink_cycle(tmp_path):
    base = _mk_tree(tmp_path / "lib")
    os.symlink(base, base / "loop")
    assert S.scan_tree(base) == S._py_scan_tree(base)


def test_signature_parity(tmp_path):
    base = _mk_tree(tmp_path / "lib")
    assert TS.compute_signature(base) == TS._py_compute_signature(base)
    os.symlink(base / "a", base / "alink")
    assert TS.compute_signature(base) == TS._py_compute_signature(base)
    os.symlink(base / "a/gone.png", base / "dangling.png")
    assert TS.compute_signature(base) == TS._py_compute_signature(base)
    assert TS.compute_signature(base / "nope") == TS._py_compute_signature(
        base / "nope") is None
    # signature reacts to content identity, not just names
    (base / "a/new.zip").write_bytes(b"z")
    assert TS.compute_signature(base) == TS._py_compute_signature(base)


def test_list_images_parity(tmp_path):
    base = _mk_tree(tmp_path / "lib")
    for sub in ["", "a", "a/第2話", "empty_dir"]:
        p = base / sub if sub else base
        assert S.list_images(p) == S._py_list_images(p), sub


def _comics(idx):
    return [c.to_dict() for c in idx.comics]


def test_meta_walk_parity(tmp_path):
    base = _mk_tree(tmp_path / "lib")
    i1 = MI.MetadataIndex()
    i1.rebuild(base)
    i2 = MI.MetadataIndex()
    i2.comics = i2._py_walk(base, base)
    assert _comics(i1) == _comics(i2)
    assert i1.comics, "fixture should index comics"


def test_rebuild_from_tree_parity(tmp_path):
    base = _mk_tree(tmp_path / "lib")
    tree = S._py_scan_tree(base)
    i1 = MI.MetadataIndex()
    i1.rebuild_from_tree(tree, base)
    # force the pure-Python path for the reference
    saved = MI._rs
    MI._rs = None
    try:
        i2 = MI.MetadataIndex()
        i2.rebuild_from_tree(tree, base)
    finally:
        MI._rs = saved
    assert _comics(i1) == _comics(i2)


def test_meta_edge_cases(tmp_path):
    """total_pages conversions + non-str artist + unparseable toml."""
    base = tmp_path / "lib"
    base.mkdir()
    (base / "f1").mkdir()
    (base / "f1/metadata.toml").write_text(
        '[comic]\ntotal_pages="12"\n[tags]\nartist={"k"=1}\n')
    (base / "f2").mkdir()
    (base / "f2/metadata.toml").write_text('[comic]\ntotal_pages=1_0\n')
    (base / "f3").mkdir()
    (base / "f3/metadata.toml").write_text('[comic]\ntotal_pages="nope"\n')
    (base / "f4").mkdir()
    (base / "f4/metadata.toml").write_text('[comic]\ntotal_pages=3.9\n')
    i1 = MI.MetadataIndex()
    i1.rebuild(base)
    i2 = MI.MetadataIndex()
    i2.comics = i2._py_walk(base, base)
    assert _comics(i1) == _comics(i2)
