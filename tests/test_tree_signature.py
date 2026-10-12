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


# --- Todo 2 RED additions: parallel tree-signature acceptance gate ---
#
# NOTE (mandatory): these tests pin the NEW algorithm's determinism and
# composition, NOT cross-version equality — the digest VALUE will change when
# app/tree_signature.py is rewritten to per-dir sub-digests (todo 3). That is
# safe: signatures are compared only against in-memory
# cache_data["signature"] (app/routes.py:191,220,252-254,722-731) and never
# persisted; ETags derive from the payload sha1 (app/routes.py:486-515).
#
# NOTE on test_signature_unreadable_subdir_none (line 115): its docstring
# ("no per-entry swallowing") is superseded by the coming two-error-class
# spec (per-entry stat failure -> name-only + continue; scandir/root-stat
# failure -> whole-walk None). The test itself stays green (scandir failure
# -> None); its docstring may be updated in todo 3, not here.

import hashlib
import stat as statmod
from concurrent.futures import ThreadPoolExecutor

_REF_ARCHIVE_SUFFIXES = {".zip", ".cbz", ".rar", ".cbr", ".epub"}
_REF_PDF_SUFFIXES = {".pdf"}
_REF_TOML_NAME = "metadata.toml"


def _ref_dir_digest(path: Path, visited: set[tuple[int, int]]) -> bytes | None:
    """Independent sequential reference for the NEW per-dir composition.

    local(dir) = b"\\x00d" + mtime_ns (8-byte little-endian signed) +
    per entry in sorted-name order: b"\\x00n" + name (surrogateescape) plus
    b"\\x00f"/b"\\x00t" + mtime_ns + size for archives/PDFs/metadata.toml
    (suffix lowercased before membership); digest(dir) = sha256(local ||
    name + digest(child) for children in sorted-name order, child digest as
    raw 32 bytes, name as surrogateescape bytes with no extra tag).
    Dot-names skipped; one stat per entry (follow_symlinks); stat failure
    -> name only + continue; scandir failure or cycle-exhausted root ->
    None. Imports nothing from app.tree_signature.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    if not statmod.S_ISDIR(st.st_mode):
        return None
    ident = (st.st_dev, st.st_ino)
    if ident in visited:
        return None
    visited.add(ident)
    local = bytearray()
    local += b"\x00d" + st.st_mtime_ns.to_bytes(8, "little", signed=True)
    try:
        with os.scandir(path) as it:
            names = sorted(e.name for e in it if not e.name.startswith("."))
    except OSError:
        return None
    children: list[tuple[str, bytes]] = []
    for name in names:
        local += b"\x00n" + name.encode("utf-8", "surrogateescape")
        full = path / name
        try:
            est = full.stat()
        except OSError:
            continue  # broken symlink / exotic type: name only
        if statmod.S_ISDIR(est.st_mode):
            child = _ref_dir_digest(full, visited)
            if child is not None:
                children.append((name, child))
        elif statmod.S_ISREG(est.st_mode):
            if name == _REF_TOML_NAME:
                local += b"\x00t" + est.st_mtime_ns.to_bytes(8, "little", signed=True)
                local += est.st_size.to_bytes(8, "little", signed=True)
            elif Path(name).suffix.lower() in (_REF_ARCHIVE_SUFFIXES | _REF_PDF_SUFFIXES):
                local += b"\x00f" + est.st_mtime_ns.to_bytes(8, "little", signed=True)
                local += est.st_size.to_bytes(8, "little", signed=True)
    h = hashlib.sha256()
    h.update(bytes(local))
    for cname, cdigest in sorted(children, key=lambda t: t[0]):
        h.update(cname.encode("utf-8", "surrogateescape"))
        h.update(cdigest)
    return h.digest()


def _ref_compute(root: Path) -> str | None:
    visited: set[tuple[int, int]] = set()
    raw = _ref_dir_digest(root, visited)
    return raw.hex() if raw is not None else None


def _seed_reference_tree(root: Path) -> None:
    """Multi-level synthetic tree exercising every term class."""
    (root / "series" / "vol1").mkdir(parents=True)
    (root / "series" / "vol2").mkdir(parents=True)
    (root / "series" / "vol1" / "p1.png").write_bytes(b"\x89PNG" + b"0" * 64)
    (root / "series" / "vol1" / "metadata.toml").write_text('title_jp = "vol1"\n')
    (root / "top.zip").write_bytes(b"PK" + b"1" * 128)
    (root / "novel.epub").write_bytes(b"PK" + b"2" * 128)
    (root / "doc.pdf").write_bytes(b"%PDF" + b"3" * 128)
    (root / "UPPER.ZIP").write_bytes(b"PK" + b"4" * 128)
    (root / "series" / "vol2" / ".hidden-note").write_bytes(b"skip me")
    (root / ".root-hidden").write_bytes(b"skip me too")
    (root / "dangling").symlink_to(root / "no-such-target")


def test_matches_independent_reference(tmp_path: Path) -> None:
    """The NEW per-dir composition, reimplemented independently, must equal
    compute_signature over the full term-class tree."""
    _seed_reference_tree(tmp_path)
    expected = _ref_compute(tmp_path)
    actual = compute_signature(tmp_path)
    assert expected is not None and len(expected) == 64
    assert actual is not None and len(actual) == 64
    assert actual == expected


def test_deterministic_repeat_and_concurrent(tmp_path: Path) -> None:
    _seed(tmp_path)
    first = compute_signature(tmp_path)
    assert first is not None
    for _ in range(4):
        assert compute_signature(tmp_path) == first
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: compute_signature(tmp_path), range(8)))
    assert all(r == first for r in results)


def test_change_touch_archive_mtime(tmp_path: Path) -> None:
    """mtime-only touch (same size) on an archive must move the digest."""
    _seed(tmp_path)
    target = tmp_path / "book.zip"
    before = compute_signature(tmp_path)
    assert before is not None
    st = target.stat()
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    assert compute_signature(tmp_path) != before


def test_change_new_uppercase_zip(tmp_path: Path) -> None:
    _seed(tmp_path)
    before = compute_signature(tmp_path)
    (tmp_path / "NEW.ZIP").write_bytes(b"PK" + b"9" * 32)
    assert compute_signature(tmp_path) != before


def test_nochange_plain_image_rewrite_pinned(tmp_path: Path) -> None:
    """Plain images contribute their name only: with the parent dir mtime
    pinned back (hidden-file test pattern), an in-place rewrite must not
    move the digest."""
    _seed(tmp_path)
    parent = tmp_path / "series" / "vol1"
    img = parent / "p1.png"
    before = compute_signature(tmp_path)
    assert before is not None
    pst = parent.stat()
    img.write_bytes(b"\x89PNG" + b"1" * 4096)  # same name, different size+mtime
    os.utime(parent, ns=(pst.st_atime_ns, pst.st_mtime_ns))  # undo dir-mtime bump
    assert compute_signature(tmp_path) == before


def test_nochange_hidden_dotfile_add(tmp_path: Path) -> None:
    """Root-level dotfile add (parent mtime pinned) must not move the digest."""
    _seed(tmp_path)
    before = compute_signature(tmp_path)
    assert before is not None
    pst = tmp_path.stat()
    (tmp_path / ".hidden").write_bytes(b"secret")
    os.utime(tmp_path, ns=(pst.st_atime_ns, pst.st_mtime_ns))
    assert compute_signature(tmp_path) == before


def test_none_root_is_file(tmp_path: Path) -> None:
    target = tmp_path / "file.zip"
    target.write_bytes(b"PK" + b"0" * 16)
    assert compute_signature(target) is None


def test_broken_symlink_name_only(tmp_path: Path) -> None:
    """A dangling symlink contributes its name only and does NOT abort."""
    _seed(tmp_path)
    before = compute_signature(tmp_path)
    assert before is not None
    pst = tmp_path.stat()
    (tmp_path / "dangling").symlink_to(tmp_path / "no-such-target")
    os.utime(tmp_path, ns=(pst.st_atime_ns, pst.st_mtime_ns))
    after = compute_signature(tmp_path)
    assert after is not None
    assert after != before  # name hashed, walk not aborted
    assert compute_signature(tmp_path) == after  # stable


def test_symlink_cycle_terminates(tmp_path: Path) -> None:
    """Dir symlink to an ancestor terminates with a stable digest."""
    _seed(tmp_path)
    (tmp_path / "up-link").symlink_to(tmp_path / "series", target_is_directory=True)
    first = compute_signature(tmp_path)
    assert first is not None and len(first) == 64
    assert compute_signature(tmp_path) == first


def test_plain_images_not_statted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Plain images must cost ZERO entry.stat() calls (d_type fast path).

    Wraps os.scandir so every returned entry counts its own .stat() calls.
    Tree has nested dirs, archives, PDFs, metadata.toml, MANY plain images,
    a dangling symlink, and a dir symlink — all entry names unique so the
    per-name counts are unambiguous. Only dirs + archives/PDFs/metadata.toml
    + symlinks may be stat-ed; every plain image must stay at zero and the
    grand total must equal exactly dirs + specials + symlinks.
    """
    (tmp_path / "sub_a" / "nested").mkdir(parents=True)
    (tmp_path / "sub_b").mkdir(parents=True)
    plain_images = [
        "a1.png",
        "a2.jpg",
        "a3.webp",
        "n1.png",
        "n2.jpeg",
    ]
    for name in plain_images[:3]:
        (tmp_path / "sub_a" / name).write_bytes(b"\x89PNG" + b"0" * 32)
    for name in plain_images[3:]:
        (tmp_path / "sub_a" / "nested" / name).write_bytes(b"\x89PNG" + b"1" * 32)
    many_images = [f"bulk{i:02d}.png" for i in range(10)]
    for name in many_images:
        (tmp_path / "sub_b" / name).write_bytes(b"\x89PNG" + b"2" * 32)
    plain_images.extend(many_images)
    specials = ["data.zip", "doc.pdf", "metadata.toml", "inner.cbz", "notes.pdf"]
    (tmp_path / "sub_a" / "data.zip").write_bytes(b"PK" + b"3" * 32)
    (tmp_path / "sub_a" / "doc.pdf").write_bytes(b"%PDF" + b"4" * 32)
    (tmp_path / "sub_a" / "metadata.toml").write_text('title = "a"\n')
    (tmp_path / "sub_a" / "nested" / "inner.cbz").write_bytes(b"PK" + b"5" * 32)
    (tmp_path / "sub_b" / "notes.pdf").write_bytes(b"%PDF" + b"6" * 32)
    (tmp_path / "dangling").symlink_to(tmp_path / "no-such-target")
    (tmp_path / "dirlink").symlink_to(tmp_path / "sub_a", target_is_directory=True)

    real_scandir = os.scandir
    stat_calls: dict[str, int] = {}

    class _CountingEntry:
        def __init__(self, inner: os.DirEntry[str]) -> None:
            self._inner = inner

        @property
        def name(self) -> str:
            return self._inner.name

        @property
        def path(self) -> str:
            return self._inner.path

        def is_symlink(self) -> bool:
            return self._inner.is_symlink()

        def is_dir(self, *, follow_symlinks: bool = True) -> bool:
            return self._inner.is_dir(follow_symlinks=follow_symlinks)

        def is_file(self, *, follow_symlinks: bool = True) -> bool:
            return self._inner.is_file(follow_symlinks=follow_symlinks)

        def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
            stat_calls[self._inner.name] = stat_calls.get(self._inner.name, 0) + 1
            return self._inner.stat(follow_symlinks=follow_symlinks)

    class _CountingIterator:
        def __init__(self, inner: object) -> None:
            self._inner = inner  # type: ignore[no-untyped-call]

        def __iter__(self) -> "_CountingIterator":
            return self

        def __next__(self) -> _CountingEntry:
            return _CountingEntry(next(self._inner))  # type: ignore[no-untyped-call]

        def __enter__(self) -> "_CountingIterator":
            return self

        def __exit__(self, *args: object) -> object:
            return self._inner.__exit__(*args)  # type: ignore[no-untyped-call, no-any-return]

        def close(self) -> None:
            self._inner.close()  # type: ignore[no-untyped-call]

    def _counting_scandir(path: object, *args: object, **kwargs: object) -> _CountingIterator:
        return _CountingIterator(real_scandir(path, *args, **kwargs))  # type: ignore[no-untyped-call]

    monkeypatch.setattr(os, "scandir", _counting_scandir)
    sig = compute_signature(tmp_path)
    assert sig is not None and len(sig) == 64

    for img in plain_images:
        assert stat_calls.get(img, 0) == 0, f"plain image {img} was stat-ed"
    # Expected: 3 subdirs (sub_a, sub_b, nested) + 5 specials
    # (data.zip, doc.pdf, metadata.toml, inner.cbz, notes.pdf)
    # + 2 symlinks (dangling, dirlink) = 10.
    assert sum(stat_calls.values()) == 3 + 5 + 2
