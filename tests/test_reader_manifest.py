"""S04 — shared work manifest from existing enumeration (#79).

``resolve_work(connection, openedFolder, initialPageRef, offset)`` builds the
``{chapters, pageRefs, spreadTable, digest}`` manifest both devices share, on
top of the SAME scan_tree tree + /api/images enumeration the solo viewer
uses (no second implementation). Deterministic and transport-free: a stub
connection carries ``app.state`` exactly like a real HTTPConnection.
"""

import json
import struct
import zlib
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.reader_sync import catalog, model, protocol
from tests.helpers import drive, make_pdf
from tests.fixtures.rar5 import make_rar5_store


def _make_png(path: Path, rgb: tuple = (60, 120, 200)) -> None:
    """Minimal solid-color PNG, same style as tests/conftest.py."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 40, 60, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(rgb) * 40
    idat = zlib.compress(row * 60)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
    )


def _png_bytes(rgb: tuple = (60, 120, 200)) -> bytes:
    import io
    import struct as _struct
    import zlib as _zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            _struct.pack(">I", len(data))
            + tag
            + data
            + _struct.pack(">I", _zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = _struct.pack(">IIBBBBB", 40, 60, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(rgb) * 40
    idat = _zlib.compress(row * 60)
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
    )


def _make_zip(path: Path, entries: list[str], payload: bytes) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name in entries:
            zf.writestr(name, payload)


@pytest.fixture(scope="module")
def manifest_tree(tmp_path_factory) -> Path:
    """Dedicated manifest tree (the shared session tree has no archives).

        root/
          r1.png r2.png                      root loose images (openedFolder="")
          simple/      1 2 3 10.png          depth-1 leaf, natural sort
          series/vol1 p1 p2 ; series/vol2 p1 depth-1 parent → NO sibling concat
          flat_wrap/inner/vol1 p1 p2         flat_wrap single-child → flattened
                     /inner/vol2 p1          names "inner/vol*", depth>=2 concat
          grp/series/v01 p1 p2               depth-2 concat; v02 excluded (mixed)
                   /v02 m1 m2 m3 + sub/s1 s2
                   /v03 p1 p2 ; grp/other c1 (keeps grp unflattened)
          grp2/run/lead/x1 a + /x2 b         lead = folder-children-only node
              /run/ch1 c1 c2 ; /run/ch2 d1   → concat siblings ch1+ch2 anyway
              grp2/other o
          kanji/s1/【第二部】【第三部】【第十部】 p1   kanji natural order
               /s2 k1                        (keeps kanji unflattened)
          book.cbz     p01 p02 p03           flat archive leaf
          nested.zip   v01/1 v01/2 v02/1     archive with inner dirs
          arch_run/arc/w1.cbz c1 c2          archives as concat chapters
                   /arc/w2.cbz d1
                   /arc/w3.cbr e1            (RAR5 store, no compressor needed)
          arch_run/other o
          doc.pdf      3 pages               pdf virtual folder
          broken.zip   not-a-zip bytes       broken archive
          emptyzip.zip valid zip, no images  empty work
          emptydir/                          empty work (dir)
    """
    root = tmp_path_factory.mktemp("manifest_downloads")

    for name in ["r1.png", "r2.png"]:
        _make_png(root / name)

    simple = root / "simple"
    simple.mkdir()
    for name in ["10.png", "1.png", "3.png", "2.png"]:
        _make_png(simple / name)

    for vol, pages in [("vol1", 2), ("vol2", 1)]:
        d = root / "series" / vol
        d.mkdir(parents=True)
        for i in range(1, pages + 1):
            _make_png(d / f"p{i}.png")

    for vol, pages in [("vol1", 2), ("vol2", 1)]:
        d = root / "flat_wrap" / "inner" / vol
        d.mkdir(parents=True)
        for i in range(1, pages + 1):
            _make_png(d / f"p{i}.png")

    for leaf in ["v01", "v03"]:
        d = root / "grp" / "series" / leaf
        d.mkdir(parents=True)
        for i in range(1, 3):
            _make_png(d / f"p{i}.png")
    v02 = root / "grp" / "series" / "v02"
    (v02 / "sub").mkdir(parents=True)
    for i in range(1, 4):
        _make_png(v02 / f"m{i}.png")
    for i in range(1, 3):
        _make_png(v02 / "sub" / f"s{i}.png")
    other = root / "grp" / "other"
    other.mkdir(parents=True)
    _make_png(other / "c1.png")

    for sub, img in [("lead/x1", "a.png"), ("lead/x2", "b.png")]:
        d = root / "grp2" / "run" / sub
        d.mkdir(parents=True)
        _make_png(d / img)
    ch1 = root / "grp2" / "run" / "ch1"
    ch1.mkdir(parents=True)
    _make_png(ch1 / "c1.png")
    _make_png(ch1 / "c2.png")
    ch2 = root / "grp2" / "run" / "ch2"
    ch2.mkdir(parents=True)
    _make_png(ch2 / "d1.png")
    g2 = root / "grp2" / "other"
    g2.mkdir(parents=True)
    _make_png(g2 / "o.png")

    for part in ["【第二部】", "【第十部】", "【第三部】"]:  # unsorted on purpose
        d = root / "kanji" / "s1" / part
        d.mkdir(parents=True)
        _make_png(d / "p1.png")
    s2 = root / "kanji" / "s2"
    s2.mkdir(parents=True)
    _make_png(s2 / "k1.png")

    png = (root / "simple" / "1.png").read_bytes()
    _make_zip(root / "book.cbz", ["p03.png", "p01.png", "p02.png"], png)
    _make_zip(root / "nested.zip",
              ["v02/1.png", "v01/2.png", "v01/1.png"], png)
    arc = root / "arch_run" / "arc"
    arc.mkdir(parents=True)
    _make_zip(arc / "w1.cbz", ["c1.png", "c2.png"], png)
    _make_zip(arc / "w2.cbz", ["d1.png"], png)
    make_rar5_store(arc / "w3.cbr", [("e1.png", png)])
    ao = root / "arch_run" / "other"
    ao.mkdir(parents=True)
    _make_png(ao / "o.png")

    make_pdf(root / "doc.pdf", pages=3)
    (root / "broken.zip").write_bytes(b"this is not a zip at all")
    _make_zip(root / "emptyzip.zip", ["readme.txt"], b"no images")
    (root / "emptydir").mkdir()
    return root


@pytest.fixture
def conn(manifest_tree):
    """HTTPConnection-shaped stub: only ``.app.state`` is consumed."""
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=Settings(downloads_dir=str(manifest_tree)),
                metadata_index_cache=None,
            )
        )
    )


def _resolve(conn, folder, ref, offset=0):
    return drive(catalog.resolve_work(conn, folder, ref, offset))


def _pref(folder, name):
    return {"folder": folder, "name": name}


def _spread(sid, chapter, right, left):
    return {"id": sid, "chapterFolder": chapter, "right": right, "left": left}


# ── shape + exact content ────────────────────────────────────────────────


def test_single_leaf_chapter_exact(conn):
    m = _resolve(conn, "simple", _pref("simple", "2.png"))
    for key in ("chapters", "pageRefs", "spreadTable", "digest"):
        assert key in m
    assert m["chapters"] == [
        {"name": "simple", "folder": "simple",
         "pages": [_pref("simple", n) for n in
                   ("1.png", "2.png", "3.png", "10.png")]}
    ]
    assert m["pageRefs"] == m["chapters"][0]["pages"]
    assert m["spreadTable"] == [
        _spread("c0:s0", "simple", _pref("simple", "1.png"), _pref("simple", "2.png")),
        _spread("c0:s1", "simple", _pref("simple", "3.png"), _pref("simple", "10.png")),
    ]
    assert m["initial"] == {"page": _pref("simple", "2.png"), "spreadId": "c0:s0"}
    assert isinstance(m["digest"], str) and m["digest"].startswith("sha256:")


def test_structural_parent_sibling_concat(conn):
    """depth>=2: chapters = parent's leaf folders in tree order; the mixed
    sibling (v02) is excluded from the run."""
    m = _resolve(conn, "grp/series/v01", _pref("grp/series/v03", "p2.png"))
    assert [c["folder"] for c in m["chapters"]] == [
        "grp/series/v01", "grp/series/v03",
    ]
    assert [c["name"] for c in m["chapters"]] == ["v01", "v03"]
    assert m["pageRefs"] == [
        _pref("grp/series/v01", "p1.png"), _pref("grp/series/v01", "p2.png"),
        _pref("grp/series/v03", "p1.png"), _pref("grp/series/v03", "p2.png"),
    ]
    # Chapters never share a spread — each chapter starts a fresh pairing run.
    assert m["spreadTable"] == [
        _spread("c0:s0", "grp/series/v01",
                _pref("grp/series/v01", "p1.png"), _pref("grp/series/v01", "p2.png")),
        _spread("c1:s0", "grp/series/v03",
                _pref("grp/series/v03", "p1.png"), _pref("grp/series/v03", "p2.png")),
    ]
    assert m["initial"]["spreadId"] == "c1:s0"


def test_flattened_names_concat(conn):
    """flattened wrapper: chapter folders keep the full raw path, chapter
    names keep the node's flattened name verbatim."""
    m = _resolve(conn, "flat_wrap/inner/vol1", _pref("flat_wrap/inner/vol2", "p1.png"))
    assert m["chapters"] == [
        {"name": "inner/vol1", "folder": "flat_wrap/inner/vol1",
         "pages": [_pref("flat_wrap/inner/vol1", "p1.png"),
                  _pref("flat_wrap/inner/vol1", "p2.png")]},
        {"name": "inner/vol2", "folder": "flat_wrap/inner/vol2",
         "pages": [_pref("flat_wrap/inner/vol2", "p1.png")]},
    ]
    assert m["spreadTable"] == [
        _spread("c0:s0", "flat_wrap/inner/vol1",
                _pref("flat_wrap/inner/vol1", "p1.png"),
                _pref("flat_wrap/inner/vol1", "p2.png")),
        _spread("c1:s0", "flat_wrap/inner/vol2",
                _pref("flat_wrap/inner/vol2", "p1.png"), None),
    ]


def test_mixed_folder_reads_direct_images_only(conn):
    """mixed (loose + subfolder) opened directly = own loose images only —
    sibling concat does NOT engage and sub/ never joins."""
    m = _resolve(conn, "grp/series/v02", _pref("grp/series/v02", "m1.png"))
    assert m["chapters"] == [
        {"name": "v02", "folder": "grp/series/v02",
         "pages": [_pref("grp/series/v02", n) for n in ("m1.png", "m2.png", "m3.png")]}
    ]
    assert all("sub/" not in p["name"] for p in m["pageRefs"])


def test_nonleaf_opened_folder_joins_sibling_run(conn):
    """A folder-children-only node at depth>=2 concatenates its leaf SIBLINGS
    (viewer parity: cross-chapter engages whenever the opened folder is not
    mixed and the parent's leaf set is >1)."""
    m = _resolve(conn, "grp2/run/lead", _pref("grp2/run/ch2", "d1.png"))
    assert [c["folder"] for c in m["chapters"]] == [
        "grp2/run/ch1", "grp2/run/ch2",
    ]
    assert m["pageRefs"] == [
        _pref("grp2/run/ch1", "c1.png"), _pref("grp2/run/ch1", "c2.png"),
        _pref("grp2/run/ch2", "d1.png"),
    ]


def test_depth1_parent_no_concat(conn):
    """series/vol1 has a leaf sibling but sits at raw depth 1 — single chapter."""
    m = _resolve(conn, "series/vol1", _pref("series/vol1", "p1.png"))
    assert [c["folder"] for c in m["chapters"]] == ["series/vol1"]
    assert len(m["pageRefs"]) == 2


def test_kanji_natural_chapter_order(conn):
    m = _resolve(conn, "kanji/s1/【第三部】", _pref("kanji/s1/【第三部】", "p1.png"))
    assert [c["folder"] for c in m["chapters"]] == [
        "kanji/s1/【第二部】", "kanji/s1/【第三部】", "kanji/s1/【第十部】",
    ]


def test_archive_flat_leaf(conn):
    m = _resolve(conn, "book.cbz", _pref("book.cbz", "p01.png"))
    assert m["chapters"] == [
        {"name": "book.cbz", "folder": "book.cbz",
         "pages": [_pref("book.cbz", n) for n in ("p01.png", "p02.png", "p03.png")]}
    ]


def test_archive_inner_dir_leaf(conn):
    m = _resolve(conn, "nested.zip/v01", _pref("nested.zip/v01", "1.png"))
    assert m["chapters"] == [
        {"name": "v01", "folder": "nested.zip/v01",
         "pages": [_pref("nested.zip/v01", "1.png"), _pref("nested.zip/v01", "2.png")]}
    ]


def test_archive_root_flat_listing(conn):
    """Opening the archive itself (inner dirs only → non-leaf, non-mixed)
    enumerates ALL entries flat, inner names verbatim — /api/images parity."""
    m = _resolve(conn, "nested.zip", _pref("nested.zip", "v02/1.png"))
    assert m["chapters"] == [
        {"name": "nested.zip", "folder": "nested.zip",
         "pages": [_pref("nested.zip", "v01/1.png"),
                  _pref("nested.zip", "v01/2.png"),
                  _pref("nested.zip", "v02/1.png")]}
    ]


def test_archive_chapter_concat(conn):
    """Archive leaf folders inside a depth>=2 parent concatenate like dirs."""
    m = _resolve(conn, "arch_run/arc/w1.cbz", _pref("arch_run/arc/w3.cbr", "e1.png"))
    assert [c["folder"] for c in m["chapters"]] == [
        "arch_run/arc/w1.cbz", "arch_run/arc/w2.cbz", "arch_run/arc/w3.cbr",
    ]
    assert m["pageRefs"] == [
        _pref("arch_run/arc/w1.cbz", "c1.png"), _pref("arch_run/arc/w1.cbz", "c2.png"),
        _pref("arch_run/arc/w2.cbz", "d1.png"),
        _pref("arch_run/arc/w3.cbr", "e1.png"),
    ]
    assert m["initial"] == {"page": _pref("arch_run/arc/w3.cbr", "e1.png"),
                            "spreadId": "c2:s0"}


def test_pdf_work(conn):
    m = _resolve(conn, "doc.pdf", _pref("doc.pdf", "p002.png"))
    assert m["chapters"] == [
        {"name": "doc.pdf", "folder": "doc.pdf",
         "pages": [_pref("doc.pdf", n) for n in ("p001.png", "p002.png", "p003.png")]}
    ]


def test_root_loose_images(conn):
    m = _resolve(conn, "", _pref("", "r2.png"))
    assert m["chapters"] == [
        {"name": "", "folder": "",
         "pages": [_pref("", "r1.png"), _pref("", "r2.png")]}
    ]
    assert m["initial"]["spreadId"] == "c0:s0"


def test_offset_one_blanks(conn):
    m = _resolve(conn, "simple", _pref("simple", "1.png"), offset=1)
    assert m["offset"] == 1
    assert m["spreadTable"] == [
        _spread("c0:s0", "simple", _pref("simple", "1.png"), None),
        _spread("c0:s1", "simple", _pref("simple", "2.png"),
                _pref("simple", "3.png")),
        _spread("c0:s2", "simple", _pref("simple", "10.png"), None),
    ]


# ── digest ────────────────────────────────────────────────────────────────


def test_digest_deterministic_same_work(conn):
    a = _resolve(conn, "grp/series/v01", _pref("grp/series/v01", "p1.png"))
    b = _resolve(conn, "grp/series/v01", _pref("grp/series/v03", "p1.png"))
    # Same work + offset → same digest regardless of the anchored page
    # (``initial`` is per-call metadata, not content).
    assert a["digest"] == b["digest"]
    assert a["pageRefs"] == b["pageRefs"]
    assert a["initial"]["page"] != b["initial"]["page"]


def test_digest_differs_across_works_and_offsets(conn):
    base = _resolve(conn, "series/vol1", _pref("series/vol1", "p1.png"))
    off = _resolve(conn, "series/vol1", _pref("series/vol1", "p1.png"), offset=1)
    other = _resolve(conn, "simple", _pref("simple", "1.png"))
    assert base["digest"] != off["digest"]
    assert base["digest"] != other["digest"]


def test_digest_path_independent(conn, tmp_path_factory, manifest_tree):
    """Two physical roots with identical content → identical digest (no
    absolute-path leakage into the canonical form)."""
    import shutil

    clone = tmp_path_factory.mktemp("manifest_clone")
    shutil.copytree(manifest_tree, clone, dirs_exist_ok=True)
    conn2 = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=Settings(downloads_dir=str(clone)),
                metadata_index_cache=None,
            )
        )
    )
    a = _resolve(conn, "grp/series/v01", _pref("grp/series/v01", "p1.png"))
    b = _resolve(conn2, "grp/series/v01", _pref("grp/series/v01", "p1.png"))
    assert a["digest"] == b["digest"]


# ── cache: repeated resolves must not rescan/re-enumerate ────────────────


def test_repeated_resolve_zero_enumeration(conn, monkeypatch):
    """TURN-path: after the cache is warm, N further resolves (leaf, mixed,
    fetch-path archive) perform ZERO scanner/archive enumeration calls."""
    import app.archive as archive_mod
    import app.archive_pdf as pdf_mod
    import app.routes as routes

    calls = {"scan": 0, "archive_routes": 0, "archive_mod": 0,
             "list_images": 0, "pdf_count": 0}

    real_scan = routes.scan_tree
    real_routes_lae = routes.list_archive_entries
    real_mod_lae = archive_mod.list_archive_entries
    real_li = routes.list_images
    real_pdf = pdf_mod.pdf_page_count

    def count_scan(*a, **k):
        calls["scan"] += 1
        return real_scan(*a, **k)

    def count_routes_lae(*a, **k):
        calls["archive_routes"] += 1
        return real_routes_lae(*a, **k)

    def count_mod_lae(*a, **k):
        calls["archive_mod"] += 1
        return real_mod_lae(*a, **k)

    def count_li(*a, **k):
        calls["list_images"] += 1
        return real_li(*a, **k)

    def count_pdf(*a, **k):
        calls["pdf_count"] += 1
        return real_pdf(*a, **k)

    monkeypatch.setattr(routes, "scan_tree", count_scan)
    monkeypatch.setattr(routes, "list_archive_entries", count_routes_lae)
    monkeypatch.setattr(archive_mod, "list_archive_entries", count_mod_lae)
    monkeypatch.setattr(routes, "list_images", count_li)
    monkeypatch.setattr(pdf_mod, "pdf_page_count", count_pdf)

    # Warm the metadata-index cache AND the manifest cache for every folder
    # the loop below resolves — the fetch-path work (nested.zip) enumerates
    # exactly once, on this warm-up.
    _resolve(conn, "simple", _pref("simple", "1.png"))
    _resolve(conn, "grp/series/v02", _pref("grp/series/v02", "m1.png"))
    _resolve(conn, "nested.zip", _pref("nested.zip", "v01/1.png"))
    for k in calls:
        calls[k] = 0

    for _ in range(3):
        _resolve(conn, "simple", _pref("simple", "1.png"))
        _resolve(conn, "grp/series/v02", _pref("grp/series/v02", "m1.png"))
        # Fetch-path work (archive root): manifest cache must also absorb it.
        _resolve(conn, "nested.zip", _pref("nested.zip", "v01/1.png"))
    assert calls == {"scan": 0, "archive_routes": 0, "archive_mod": 0,
                     "list_images": 0, "pdf_count": 0}


# ── error kinds (distinct, typed — never a silent fallback) ──────────────


def test_missing_initial_page_ref(conn):
    with pytest.raises(catalog.ManifestError) as e:
        _resolve(conn, "simple", None)
    assert e.value.code == "ERR_NO_INITIAL_PAGE"


def test_malformed_inputs(conn):
    with pytest.raises(catalog.ManifestError) as e:
        _resolve(conn, "simple", {"folder": "simple"})  # name missing
    assert e.value.code == "ERR_MALFORMED"
    with pytest.raises(catalog.ManifestError) as e:
        _resolve(conn, 123, _pref("simple", "1.png"))
    assert e.value.code == "ERR_MALFORMED"
    with pytest.raises(catalog.ManifestError) as e:
        _resolve(conn, "simple", _pref("simple", "1.png"), offset=2)
    assert e.value.code == "ERR_RANGE"


def test_initial_ref_not_in_work_is_error_not_fallback(conn):
    """暗黙の別ページ fallback 禁止: an unknown initialPageRef is an error;
    the returned page is always the input page."""
    with pytest.raises(catalog.ManifestError) as e:
        _resolve(conn, "simple", _pref("simple", "nope.png"))
    assert e.value.code == "ERR_PAGE_NOT_FOUND"
    with pytest.raises(catalog.ManifestError) as e:
        _resolve(conn, "simple", _pref("other", "p1.png"))
    assert e.value.code == "ERR_PAGE_NOT_FOUND"
    ok = _resolve(conn, "simple", _pref("simple", "3.png"))
    assert ok["initial"]["page"] == _pref("simple", "3.png")


def test_out_of_path_forbidden(conn):
    for bad in ("../outside", "grp/../../etc", ".."):
        with pytest.raises(catalog.ManifestError) as e:
            _resolve(conn, bad, _pref(bad, "x.png"))
        assert e.value.code == "ERR_FORBIDDEN", bad


def test_folder_not_found(conn):
    with pytest.raises(catalog.ManifestError) as e:
        _resolve(conn, "nosuchdir", _pref("nosuchdir", "x.png"))
    assert e.value.code == "ERR_NOT_FOUND"
    # Archive-inner path that does not exist inside a VALID archive → 404-ish.
    with pytest.raises(catalog.ManifestError) as e:
        _resolve(conn, "nested.zip/nothere", _pref("nested.zip/nothere", "x.png"))
    assert e.value.code == "ERR_NOT_FOUND"


def test_empty_work(conn):
    for empty in ("emptydir", "emptyzip.zip", "grp/series"):  # dirs/sub-only too
        with pytest.raises(catalog.ManifestError) as e:
            _resolve(conn, empty, _pref(empty, "x.png"))
        assert e.value.code == "ERR_EMPTY_WORK", empty


def test_broken_archive_distinct(conn):
    with pytest.raises(catalog.ManifestError) as e:
        _resolve(conn, "broken.zip", _pref("broken.zip", "x.png"))
    assert e.value.code == "ERR_BROKEN_ARCHIVE"
    # broken inner path also reports broken, not not-found
    with pytest.raises(catalog.ManifestError) as e:
        _resolve(conn, "broken.zip/inner", _pref("broken.zip/inner", "x.png"))
    assert e.value.code == "ERR_BROKEN_ARCHIVE"


# ── chunking ──────────────────────────────────────────────────────────────


def test_chunk_roundtrip_same_digest(conn):
    m = _resolve(conn, "grp/series/v01", _pref("grp/series/v01", "p1.png"))
    chunks = catalog.split_manifest(m, max_bytes=97)  # force many chunks
    assert len(chunks) > 3
    for i, ch in enumerate(chunks):
        assert ch["v"] == protocol.PROTOCOL_VERSION
        assert ch["kind"] == "manifest-chunk"
        assert ch["index"] == i and ch["count"] == len(chunks)
        assert ch["digest"] == m["digest"]
        assert isinstance(ch["data"], str)
        # every chunk fits a normal frame
        assert len(protocol.encode_frame(ch)) <= protocol.MAX_FRAME_BYTES
    again = catalog.join_manifest(chunks)
    assert again["digest"] == m["digest"]
    assert {k: again[k] for k in
            ("chapters", "pageRefs", "spreadTable")} == {
        k: m[k] for k in ("chapters", "pageRefs", "spreadTable")}


def test_chunk_order_independent(conn):
    m = _resolve(conn, "arch_run/arc/w1.cbz", _pref("arch_run/arc/w1.cbz", "c1.png"))
    chunks = catalog.split_manifest(m, max_bytes=64)
    assert catalog.join_manifest(list(reversed(chunks)))["digest"] == m["digest"]


def test_manifest_has_no_image_bytes(conn):
    m = _resolve(conn, "nested.zip", _pref("nested.zip", "v01/1.png"))
    for ch in m["chapters"]:
        for p in ch["pages"]:
            assert set(p.keys()) == {"folder", "name"}
            assert isinstance(p["folder"], str) and isinstance(p["name"], str)
    # canonical form is pure ASCII JSON text — no binary anywhere
    raw = catalog.encode_manifest(m)
    assert all(b < 128 for b in raw)


def test_join_rejects_tampered_chunks(conn):
    m = _resolve(conn, "simple", _pref("simple", "1.png"))
    chunks = catalog.split_manifest(m, max_bytes=64)
    with pytest.raises(catalog.ManifestError):
        catalog.join_manifest(chunks[:-1])  # missing tail
    bad = [dict(c) for c in chunks]
    bad[0]["digest"] = "sha256:" + "0" * 64
    with pytest.raises(catalog.ManifestError):
        catalog.join_manifest(bad)  # digest mismatch between chunks
    bad2 = [dict(c) for c in chunks]
    bad2[0]["data"] = bad2[0]["data"].replace('"', "x", 1)
    with pytest.raises(catalog.ManifestError):
        catalog.join_manifest(bad2)  # corrupted payload → digest mismatch


def test_split_oversize_manifest(conn, monkeypatch):
    m = _resolve(conn, "simple", _pref("simple", "1.png"))
    monkeypatch.setattr(catalog, "MAX_MANIFEST_BYTES", 100)
    with pytest.raises(catalog.ManifestError) as e:
        catalog.split_manifest(m)
    assert e.value.code == "ERR_SIZE"


def test_split_chunk_over_frame_cap():
    """A max_bytes large enough to blow the per-frame cap is a typed error,
    not an over-limit frame on the wire."""
    big = {"v": protocol.PROTOCOL_VERSION, "folder": "big", "offset": 0,
           "chapters": [{"name": "big", "folder": "big", "pages": []}],
           "pageRefs": [_pref("big", f"p{i:05d}.png") for i in range(4000)],
           "spreadTable": []}
    big["digest"] = catalog.manifest_digest(big)
    with pytest.raises(catalog.ManifestError) as e:
        catalog.split_manifest(big, max_bytes=protocol.MAX_FRAME_BYTES)
    assert e.value.code == "ERR_SIZE"
    # …while the default budget stays under the cap and round-trips.
    chunks = catalog.split_manifest(big)
    assert catalog.join_manifest(chunks)["digest"] == big["digest"]
