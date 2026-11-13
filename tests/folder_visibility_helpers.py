"""Shared helpers for folder-visibility tests that mutate state.

Each test file builds its own synthetic library + its own BOOKQ_STATE_DIR +
its own uvicorn — the shared conftest `server`/`downloads_tree` fixtures keep
their exact root count and never see a hidden flag. Never reuse the session
server for hide/unhide writes.
"""

import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request
import zlib
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def request_browser(request):
    """BROWSER=webkit / --browser=webkit switch, mirroring conftest._is_webkit_request."""
    if os.environ.get("BROWSER") == "webkit":
        return request.getfixturevalue("webkit_browser")
    try:
        if request.config.getoption("--browser", default=None) == "webkit":
            return request.getfixturevalue("webkit_browser")
    except Exception:
        pass
    return request.getfixturevalue("browser")


def make_png(path: Path, w: int = 40, h: int = 60, rgb: tuple = (200, 60, 60)) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(rgb) * w
    idat = zlib.compress(row * h)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
    )


def _metadata_toml(title: str) -> str:
    """Minimal parseable metadata.toml — /api/search scores title_jp/title_en/
    artist/tags only (app/metadata/search.py); folder names alone never match.
    """
    return (
        "[source]\n"
        'name = "generic"\n'
        "\n"
        "[comic]\n"
        f'title_jp = "{title}"\n'
    )


def build_vis_tree(root: Path) -> Path:
    """Top-level roots for visibility tests.

      hide_me/            leaf work (hide target, 2 pages, searchable)
      hide_me_b/          prefix-collision sibling — hiding hide_me must not hide it
      hide_series/vol1|2/ hideable series (descendant check; the root is searchable)
      hide_mixed/         loose image + sub/ → data-read-child duplicate tile
      hide_arc.zip        hideable archive root (1 zip image entry)
      keep_a/             always-visible work, 4 pages → 2 spreads (turnable)
      keep_b/             always-visible work, 1 page
      root.png            root-level loose image → data-read-self tile
    """
    d = root / "hide_me"
    d.mkdir()
    make_png(d / "1.png")
    make_png(d / "2.png")
    (d / "metadata.toml").write_text(_metadata_toml("hide work alpha"))

    d = root / "hide_me_b"
    d.mkdir()
    make_png(d / "1.png")
    (d / "metadata.toml").write_text(_metadata_toml("hide work beta"))

    for vol in ["vol1", "vol2"]:
        d = root / "hide_series" / vol
        d.mkdir(parents=True)
        make_png(d / "p1.png")
    (root / "hide_series" / "metadata.toml").write_text(_metadata_toml("hide series work"))

    d = root / "hide_mixed"
    (d / "sub").mkdir(parents=True)
    make_png(d / "cover.png")
    make_png(d / "sub" / "1.png")
    (d / "metadata.toml").write_text(_metadata_toml("hide mixed work"))

    png_tmp = root / "_zip_src.png"
    make_png(png_tmp)
    with zipfile.ZipFile(root / "hide_arc.zip", "w") as z:
        z.write(png_tmp, "p1.png")
    png_tmp.unlink()

    for name, pages in [("keep_a", 4), ("keep_b", 1)]:
        d = root / name
        d.mkdir()
        for i in range(1, pages + 1):
            make_png(d / f"{i}.png")

    make_png(root / "root.png")
    return root


VIS_ROOTS = ["hide_me", "hide_me_b", "hide_series", "hide_mixed", "hide_arc.zip"]


def build_flat_vis_tree(root: Path) -> Path:
    """Flattened scanner roots: the library root holds ONLY ``wrap/`` — no
    files, no other dirs — so scan_tree hoists wrap's children to the top
    level with ``wrap/``-prefixed names. Those ARE the top-level nodes:
    ``wrap`` itself never exists as a tree node.

      wrap/wA/        flattened leaf work (hide target, 2 pages)
      wrap/wB/        flattened leaf work (always-visible sibling, 2 pages)
      wrap/arc.zip    flattened ARCHIVE root (1 zip image entry)
    """
    for w in ("wA", "wB"):
        d = root / "wrap" / w
        d.mkdir(parents=True)
        make_png(d / "1.png")
        make_png(d / "2.png")
    png_tmp = root / "wrap" / "_z.png"
    make_png(png_tmp)
    with zipfile.ZipFile(root / "wrap" / "arc.zip", "w") as z:
        z.write(png_tmp, "p1.png")
    png_tmp.unlink()
    return root


FLAT_VIS_ROOTS = ["wrap/wA", "wrap/wB", "wrap/arc.zip"]


def spawn_vis_server(tree: Path, state_dir: Path) -> tuple:
    """Same shape as conftest's server fixture but with a dedicated state dir."""
    from rs_spawn import spawn_backend

    return spawn_backend(downloads_dir=tree, state_dir=state_dir)


def _vis_path() -> str:
    # #469: v0 compat surface answers 501 under BOOKQ_TEST_SERVER=rs.
    if os.environ.get("BOOKQ_TEST_SERVER") == "rs":
        return "/api/v1/folder-visibility"
    return "/api/folder-visibility"


def vis_get(base_url: str) -> dict:
    return json.loads(
        urllib.request.urlopen(base_url + _vis_path(), timeout=5).read()
    )


def vis_put(base_url: str, root: str, hidden: bool) -> dict:
    owner = vis_get(base_url)["owner"]
    req = urllib.request.Request(
        base_url + _vis_path(),
        method="PUT",
        data=json.dumps({"root": root, "hidden": hidden}).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Bookq-Visibility-Owner": owner,
        },
    )
    return json.loads(urllib.request.urlopen(req, timeout=5).read())


def vis_reset(base_url: str, roots=VIS_ROOTS) -> None:
    """Un-hide every root a test may have used (state-dir cleanup between tests)."""
    for r in roots:
        try:
            vis_put(base_url, r, False)
        except Exception:
            pass


def swipe_root_icon(page) -> None:
    """The hidden entry/exit gesture: drag the root 📁 header icon to the
    right edge of the screen. Toggles full-display mode both ways."""
    icon = page.locator("#browse-root-icon")
    # The icon renders after .tile does — under parallel CI load the
    # bounding box can still be empty right after the grid paints.
    icon.wait_for(state="visible", timeout=15000)
    box = icon.bounding_box()
    assert box, "#browse-root-icon has no box (not at browse root?)"
    vw = page.viewport_size["width"] if page.viewport_size else 1280
    x0 = box["x"] + box["width"] / 2
    y0 = box["y"] + box["height"] / 2
    page.mouse.move(x0, y0)
    page.mouse.down()
    page.mouse.move(vw - 2, y0, steps=8)
    page.mouse.up()
