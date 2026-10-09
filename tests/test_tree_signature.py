"""Tests for app.tree_signature.compute_signature (cache-gating digest).

TDD Red: this file is written BEFORE app/tree_signature.py exists, so every
test here must fail (ModuleNotFoundError) until the implementation lands.
"""

import os
from pathlib import Path

import pytest

from app.tree_signature import compute_signature


def _seed(root: Path) -> None:
    """Minimal library shape: a series dir, an archive, and a metadata.toml."""
    series = root / "series" / "vol1"
    series.mkdir(parents=True)
    (series / "p1.png").write_bytes(b"\x89PNG" + b"0" * 64)
    (root / "book.zip").write_bytes(b"PK" + b"1" * 128)
    (series / "metadata.toml").write_text('title_jp = "vol1"\n')


def test_signature_stable_repeat(tmp_path: Path) -> None:
    _seed(tmp_path)
    first = compute_signature(tmp_path)
    second = compute_signature(tmp_path)
    assert first is not None
    assert isinstance(first, str) and len(first) == 64
    int(first, 16)  # valid sha256 hexdigest
    assert second == first


def test_signature_changes_on_mkdir(tmp_path: Path) -> None:
    _seed(tmp_path)
    before = compute_signature(tmp_path)
    (tmp_path / "new_series").mkdir()
    assert compute_signature(tmp_path) != before


def test_signature_changes_on_new_zip(tmp_path: Path) -> None:
    _seed(tmp_path)
    before = compute_signature(tmp_path)
    (tmp_path / "extra.zip").write_bytes(b"PK" + b"2" * 64)
    assert compute_signature(tmp_path) != before


def test_signature_changes_on_new_epub(tmp_path: Path) -> None:
    _seed(tmp_path)
    before = compute_signature(tmp_path)
    (tmp_path / "novel.epub").write_bytes(b"PK" + b"3" * 64)
    assert compute_signature(tmp_path) != before


def test_signature_changes_on_new_pdf(tmp_path: Path) -> None:
    _seed(tmp_path)
    before = compute_signature(tmp_path)
    (tmp_path / "doc.pdf").write_bytes(b"%PDF" + b"4" * 64)
    assert compute_signature(tmp_path) != before


def test_signature_changes_on_uppercase_zip_rewrite(tmp_path: Path) -> None:
    """Suffixes are lowercased before set membership: rewriting UPPER.ZIP
    in place (no name-set change) must still move the digest — a plain
    name-only walk would stay identical here."""
    target = tmp_path / "BOOK.ZIP"
    target.write_bytes(b"PK" + b"5" * 64)
    before = compute_signature(tmp_path)
    assert before is not None
    target.write_bytes(b"PK" + b"5" * 1024)  # same name, different size+mtime
    os.utime(target, ns=(1_700_000_000_000_000_000, 1_700_000_001_000_000_000))
    assert compute_signature(tmp_path) != before


def test_signature_changes_on_metadata_toml_rewrite(tmp_path: Path) -> None:
    """In-place metadata.toml edits never bump the parent dir mtime, so the
    toml mtime+size term is mandatory — without it this stays identical."""
    _seed(tmp_path)
    toml = tmp_path / "series" / "vol1" / "metadata.toml"
    before = compute_signature(tmp_path)
    toml.write_text('title_jp = "vol1-extended-title"\nartist = ["a"]\n')
    assert compute_signature(tmp_path) != before


def test_signature_ignores_hidden_dotfile(tmp_path: Path) -> None:
    """Dot-names are skipped: with the parent dir mtime pinned back, adding
    a dotfile (or rewriting it) must not move the digest."""
    _seed(tmp_path)
    parent = tmp_path / "series" / "vol1"
    before = compute_signature(tmp_path)
    assert before is not None
    pst = parent.stat()
    hidden = parent / ".hidden"
    hidden.write_bytes(b"secret")
    os.utime(parent, ns=(pst.st_atime_ns, pst.st_mtime_ns))  # undo dir-mtime bump
    assert compute_signature(tmp_path) == before
    hidden.write_bytes(b"secret-extended")
    os.utime(parent, ns=(pst.st_atime_ns, pst.st_mtime_ns))
    assert compute_signature(tmp_path) == before


def test_signature_missing_root_none(tmp_path: Path) -> None:
    assert compute_signature(tmp_path / "no-such-dir") is None


def test_signature_symlink_cycle_terminates(tmp_path: Path) -> None:
    _seed(tmp_path)
    sub = tmp_path / "series" / "vol1"
    (sub / "loop").symlink_to(sub, target_is_directory=True)
    sig = compute_signature(tmp_path)
    assert sig is not None and len(sig) == 64  # visited once, no hang


def test_signature_unreadable_subdir_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Any walk-time OSError is a fail-open None (no per-entry swallowing)."""
    _seed(tmp_path)
    denied = tmp_path / "series"
    real_scandir = os.scandir

    def _boom(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if Path(path) == denied:
            raise PermissionError(13, "Permission denied")
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", _boom)
    assert compute_signature(tmp_path) is None
