"""Shared test helpers (deduplicated from per-module copies).

``drive`` consolidates the ``_drive`` copies in test_ensure_index,
test_stale_serve, test_cover_warmup, test_warmup_baseline,
test_scan_phase_timings, test_warmup_breakdown, test_thumb_sprite, and
test_warmup_lifespan; ``make_pdf`` consolidates the minimal multi-page PDF
builder copies in test_thumbs and test_cover_warmup. Import via
``from tests.helpers import drive, make_pdf`` (tests/ has ``__init__.py``).
"""

import asyncio
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path


def drive(coro):
    """Run *coro* to completion on a fresh event loop in a helper thread.

    Playwright's sync API (used by the browser tests in this suite) leaves a
    running event loop behind in the main thread, so plain asyncio.run() fails
    with "cannot be called from a running event loop" when this module runs
    after them. A dedicated thread always has a clean loop state. The
    module-level _index_lock is never contended here (strictly sequential
    awaits), so it never binds to any of these short-lived loops.

    Exceptions raised by *coro* propagate to the caller (needed by
    error-path tests such as test_thumb_sprite's 404 probes).
    """
    outcome = {}

    def target():
        try:
            outcome["value"] = asyncio.run(coro)
        except BaseException as exc:  # propagate to caller
            outcome["error"] = exc

    worker = threading.Thread(target=target)
    worker.start()
    worker.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def make_pdf(path: Path, pages: int = 2) -> None:
    """Minimal multi-page PDF (content stream per page, same bytes as before)."""
    objs: list[str] = ["1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"]
    kids = " ".join(f"{3 + i} 0 R" for i in range(pages))
    objs.append(f"2 0 obj\n<< /Type /Pages /Kids [{kids}] /Count {pages} >>\nendobj\n")
    for i in range(pages):
        objs.append(
            f"{3 + i} 0 obj\n<< /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 200 200] /Contents {3 + pages + i} 0 R >>\nendobj\n"
        )
    for i in range(pages):
        content = f"BT 10 90 Td (Page {i + 1}) Tj ET"
        objs.append(
            f"{3 + pages + i} 0 obj\n<< /Length {len(content)} >>\n"
            f"stream\n{content}\nendstream\nendobj\n"
        )
    path.write_bytes(
        b"%PDF-1.4\n" + "".join(objs).encode() + b"trailer\n<< /Root 1 0 R >>\n%%EOF"
    )


@asynccontextmanager
async def cold_index_lifespan(app):
    yield


def get_folders_ready(client, *, params=None, timeout: float = 30):
    deadline = time.monotonic() + timeout
    while True:
        res = client.get("/api/folders", params=params)
        if res.status_code != 202 or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    assert res.status_code == 200, (
        f"/api/folders did not become ready: status {res.status_code}, "
        f"body {res.text[:500]!r}"
    )
    return res.json()


# ── backend spawn (#469 Phase 2a) ────────────────────────────────────────────
# Single home for the "boot a test server" shape duplicated across
# conftest.py, folder_visibility_helpers.py and the module-local spawners
# (test_filters_e2e, test_mixed_folder, test_ui_logic,
# test_viewer_slider_per_book). BOOKQ_TEST_SERVER=rs boots the viewer-rs
# binary instead of uvicorn; anything else keeps the historical behaviour.

_REPO_ROOT = Path(__file__).resolve().parent.parent


class ViewerRsUnavailable(RuntimeError):
    """viewer-rs binary could not be provided (build failed / cargo absent)."""


def test_backend_is_rs() -> bool:
    """True when the suite is retargeted at the viewer-rs binary (#469)."""
    return os.environ.get("BOOKQ_TEST_SERVER") == "rs"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http_ok(base_url: str, paths: list, proc, timeout: float) -> None:
    """Poll *paths* in order until each answers 2xx (cold index may 202)."""
    deadline = time.time() + timeout
    for path in paths:
        while True:
            if proc.poll() is not None:
                raise RuntimeError(f"test server exited early while probing {path}")
            try:
                with urllib.request.urlopen(base_url + path, timeout=1):
                    break
            except OSError:
                pass
            if time.time() >= deadline:
                raise RuntimeError(f"test server did not serve {path} within {timeout}s")
            time.sleep(0.1)


def _wait_tree_ready(base_url: str, tree_path: str, proc, timeout: float) -> None:
    """Poll the tree endpoint until the cold index build lands (200)."""
    import json as _json

    deadline = time.time() + timeout
    while True:
        if proc.poll() is not None:
            raise RuntimeError("test server exited early while warming the tree")
        try:
            with urllib.request.urlopen(base_url + tree_path, timeout=5) as resp:
                if resp.status == 200:
                    _json.loads(resp.read() or b"{}")
                    return
        except OSError:
            pass
        if time.time() >= deadline:
            raise RuntimeError(f"tree {tree_path} did not warm within {timeout}s")
        time.sleep(0.5)


def _ensure_viewer_rs_binary() -> Path:
    """Return the debug viewer-rs binary, building it on demand.

    Raises ViewerRsUnavailable when the binary is absent and cannot be
    built (callers translate to pytest.skip).
    """
    binary = _REPO_ROOT / "rust" / "target" / "debug" / "viewer-rs"
    if binary.is_file():
        return binary
    try:
        build = subprocess.run(
            ["cargo", "build", "-p", "viewer-rs"],
            cwd=str(_REPO_ROOT / "rust"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ViewerRsUnavailable(f"cargo build -p viewer-rs failed: {exc}") from exc
    if build.returncode != 0 or not binary.is_file():
        raise ViewerRsUnavailable("cargo build -p viewer-rs did not produce the binary")
    return binary


def _rs_env(port: int, downloads: Path, state_dir: str | None, thumb_cache_dir: str | None) -> dict:
    """Env parity for the viewer-rs backend (#469 scope): bind, test mode,
    all-native strict, dummy OAuth, downloads/state/thumb roots, app shell."""
    env = os.environ.copy()
    # No-upstream mode is the deterministic posture: never inherit a
    # developer's proxy target into the test rig.
    env.pop("BOOKQ_UPSTREAM", None)
    env["BOOKQ_TEST_MODE"] = "true"
    env["BOOKQ_BIND"] = f"127.0.0.1:{port}"
    env["BOOKQ_RS_NATIVE"] = "all"
    env["BOOKQ_RS_NATIVE_STRICT"] = "1"
    env["DOWNLOADS_DIR"] = str(downloads)
    if state_dir is not None:
        env["BOOKQ_STATE_DIR"] = str(state_dir)
    if thumb_cache_dir is not None:
        env["BOOKQ_THUMB_CACHE_DIR"] = str(thumb_cache_dir)
    else:
        env.setdefault("BOOKQ_THUMB_CACHE_DIR", str(Path(downloads).parent / f"thumbcache-{port}"))
    env.setdefault("BOOKQ_APP_DIR", str(_REPO_ROOT / "app"))
    # Dummy OAuth / signing config (mirrors .github/workflows/api-contract.yml).
    env.setdefault("GOOGLE_CLIENT_ID", "ci-dummy")
    env.setdefault("GOOGLE_CLIENT_SECRET", "ci-dummy")
    env.setdefault("BASE_URL", f"http://127.0.0.1:{port}")
    env.setdefault("SECRET_KEY", "ci-not-a-secret")
    env.setdefault("ALLOWED_EMAILS", "test@example.com")
    return env


def spawn_test_server(
    downloads: Path,
    *,
    state_dir: str | Path | None = None,
    thumb_cache_dir: str | Path | None = None,
    timeout: float = 30,
) -> tuple:
    """Boot a test server against *downloads*; return (proc, base_url).

    Respects BOOKQ_TEST_SERVER=rs (viewer-rs binary, /api/v1/health probe +
    tree warmup) and otherwise preserves the historical uvicorn behaviour
    (/api/health probe). Raises ViewerRsUnavailable when the rs backend is
    requested but the binary cannot be provided.
    """
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    if test_backend_is_rs():
        binary = _ensure_viewer_rs_binary()
        env = _rs_env(port, downloads, state_dir, thumb_cache_dir)
        proc = subprocess.Popen(
            [str(binary)],
            env=env,
            cwd=str(_REPO_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            _wait_http_ok(base_url, ["/api/v1/health"], proc, timeout)
            _wait_tree_ready(base_url, "/api/v1/tree", proc, timeout)
        except Exception:
            proc.terminate()
            raise
        return proc, base_url
    env = os.environ.copy()
    env["BOOKQ_TEST_MODE"] = "true"
    env["DOWNLOADS_DIR"] = str(downloads)
    if state_dir is not None:
        env["BOOKQ_STATE_DIR"] = str(state_dir)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        cwd=str(_REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_http_ok(base_url, ["/api/health"], proc, timeout)
    except Exception:
        proc.terminate()
        raise
    return proc, base_url


def terminate_test_server(proc) -> None:
    """Best-effort shutdown for a server from spawn_test_server."""
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
