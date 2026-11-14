"""Python-internal nested-archive behavior — tree shape / listing units (#483).

Quarantined under ``BOOKQ_TEST_SERVER=rs`` (imports ``app.*``): these pin
the Python implementation that retires with ``app/`` in Phase 3. The HTTP
contract lives in ``tests/test_archive_nested.py`` and runs in both lanes.

Run:
    BOOKQ_TEST_MODE=true DOWNLOADS_DIR=<tmp> uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_archive_nested_internals.py -q
"""

import os
import struct
import sys
import zipfile
import zlib
from pathlib import Path
from urllib.parse import quote

import pytest

os.environ["BOOKQ_TEST_MODE"] = "true"

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from app.archive import build_archive_tree, list_archive_entries  # noqa: E402
from app.routes import _find_tree_node  # noqa: E402
from app.scanner import scan_tree  # noqa: E402
from tests.fixtures.rar5 import make_rar5_store  # noqa: E402
from tests.helpers import cold_index_lifespan, get_folders_ready  # noqa: E402

ARCHIVES = ["nested.zip", "nested.cbr"]


def _make_png(path: Path) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 40, 60, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes((60, 200, 60)) * 40
    idat = zlib.compress(row * 60)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
    )


def _make_epub(path: Path, pages: list[tuple[str, str]], png: bytes) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?>'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        items: list[str] = []
        refs: list[str] = []
        for i, (xhtml, img) in enumerate(pages):
            items.append(f'<item id="pg{i}" href="Text/{xhtml}" media-type="application/xhtml+xml"/>')
            items.append(f'<item id="im{i}" href="Images/{quote(img)}" media-type="image/png"/>')
            refs.append(f'<itemref idref="pg{i}"/>')
            src = "../Images/" + quote(img)
            body = f'<img src="{src}" alt=""/>'
            zf.writestr(f"OEBPS/Text/{xhtml}", f"<html><body>{body}</body></html>")
            zf.writestr(f"OEBPS/Images/{img}", png)
        opf = (
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            "<metadata/>" + f"<manifest>{''.join(items)}</manifest>"
            + f"<spine>{''.join(refs)}</spine></package>"
        )
        zf.writestr("OEBPS/content.opf", opf)


# Logical nested content shared by the ZIP and the RAR5-store fixture.
NESTED_ENTRIES = [
    "cover.jpg",
    "vol01/01.jpg",
    "vol01/02.jpg",
    "vol01/sub/03.jpg",
    "vol02/01.jpg",
]
JUNK_ENTRIES = [
    "vol01/__MACOSX/x.jpg",
    "vol01/.dot.jpg",
    "../evil.png",
    "/abs.png",
    "vol01/sub/../../evil2.png",
    "readme.txt",
]


@pytest.fixture
def nested_tree(tmp_path: Path) -> Path:
    _make_png(tmp_path / "src.png")
    png = (tmp_path / "src.png").read_bytes()
    payloads = [(name, png) for name in NESTED_ENTRIES + JUNK_ENTRIES]
    with zipfile.ZipFile(tmp_path / "nested.zip", "w") as zf:
        for name, data in payloads:
            zf.writestr(name, data)
    make_rar5_store(tmp_path / "nested.cbr", payloads)
    # Non-ASCII nested inner, one archive per format.
    jp_payloads = [("vol01/\u7b2c01\u8a71/01.jpg", png), ("vol01/01.jpg", png)]
    with zipfile.ZipFile(tmp_path / "jp.zip", "w") as zf:
        for name, data in jp_payloads:
            zf.writestr(name, data)
    make_rar5_store(tmp_path / "jp.cbr", jp_payloads)
    # EPUB with spine order deliberately disagreeing with filename order.
    _make_epub(
        tmp_path / "manga.epub",
        [("c.xhtml", "z_cover.png"), ("b.xhtml", "a_p1.png"), ("a.xhtml", "m_p2.png")],
        png,
    )
    (tmp_path / "broken.rar").write_bytes(b"Rar!\x1a\x07\x01\x00garbage")
    return tmp_path


@pytest.fixture
def client(nested_tree: Path, monkeypatch):
    monkeypatch.setenv("DOWNLOADS_DIR", str(nested_tree))
    from app.main import create_app

    from fastapi.testclient import TestClient

    app = create_app()
    app.router.lifespan_context = cold_index_lifespan
    with TestClient(app) as client:
        yield client


def _node(tree: list[dict], name: str) -> dict:
    return next(n for n in tree if n["name"] == name)


# ── scan_tree shape ───────────────────────────────────────────────────────


@pytest.mark.parametrize("archive", ARCHIVES)
def test_scan_tree_recursive_shape(nested_tree: Path, archive: str):
    node = _node(scan_tree(nested_tree), archive)
    assert node["type"] == "folder"
    assert node["archive"] is True
    # Root loose-first: cover leads, then sub-folders.
    assert [c["name"] for c in node["children"]] == ["cover.jpg", "vol01", "vol02"]
    vol01 = _node(node["children"], "vol01")
    assert [c["name"] for c in vol01["children"]] == ["01.jpg", "02.jpg", "sub"]
    sub = _node(vol01["children"], "sub")
    assert [c["name"] for c in sub["children"]] == ["03.jpg"]
    vol02 = _node(node["children"], "vol02")
    assert [c["name"] for c in vol02["children"]] == ["01.jpg"]


@pytest.mark.parametrize("archive", ARCHIVES)
def test_nested_thumbs_and_no_inner_archive_flag(nested_tree: Path, archive: str):
    node = _node(scan_tree(nested_tree), archive)
    assert node["thumb"] == "cover.jpg"
    assert node["thumbs"] == ["vol01/01.jpg", "vol02/01.jpg"]
    vol01 = _node(node["children"], "vol01")
    assert vol01["thumb"] == "01.jpg"
    assert "archive" not in vol01
    sub = _node(vol01["children"], "sub")
    assert sub["thumb"] == "03.jpg"
    assert "archive" not in sub
    vol02 = _node(node["children"], "vol02")
    assert vol02["thumb"] == "01.jpg"
    assert "archive" not in vol02


@pytest.mark.parametrize("archive", ARCHIVES)
def test_junk_nesting_never_surfaces(nested_tree: Path, archive: str):
    entries = list_archive_entries(nested_tree / archive)
    assert entries == [
        "cover.jpg",
        "vol01/01.jpg",
        "vol01/02.jpg",
        "vol01/sub/03.jpg",
        "vol02/01.jpg",
    ]
    assert all("__MACOSX" not in e and "evil" not in e and "abs.png" not in e for e in entries)
    assert all(not e.split("/")[-1].startswith(".") for e in entries)


# ── non-ASCII / EPUB / cap / deep-nesting units ───────────────────────────


@pytest.mark.parametrize("archive", ["jp.zip", "jp.cbr"])
def test_non_ascii_nested_inner_unit(nested_tree: Path, archive: str):
    inner = "vol01/\u7b2c01\u8a71/01.jpg"
    assert inner in list_archive_entries(nested_tree / archive)
    node = _node(scan_tree(nested_tree), archive)
    vol01 = _node(node["children"], "vol01")
    assert "\u7b2c01\u8a71" in [c["name"] for c in vol01["children"]]


def test_epub_stays_flat_spine_order(nested_tree: Path):
    node = _node(scan_tree(nested_tree), "manga.epub")
    assert node["archive"] is True
    assert all(c["type"] == "image" for c in node["children"])
    assert [c["name"] for c in node["children"]] == [
        "OEBPS/Images/z_cover.png",
        "OEBPS/Images/a_p1.png",
        "OEBPS/Images/m_p2.png",
    ]
    assert node["thumb"] == "OEBPS/Images/z_cover.png"


def test_entry_cap_still_truncates_after_recursion(tmp_path: Path, monkeypatch):
    import app.archive as archive_mod

    _make_png(tmp_path / "src.png")
    png = (tmp_path / "src.png").read_bytes()
    names = [f"vol01/{i:02d}.jpg" for i in range(1, 7)] + [
        f"vol02/{i:02d}.jpg" for i in range(1, 7)
    ]
    with zipfile.ZipFile(tmp_path / "big.zip", "w") as zf:
        for name in names:
            zf.writestr(name, png)
    monkeypatch.setattr(archive_mod, "MAX_ARCHIVE_ENTRIES", 5)
    entries = list_archive_entries(tmp_path / "big.zip")
    assert entries == [f"vol01/{i:02d}.jpg" for i in range(1, 6)]
    leaves: list[str] = []

    def _collect(nodes: list[dict], prefix: str = "") -> None:
        for n in nodes:
            if n["type"] == "image":
                leaves.append(prefix + n["name"])
            else:
                _collect(n["children"], prefix + n["name"] + "/")

    _collect(build_archive_tree(entries))
    assert sorted(leaves) == sorted(entries)


def test_unknown_prefix_unit_and_broken_rar_empty(nested_tree: Path):
    node = _node(scan_tree(nested_tree), "broken.rar")
    assert node["children"] == []
    assert node["thumb"] is None


def test_deep_nesting_collapses_to_single_leaf():
    deep = "/".join(f"d{i:02d}" for i in range(40)) + "/leaf.jpg"
    children = build_archive_tree([deep])
    node = children[0]
    for _ in range(32):
        assert node["type"] == "folder"
        node = node["children"][-1]
    assert node["type"] == "image"
    assert node["name"].count("/") >= 1  # remainder stays one servable leaf
    assert node["name"].endswith("leaf.jpg")


# ── API prefix + delivery (Python TestClient; covered by the v1 port) ─────


@pytest.mark.parametrize("archive", ARCHIVES)
def test_folders_addresses_nested_prefix_py(client, archive: str):
    tree = get_folders_ready(client)
    found = _find_tree_node(tree, f"{archive}/vol01")
    assert found is not None and found["type"] == "folder"
    assert found["thumb"] == "01.jpg"
    assert _find_tree_node(tree, f"{archive}/vol01/sub")["thumb"] == "03.jpg"
    # Shallow subtree endpoint resolves the archive prefix too.
    children = get_folders_ready(client, params={"path": f"{archive}/vol01", "depth": 1})
    assert [c["name"] for c in children] == ["sub"]


# ── rar vol2 lock-in (wrong-folder evidence, GREEN characterization) ─────────
# Real-data evidence (.omo/evidence/rar-vol2-diff.txt, cause=wrong-folder):
# v02 holds 197 pages (191 loose + 6 in subfolder) identically in the rar and
# the extracted dir — the backend is CORRECT, so these tests lock the shape in
# with a small 6+2 synthetic mirror. Counts assert structure; the 197 truth is
# pinned by the evidence file, not re-asserted here.

VOL2_LOOSE = [f"top/v02/p{i:02d}.jpg" for i in range(1, 7)]
VOL2_SUB = "top/v02/\u5358\u30da\u30fc\u30b8"
VOL2_SUB_FILES = [f"{VOL2_SUB}/s{i:02d}.jpg" for i in range(1, 3)]
# Sibling keeps every level at 2+ children (or holding loose files) so the
# single-child flatten in scan_tree (app/scanner.py:232-237) never fires here.
VOL2_CASE_NAMES = ["cover.jpg", "top/v01/cover.jpg"] + VOL2_LOOSE + VOL2_SUB_FILES


def _make_vol2_case(tmp_path: Path) -> tuple[Path, Path]:
    """Mirror the real v02 two-level shape as both a rar and a real dir."""
    _make_png(tmp_path / "src.png")
    png = (tmp_path / "src.png").read_bytes()
    case = tmp_path / "case"
    for name in VOL2_CASE_NAMES:
        dest = case / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(png)
    rar = tmp_path / "vol2.cbr"
    make_rar5_store(rar, [(name, png) for name in VOL2_CASE_NAMES])
    return rar, case


def test_rar_vol2_both_levels(tmp_path: Path):
    rar, _case = _make_vol2_case(tmp_path)
    entries = list_archive_entries(rar)
    v02 = [e for e in entries if e.startswith("top/v02/")]
    assert len(v02) == 8  # 6 loose + 2 in sub (real v02: 191 + 6 = 197)
    sub = [e for e in entries if e.startswith(VOL2_SUB + "/")]
    assert sub == [f"{VOL2_SUB}/s01.jpg", f"{VOL2_SUB}/s02.jpg"]
    # Mirrored real dir agrees on both levels.
    node = _node(scan_tree(tmp_path), "case")
    v02node = _node(_node(node["children"], "top")["children"], "v02")
    loose = [c for c in v02node["children"] if c["type"] == "image"]
    assert [c["name"] for c in loose] == [f"p{i:02d}.jpg" for i in range(1, 7)]
    subnode = _node(v02node["children"], "\u5358\u30da\u30fc\u30b8")
    assert [c["name"] for c in subnode["children"]] == ["s01.jpg", "s02.jpg"]
    assert len(loose) + len(subnode["children"]) == len(v02) == 8


def test_rar_tile_parity_scan_tree(tmp_path: Path):
    rar, _case = _make_vol2_case(tmp_path)
    rar_children = build_archive_tree(list_archive_entries(rar))
    dir_node = _node(scan_tree(tmp_path), "case")
    # No single-child flatten diff in this shape: every level holds 2+
    # children or loose files, so scan_tree (app/scanner.py:232-237) never
    # rewrites a name to "parent/child" here. That flatten asymmetry is the
    # only tolerated diff per the plan — none fires, so parity is exact.
    assert [c["name"] for c in rar_children] == [c["name"] for c in dir_node["children"]]
    assert [c["type"] for c in rar_children] == [c["type"] for c in dir_node["children"]]
    assert rar_children[0]["type"] == "image"  # loose-first on both sides
    rar_top = _node(rar_children, "top")
    dir_top = _node(dir_node["children"], "top")
    assert [c["name"] for c in rar_top["children"]] == [c["name"] for c in dir_top["children"]]
    assert rar_top["thumb"] == dir_top["thumb"]
    assert rar_top.get("thumbs") == dir_top.get("thumbs")
    rar_v02 = _node(rar_top["children"], "v02")
    dir_v02 = _node(dir_top["children"], "v02")
    assert [c["name"] for c in rar_v02["children"]] == [c["name"] for c in dir_v02["children"]]
    assert [c["type"] for c in rar_v02["children"]] == [c["type"] for c in dir_v02["children"]]
    assert rar_v02["children"][0]["type"] == "image"  # loose-first inside v02
    assert rar_v02["thumb"] == dir_v02["thumb"] == "p01.jpg"
    assert "thumbs" not in rar_v02 and "thumbs" not in dir_v02  # single subfolder
    rar_sub = _node(rar_v02["children"], "\u5358\u30da\u30fc\u30b8")
    dir_sub = _node(dir_v02["children"], "\u5358\u30da\u30fc\u30b8")
    assert rar_sub["thumb"] == dir_sub["thumb"] == "s01.jpg"
