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


def spawn_vis_server(tree: Path, state_dir: Path) -> tuple:
    """Same shape as conftest's server fixture but with a dedicated state dir."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = os.environ.copy()
    env["BOOKQ_TEST_MODE"] = "true"
    env["DOWNLOADS_DIR"] = str(tree)
    env["BOOKQ_STATE_DIR"] = str(state_dir)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        env=env, cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("visibility test server exited early")
        try:
            urllib.request.urlopen(base_url + "/api/health", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.terminate()
        raise RuntimeError("visibility test server did not become healthy in 10s")
    return proc, base_url


def vis_get(base_url: str) -> dict:
    return json.loads(
        urllib.request.urlopen(base_url + "/api/folder-visibility", timeout=5).read()
    )


def vis_put(base_url: str, root: str, hidden: bool) -> dict:
    owner = vis_get(base_url)["owner"]
    req = urllib.request.Request(
        base_url + "/api/folder-visibility",
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
