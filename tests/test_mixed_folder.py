"""Mixed-folder parent-level images-tile spec pins (plan Todo 4).

Parent-level spec: a mixed folder (loose images + subfolder, e.g.
``mgroup/mseries/v02`` = 3 loose + ``sub`` of 2) shows NO tile for its own
loose images in-folder — the in-folder grid holds subfolders only. The loose
images surface ONE level up as a single images-tile
(``data-read-child="1"``, label ``このフォルダを読む``) after the folder
tiles. The viewer reads loose-only (``1 / 3``, m1 first) whether opened via
deep link or via the images-tile click. There is no browse-header read
button (header is parent-nav only).

Fixture mirrors group/deep_series (raw depth 2, so SIBLING_AUTOLOAD_MIN_DEPTH
= 2 engages the chapter flow) with v02 mixed between image-only v01/v03:

    mixed_root/
      mgroup/mseries/v01/  p1.png p2.png          (leaf, image-only)
      mgroup/mseries/v02/  m1..m3.png + sub/s1..s2.png   (MIXED)
      mgroup/mseries/v03/  p1.png p2.png          (leaf, image-only)
      mgroup/other/        c1.png                 (keeps mgroup unflattened)
      solo/                a.png b.png c.png      (root image-only)

Server/page fixtures mirror tests/conftest.py (own uvicorn on a module tree so
the shared session tree stays untouched); selectors mirror
tests/test_url_state.py (#counter, #folder-name, .tile[data-path]) and
tests/test_ui.py (#img-track .page-cell img[src*=...]).

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_mixed_folder.py -q
"""

import os
import socket
import struct
import subprocess
import sys
import time
import json
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.fixtures.rar5 import make_rar5_store

REPO_ROOT = Path(__file__).resolve().parent.parent

MIXED = "mgroup/mseries/v02"
MIXED_PARENT = "mgroup/mseries"
# Parent-level spec: the viewer reads the mixed folder's LOOSE images only
# (its direct image children, m1..m3). sub/ pages live in their own leaf
# chapter and never join this one.
MIXED_LOOSE = 3

AMIXED = "mixed.cbr/amseries/v02"
AMIXED_PARENT = "mixed.cbr/amseries"
# Same mixed shape inside a RAR5-store archive. The archive backend lists ALL
# descendants recursively, so /api/images for the mixed prefix returns 5 —
# but the viewer chapter is tree-driven (direct image children only) and
# reads the 3 loose pages, each exactly once.
AMIXED_LOOSE = 3

READ_LABEL = "このフォルダを読む"


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


@pytest.fixture(scope="module")
def mixed_tree(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("mixed_downloads")
    for leaf, pages in [("v01", 2), ("v03", 2)]:
        d = root / "mgroup" / "mseries" / leaf
        d.mkdir(parents=True)
        for i in range(1, pages + 1):
            _make_png(d / f"p{i}.png")
    v02 = root / "mgroup" / "mseries" / "v02"
    (v02 / "sub").mkdir(parents=True)
    for i in range(1, 4):
        _make_png(v02 / f"m{i}.png")
    for i in range(1, 3):
        _make_png(v02 / "sub" / f"s{i}.png")
    other = root / "mgroup" / "other"
    other.mkdir(parents=True)
    _make_png(other / "c1.png")
    solo = root / "solo"
    solo.mkdir()
    for name in ["a.png", "b.png", "c.png"]:
        _make_png(solo / name)
    # Archive mirror of the mixed shape (real rar-v02 case): amseries/v02 is
    # mixed (3 loose + sub/ of 2) between image-only v01/v03. Placed at root
    # so neither the fs tree nor this archive flattens (2+ children each).
    png_sample = (solo / "a.png").read_bytes()
    apayloads = (
        [(f"amseries/v01/p{i}.png", png_sample) for i in range(1, 3)]
        + [(f"amseries/v02/m{i}.png", png_sample) for i in range(1, 4)]
        + [(f"amseries/v02/sub/s{i}.png", png_sample) for i in range(1, 3)]
        + [(f"amseries/v03/p{i}.png", png_sample) for i in range(1, 3)]
        + [("aother/c1.png", png_sample)]
    )
    make_rar5_store(root / "mixed.cbr", apayloads)
    return root


@pytest.fixture(scope="module")
def mixed_server(mixed_tree: Path) -> str:
    """Live FastAPI server in test mode against mixed_tree (conftest.py idiom)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    env = os.environ.copy()
    env["BOOKQ_TEST_MODE"] = "true"
    env["DOWNLOADS_DIR"] = str(mixed_tree)

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("mixed server exited early")
        try:
            with urllib.request.urlopen(base_url + "/api/health", timeout=1):
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.terminate()
        raise RuntimeError("mixed server did not become healthy within 10s")

    yield base_url

    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def mixed_page(browser, mixed_server):
    """Portrait viewport (single-page mode) on the mixed tree."""
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    page.goto(mixed_server)
    page.wait_for_selector(".tile")  # folder tree fetched and rendered
    yield page
    context.close()


# ── Parent-level images-tile spec ───────────────────────────────────────


def _grid_tiles(page):
    """Split the current browse grid into (folder_paths, read_child_paths, self_paths).

    Folder tiles carry no read marker; images-tiles carry data-read-child and
    the root self-tile carries data-read-self. Order-sensitive: folders render
    first, images-tiles after.
    """
    folders, reads, selfs = [], [], []
    for t in page.locator(".tile").all():
        if t.get_attribute("data-read-child") == "1":
            reads.append(t.get_attribute("data-path"))
        elif t.get_attribute("data-read-self") == "1":
            selfs.append(t.get_attribute("data-path"))
        else:
            folders.append(t.get_attribute("data-path"))
    return folders, reads, selfs


def test_mixed_folder_deep_link_reads_loose_only(mixed_page, mixed_server):
    # Deep link into the mixed folder opens its OWN loose images as a single
    # chapter: 3 loose (m1..m3), m1 first. sub/ pages are not part of it.
    mixed_page.goto(mixed_server + f"/#/view/{MIXED}?p=0")
    expect(mixed_page.locator("#counter")).to_have_text(f"1 / {MIXED_LOOSE}")
    expect(
        mixed_page.locator(f'#img-track .page-cell img[src*="{MIXED}/m1.png"]')
    ).to_be_visible()
    expect(
        mixed_page.locator(f'#img-track .page-cell img[src*="{MIXED}/sub/"]')
    ).to_have_count(0)


def test_no_header_read_button(mixed_page, mixed_server):
    # The browse header is parent-nav only: no read entry point lives there.
    # The loose images are reached one level up via the images-tile instead.
    mixed_page.goto(mixed_server + f"/#/browse/{MIXED}")
    expect(mixed_page.locator("#browse-header")).to_have_text("↩ v02")
    assert mixed_page.get_by_role("button", name=READ_LABEL).count() == 0


def test_in_folder_shows_subfolders_only(mixed_page, mixed_server):
    # In-folder grid holds subfolders only — no tile for the loose images.
    mixed_page.goto(mixed_server + f"/#/browse/{MIXED}")
    folders, reads, selfs = _grid_tiles(mixed_page)
    assert folders == [f"{MIXED}/sub"]
    assert reads == []
    assert selfs == []


def test_parent_shows_images_tile_plus_folder(mixed_page, mixed_server):
    # Parent level carries the mixed child's loose images as ONE images-tile
    # (data-read-child, このフォルダを読む) after the folder tiles.
    mixed_page.goto(mixed_server + f"/#/browse/{MIXED_PARENT}")
    expect(mixed_page.locator("#browse-header")).to_have_text("↩ mseries")
    folders, reads, selfs = _grid_tiles(mixed_page)
    assert folders == [
        f"{MIXED_PARENT}/v01",
        MIXED,
        f"{MIXED_PARENT}/v03",
    ]
    assert reads == [MIXED]
    assert selfs == []
    tile = mixed_page.locator('.tile[data-read-child="1"]')
    expect(tile).to_have_count(1)
    expect(tile).to_have_attribute("data-path", MIXED)
    expect(tile).to_contain_text(READ_LABEL)


def test_images_tile_click_reads_loose(mixed_page, mixed_server):
    # Images-tile click and deep-link agree: loose-only chapter, m1 first.
    mixed_page.goto(mixed_server + f"/#/browse/{MIXED_PARENT}")
    mixed_page.locator('.tile[data-read-child="1"]').click()
    expect(mixed_page.locator("#counter")).to_have_text(f"1 / {MIXED_LOOSE}")
    expect(
        mixed_page.locator(f'#img-track .page-cell img[src*="{MIXED}/m1.png"]')
    ).to_be_visible()


def test_mixed_folder_archive_no_duplicates(mixed_page, mixed_server):
    # Archive mirror of the mixed shape reads loose-only too: the recursive
    # backend listing (5 under the prefix) must not leak sub/ pages into the
    # tree-driven chapter — each loose page exactly once.
    mixed_page.goto(mixed_server + f"/#/view/{AMIXED}?p=0")
    expect(mixed_page.locator("#counter")).to_have_text(f"1 / {AMIXED_LOOSE}")
    for src in ["m1.png", "m2.png", "m3.png"]:
        expect(
            mixed_page.locator(f'#img-track .page-cell img[src*="{AMIXED}/{src}"]')
        ).to_have_count(1)
    expect(
        mixed_page.locator(f'#img-track .page-cell img[src*="{AMIXED}/sub/"]')
    ).to_have_count(0)


def test_archive_parent_shows_images_tile(mixed_page, mixed_server):
    # Archive parent level mirrors the fs tree: folder tiles plus ONE
    # images-tile for the mixed child, and its click reads loose-only.
    mixed_page.goto(mixed_server + f"/#/browse/{AMIXED_PARENT}")
    folders, reads, selfs = _grid_tiles(mixed_page)
    assert folders == [
        f"{AMIXED_PARENT}/v01",
        AMIXED,
        f"{AMIXED_PARENT}/v03",
    ]
    assert reads == [AMIXED]
    assert selfs == []
    mixed_page.locator('.tile[data-read-child="1"]').click()
    expect(mixed_page.locator("#counter")).to_have_text(f"1 / {AMIXED_LOOSE}")
    expect(
        mixed_page.locator(f'#img-track .page-cell img[src*="{AMIXED}/m1.png"]')
    ).to_be_visible()


# ── Unchanged-behavior pins (leaf / image-only flow untouched) ─────────────


def test_leaf_deep_link_unchanged(mixed_page, mixed_server):
    # Sibling leaf chapter flow is untouched: v01 deep link lands in v01 with
    # its chapter-local counter.
    mixed_page.goto(mixed_server + "/#/view/mgroup/mseries/v01?p=0")
    expect(mixed_page.locator("#counter")).to_have_text("1 / 2")
    expect(mixed_page.locator("#folder-name")).to_contain_text("v01")


def test_image_only_unchanged(mixed_page):
    # Image-only folders still open directly from tile click (hasOnly path).
    mixed_page.locator('.tile[data-path="solo"]').click()
    expect(mixed_page.locator("#counter")).to_have_text("1 / 3")


# ── Reachability matrix ───────────────────────────────────────────────────
#
# Synthetic depth-3 mixed tree: loose present/absent per level, an empty
# folder, and an imageless single-child flatten chain.
#
#     reach_root/
#       r1.png r2.png              (ROOT LOOSE x2 -> root self-tile)
#       lvl1/  l1.png + lvl2/ + side/     (MIXED, loose x1)
#         lvl2/  l2a.png l2b.png + lvl3/  (MIXED, loose x2)
#           lvl3/  l3.png                 (leaf)
#         side/  s1.png                   (leaf; keeps lvl1 unflattened)
#       plain/  p1.png p2.png             (leaf, image-only)
#       noloose/  n1/x.png + n2/y.png     (no loose: subfolders only)
#       empty/                            (no children at all)
#       chain/only/leaf/  c1.png c2.png   (imageless chain -> flattened tile)
#
# All counts below derive from REACH_LOOSE / REACH_LEAVES; nothing else is
# hardcoded.

# Per-level loose images: viewer chapters reachable via that level's
# images-tile (root loose via the root self-tile).
REACH_LOOSE = {
    "": ["r1.png", "r2.png"],
    "lvl1": ["l1.png"],
    "lvl1/lvl2": ["l2a.png", "l2b.png"],
}
# Image-only leaves: opened directly, never behind an images-tile.
REACH_LEAVES = {
    "lvl1/lvl2/lvl3": ["l3.png"],
    "lvl1/side": ["s1.png"],
    "plain": ["p1.png", "p2.png"],
    "noloose/n1": ["x.png"],
    "noloose/n2": ["y.png"],
    "chain": ["c1.png", "c2.png"],
}
# In-folder browse expectation per level: subfolder tiles only. Sorted —
# scan order is filesystem-dependent, set equality is what matters.
REACH_SUBS = {
    "": ["lvl1", "plain", "noloose", "empty", "chain"],
    "lvl1": ["lvl1/lvl2", "lvl1/side"],
    "lvl1/lvl2": ["lvl1/lvl2/lvl3"],
    "noloose": ["noloose/n1", "noloose/n2"],
    "empty": [],
}
# Images-tiles expected per parent level: one per MIXED child (loose>0).
REACH_READ_CHILD = {
    "": ["lvl1"],
    "lvl1": ["lvl1/lvl2"],
    "lvl1/lvl2": [],
    "noloose": [],
}


@pytest.fixture(scope="module")
def reach_tree(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("reach_downloads")
    for name in REACH_LOOSE[""]:
        _make_png(root / name)
    for folder, names in {**REACH_LOOSE, **REACH_LEAVES}.items():
        if not folder:
            continue
        d = root / folder
        d.mkdir(parents=True, exist_ok=True)
        for name in names:
            if not (d / name).exists():
                _make_png(d / name)
    (root / "empty").mkdir(exist_ok=True)
    return root


@pytest.fixture(scope="module")
def reach_server(reach_tree: Path) -> str:
    """Live FastAPI server in test mode against reach_tree (conftest idiom)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    env = os.environ.copy()
    env["BOOKQ_TEST_MODE"] = "true"
    env["DOWNLOADS_DIR"] = str(reach_tree)

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("reach server exited early")
        try:
            with urllib.request.urlopen(base_url + "/api/health", timeout=1):
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.terminate()
        raise RuntimeError("reach server did not become healthy within 10s")

    yield base_url

    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def reach_page(browser, reach_server):
    """Portrait viewport (single-page mode) on the reachability tree."""
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    page.goto(reach_server)
    page.wait_for_selector(".tile")  # folder tree fetched and rendered
    yield page
    context.close()


def _browse_to(page, base, folder):
    if not folder:
        page.goto(base)
    else:
        page.goto(base + "/#/browse/" + folder)
    page.wait_for_selector("#tile-grid")


def test_reachability_matrix(reach_page, reach_server):
    base = reach_server

    # (a) every in-folder grid == subfolders only; (b) parent shows that
    # level's images-tile iff loose>0 (root adds the self-tile exception).
    for folder, subs in REACH_SUBS.items():
        _browse_to(reach_page, base, folder)
        folders, reads, selfs = _grid_tiles(reach_page)
        assert sorted(folders) == sorted(subs), f"in-folder grid for {folder!r}"
        assert sorted(reads) == sorted(REACH_READ_CHILD.get(folder, [])), (
            f"images-tiles visible from {folder!r}"
        )
        assert selfs == ([""] if folder == "" else []), (
            f"root self-tile shown at {folder!r}"
        )

    # Folder counts include the tiles (root: 5 folders + 1 images-tile + 1 self).
    _browse_to(reach_page, base, "")
    expect(reach_page.locator("#folder-count")).to_have_text("7 件")

    # (c) viewer reads exactly that level's loose via deep-link AND tile click.
    reach_page.goto(base + "/#/view/lvl1?p=0")
    expect(reach_page.locator("#counter")).to_have_text("1 / 1")
    expect(
        reach_page.locator('#img-track .page-cell img[src*="lvl1/l1.png"]')
    ).to_be_visible()
    reach_page.goto(base + "/#/view/lvl1/lvl2?p=0")
    expect(reach_page.locator("#counter")).to_have_text("1 / 2")
    expect(
        reach_page.locator('#img-track .page-cell img[src*="lvl2/l2a.png"]')
    ).to_be_visible()
    expect(
        reach_page.locator('#img-track .page-cell img[src*="lvl2/lvl3"]')
    ).to_have_count(0)

    _browse_to(reach_page, base, "")
    reach_page.locator('.tile[data-read-child="1"]').click()
    expect(reach_page.locator("#counter")).to_have_text("1 / 1")
    expect(
        reach_page.locator('#img-track .page-cell img[src*="lvl1/l1.png"]')
    ).to_be_visible()

    # (e) root loose reachable via the root self-tile.
    _browse_to(reach_page, base, "")
    self_tile = reach_page.locator('.tile[data-read-self="1"]')
    expect(self_tile).to_have_count(1)
    expect(self_tile).to_have_attribute("data-path", "")
    expect(self_tile).to_contain_text(READ_LABEL)
    self_tile.click()
    expect(reach_page.locator("#counter")).to_have_text("1 / 2")
    expect(
        reach_page.locator('#img-track .page-cell img[src*="r1.png"]')
    ).to_be_visible()

    # Flattened chain still opens directly as a leaf (no images-tile anywhere).
    _browse_to(reach_page, base, "")
    reach_page.locator('.tile[data-path="chain"]').click()
    expect(reach_page.locator("#counter")).to_have_text("1 / 2")

    # (d) closure: union over every reachable chapter == every fixture image
    # exactly once (no dupes, none missing). /api/images is direct-children
    # only, matching the loose-only chapter semantics.
    seen: dict[str, int] = {}

    def _chapter(folder):
        url = base + "/api/images?folder=" + urllib.parse.quote(folder, safe="/")
        with urllib.request.urlopen(url, timeout=5) as resp:
            names = json.load(resp)
        assert sorted(names) == sorted(
            REACH_LOOSE.get(folder, REACH_LEAVES.get(folder, []))
        ), f"chapter listing for {folder!r}"
        for n in names:
            key = f"{folder}/{n}" if folder else n
            seen[key] = seen.get(key, 0) + 1

    for folder in list(REACH_LOOSE) + list(REACH_LEAVES):
        _chapter(folder)
    expected = {
        (f"{folder}/{n}" if folder else n)
        for folder, names in {**REACH_LOOSE, **REACH_LEAVES}.items()
        for n in names
    }
    assert set(seen) == expected, "every fixture image reachable exactly once"
    assert all(v == 1 for v in seen.values()), "no duplicate listings"
