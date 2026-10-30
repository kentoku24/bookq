"""W2 archive-reader parity harness (issue #163).

Every case asserts the *production wrapper* path (``archive._zip_names`` /
``_rar_names`` / ``_epub_entries`` / ``_read_zip``) equals the ``_py_*``
reference implementation. Where the Rust call is expected to prove an answer
it is also diffed directly; ``None`` from Rust means "fallback" and is only
acceptable when the wrapper still matches Python.

Skipped when the wheel is not installed (CI builds it; local runs need
``maturin build`` first).
"""

import os
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    __import__("importlib").util.find_spec("bookq_rs") is None,
    reason="bookq_rs wheel not installed",
)

import bookq_rs  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import app.archive as A  # noqa: E402
from tests.fixtures.rar5 import make_rar5_store  # noqa: E402

PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 32


def _mkzip(path, entries, comment=None):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries:
            zf.writestr(name, data)
        if comment:
            zf.comment = comment
    return path


@pytest.fixture()
def corpus(tmp_path):
    out = {}
    out["simple.zip"] = _mkzip(tmp_path / "simple.zip",
                               [("b/2.png", PNG), ("a.png", PNG), ("b/10.png", PNG)])
    out["uni.zip"] = _mkzip(tmp_path / "uni.zip",
                            [("第10話/表紙.png", PNG), ("第2話/1.png", PNG),
                             ("dir/", b""), ("café.png", PNG)])
    p = tmp_path / "cp437.zip"
    with zipfile.ZipFile(p, "w") as zf:
        for raw in [b"caf\xe9.png", b"\x99resen.png", b"a\x80z.png"]:
            zi = zipfile.ZipInfo(raw.decode("cp437"))
            zi.flag_bits &= ~0x800
            zf.writestr(zi, PNG)
    out["cp437.zip"] = p
    out["comment.zip"] = _mkzip(tmp_path / "comment.zip", [("x.png", PNG)],
                                comment=b"c" * 900)
    plain = _mkzip(tmp_path / "plain.zip", [("a.png", PNG), ("b.png", PNG)])
    sfx = tmp_path / "sfx.zip"
    sfx.write_bytes(b"MZ\x90\x00" + os.urandom(200) + plain.read_bytes())
    out["sfx.zip"] = sfx
    p = tmp_path / "dup.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("a.png", PNG)
        zf.writestr("a.png", PNG + b"x")
        zf.writestr("b.png", PNG)
    out["dup.zip"] = p
    out["unsafe.zip"] = _mkzip(tmp_path / "unsafe.zip",
                               [("../evil.png", PNG), ("/abs.png", PNG),
                                ("dir/.hidden.png", PNG), ("ok/good.png", PNG),
                                ("C:/win.png", PNG), ("./dot.png", PNG)])
    out["macosx.zip"] = _mkzip(tmp_path / "macosx.zip",
                               [("__MACOSX/._a.png", PNG), ("real/1.png", PNG)])
    out["empty.zip"] = _mkzip(tmp_path / "empty.zip", [])
    p = tmp_path / "notazip.zip"
    p.write_bytes(b"not a zip at all" * 8)
    out["notazip.zip"] = p
    p = tmp_path / "trunc.zip"
    p.write_bytes(plain.read_bytes()[: len(plain.read_bytes()) - 30])
    out["trunc.zip"] = p
    p = tmp_path / "utf8flag.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zi = zipfile.ZipInfo("日本語.png")
        zi.flag_bits |= 0x800
        zf.writestr(zi, PNG)
    out["utf8flag.zip"] = p
    p = tmp_path / "bzip2.zip"
    with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_BZIP2) as zf:
        zf.writestr("a.png", PNG)
    out["bzip2.zip"] = p
    p = tmp_path / "deflate.zip"
    with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("big.bin.png", os.urandom(60000))
        zf.writestr("text.png", b"A" * 50000)
    out["deflate.zip"] = p
    # NUL + backslash + dotfile-ish names
    p = tmp_path / "weird.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("a\x00evil.png", PNG)
        zf.writestr("win\\path.png", PNG)
        zf.writestr("trail/.png", PNG)
    out["weird.zip"] = p

    # RAR5
    make_rar5_store(tmp_path / "simple.rar", [("b/2.png", PNG), ("a.png", PNG)])
    make_rar5_store(tmp_path / "uni.cbr", [("第10話/表紙.png", PNG), ("café.png", PNG)])
    make_rar5_store(tmp_path / "many.rar",
                    [(f"d{i % 3}/p{i:03d}.png", PNG + bytes([i])) for i in range(80)])
    (tmp_path / "notarar.rar").write_bytes(b"Rar!not really" + b"\x00" * 64)
    (tmp_path / "empty.rar").write_bytes(b"")
    (tmp_path / "sfx.cbr").write_bytes(b"MZ" + os.urandom(300)
                                     + (tmp_path / "simple.rar").read_bytes())
    for n in ["simple.rar", "uni.cbr", "many.rar", "notarar.rar", "empty.rar", "sfx.cbr"]:
        out[n] = tmp_path / n

    # EPUB
    def epub(path, pages, extra=None, container_rootfile="OEBPS/content.opf"):
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("META-INF/container.xml",
                        '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                        f'<rootfiles><rootfile full-path="{container_rootfile}"/>'
                        "</rootfiles></container>")
            items = "".join(
                f'<item id="p{i}" href="pages/p{i}.xhtml" media-type="application/xhtml+xml"/>'
                for i in range(len(pages)))
            spine = "".join(f'<itemref idref="p{i}"/>' for i in range(len(pages)))
            zf.writestr(container_rootfile,
                        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                        f"<manifest>{items}</manifest><spine>{spine}</spine></package>")
            for i, html in enumerate(pages):
                zf.writestr(f"OEBPS/pages/p{i}.xhtml", html)
            for arc, data in (extra or {}).items():
                zf.writestr(arc, data)
        return path

    out["manga.epub"] = epub(tmp_path / "manga.epub",
        ['<?xml version="1.0"?><!DOCTYPE html><html><body>'
         '<img src="../img/2.png"/><img src="../img/10.png"/></body></html>',
         '<?xml version="1.0"?><!DOCTYPE html><html><body><img src="../img/1.png"/></body></html>'],
        extra={"OEBPS/img/1.png": PNG, "OEBPS/img/2.png": PNG, "OEBPS/img/10.png": PNG})
    out["svg.epub"] = epub(tmp_path / "svg.epub",
        ['<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
         '<image xlink:href="../img/a%20b.png"/></svg>'],
        extra={"OEBPS/img/a b.png": PNG})
    p = tmp_path / "nocontainer.epub"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("z/2.png", PNG)
        zf.writestr("a/1.png", PNG)
    out["nocontainer.epub"] = p
    (tmp_path / "broken.epub").write_bytes(b"garbage" * 50)
    out["broken.epub"] = tmp_path / "broken.epub"
    out["empty.epub"] = tmp_path / "empty.epub"
    out["empty.epub"].write_bytes(b"")
    return out


def test_zip_names_parity(corpus):
    for name, p in corpus.items():
        if p.suffix.lower() in (".zip", ".cbz"):
            assert A._zip_names(p) == A._py_zip_names(p), name


def test_zip_names_raw_when_provable(corpus):
    """Where Rust proves an answer it must equal the Python one exactly."""
    for name, p in corpus.items():
        if p.suffix.lower() in (".zip", ".cbz"):
            got = bookq_rs.zip_names(str(p))
            py = A._py_zip_names(p)
            if got is not None:
                assert got == py, name


def test_rar_names_parity(corpus):
    for name, p in corpus.items():
        if p.suffix.lower() in (".rar", ".cbr"):
            assert A._rar_names(p) == A._py_rar_names(p), name
            got = bookq_rs.rar_names(str(p))
            py = A._py_rar_names(p)
            if got is not None:
                assert [n for n, d in got if not d] == py, name


def test_epub_parity(corpus):
    for name, p in corpus.items():
        if p.suffix.lower() == ".epub":
            assert A._epub_entries(p) == A._py_epub_entries(p), name


def test_read_zip_parity(corpus):
    for name, p in corpus.items():
        if p.suffix.lower() not in (".zip", ".cbz", ".epub"):
            continue
        for inner in A._py_zip_names(p):
            if not A._is_image_entry(inner):
                continue
            assert A._read_zip(p, inner) == A._py_read_zip(p, inner), (name, inner)
        assert A._read_zip(p, "nope/missing.png") == A._py_read_zip(p, "nope/missing.png")


def test_list_archive_entries_parity(corpus, monkeypatch):
    """End-to-end listing through the real cached entry point."""
    monkeypatch.setattr(A.warmup_cache, "get_archive_entries", lambda *a, **k: None)
    monkeypatch.setattr(A.warmup_cache, "cache_key_for_path", lambda *a, **k: None)
    for name, p in corpus.items():
        suf = p.suffix.lower()
        if suf in A.RAR_EXTENSIONS:
            expected = A._finalize(A._py_rar_names(p))
        elif suf in A.EPUB_EXTENSIONS:
            expected = A._py_epub_entries(p)
        else:
            expected = A._finalize(A._py_zip_names(p))
        assert A.list_archive_entries(p) == expected, name


def test_bookq_rust_zero_fallback(tmp_path):
    """BOOKQ_RUST=0 must force the Python path entirely."""
    p = tmp_path / "t.zip"
    _mkzip(p, [("a.png", PNG)])
    env = dict(os.environ, BOOKQ_RUST="0")
    code = (
        "from pathlib import Path\n"
        "import app.archive as A\n"
        "assert A._rs is None, 'dispatch must be off'\n"
        f"assert A._zip_names(Path({str(p)!r})) == ['a.png']\n"
    )
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                       cwd=Path(__file__).resolve().parents[2])
    assert r.returncode == 0, r.stderr
