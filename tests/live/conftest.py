"""Shared fixtures for live-library regression (#31). See README.md."""
import hashlib
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

LIVE_ROOT = os.environ.get("BOOKQ_LIVE_ROOT", "")
TOP_N = int(os.environ.get("BOOKQ_LIVE_TOP", "6"))
RANDOM_N = int(os.environ.get("BOOKQ_LIVE_RANDOM", "6"))
SEED = int(os.environ.get("BOOKQ_LIVE_SEED", "7"))

REPO_ROOT = Path(__file__).resolve().parents[2]


from live.discover import census as _census, get_tree as _get_tree, hid as _hid, walk_leaves as _walk_leaves


@pytest.fixture(scope="session")
def live_root():
    if not LIVE_ROOT:
        pytest.skip("live-only: set BOOKQ_LIVE_ROOT to run")
    assert os.path.isdir(LIVE_ROOT), "BOOKQ_LIVE_ROOT is not a directory"
    if os.environ.get("BOOKQ_TEST_MODE", "") != "true":
        pytest.skip("live-only: BOOKQ_TEST_MODE=true is required")
    before = _census(LIVE_ROOT)
    yield LIVE_ROOT
    after = _census(LIVE_ROOT)
    assert before == after, "live tree changed during the run"


@pytest.fixture(scope="session")
def live_server(live_root, tmp_path_factory):
    """uvicorn against the live root with caches sandboxed to tmp."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cache = tmp_path_factory.mktemp("live-cache")
    env = os.environ.copy()
    env["BOOKQ_TEST_MODE"] = "true"
    env["DOWNLOADS_DIR"] = str(live_root)
    env["BOOKQ_THUMB_CACHE_DIR"] = str(cache / "thumbs")
    env["BOOKQ_CACHE_DIR"] = str(cache / "warmup")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 180
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("live server exited early")
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=2):
                break
        except OSError:
            time.sleep(0.5)
    else:
        proc.terminate()
        raise RuntimeError("live server did not become healthy")
    yield base
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(scope="session")
def live_sample(live_server):
    """Deterministic sample of leaf folders: TOP_N biggest + RANDOM_N more.
    Entries carry hash IDs only: {"id", "path", "images"}."""
    tree = _get_tree(live_server)
    leaves = [lf for lf in _walk_leaves(tree) if len(lf["images"]) >= 5]
    assert leaves, "no leaf folders with >= 5 images discovered"
    by_size = sorted(leaves, key=lambda lf: -len(lf["images"]))
    top = by_size[:TOP_N]
    rest = [lf for lf in by_size[TOP_N:] if lf not in top]
    extra = random.Random(SEED).sample(rest, min(RANDOM_N, len(rest)))
    sample = []
    seen = set()
    for lf in top + extra:
        hid = _hid(lf["path"])
        assert hid not in seen, "hash collision in sample"
        seen.add(hid)
        sample.append({"id": hid, "path": lf["path"], "images": lf["images"]})
    return sample


@pytest.fixture()
def live_page(browser, live_server):
    ctx = browser.new_context(
        viewport={"width": 1280, "height": 800}, has_touch=True
    )
    page = ctx.new_page()
    yield page
    ctx.close()
