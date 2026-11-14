"""Shared test-backend spawner — uvicorn or viewer-rs under BOOKQ_TEST_SERVER (#469).

`BOOKQ_TEST_SERVER=rs` swaps the spawned process from `uvicorn app.main:app`
to the `viewer-rs` binary: same synthetic tree, same contract surface. The
binary boots no-upstream (`BOOKQ_RS_NATIVE=all` + `BOOKQ_RS_NATIVE_STRICT=1`),
so v0 endpoints answer 501 exactly as production does.

Builds the binary with `cargo build -p viewer-rs` on first use when
`rust/target/debug/viewer-rs` is missing.
"""

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

RS_MODE = os.environ.get("BOOKQ_TEST_SERVER") == "rs"


def _viewer_rs_binary() -> Path:
    bin_path = REPO_ROOT / "rust" / "target" / "debug" / "viewer-rs"
    if not bin_path.exists():
        subprocess.run(
            ["cargo", "build", "-p", "viewer-rs"],
            cwd=REPO_ROOT / "rust",
            check=True,
        )
    return bin_path


def spawn_backend(*, downloads_dir: Path, state_dir: str | Path) -> tuple:
    """Spawn the test backend against *downloads_dir*; returns (proc, base_url).

    Equivalent to the previous inline uvicorn block: free port, env, then a
    30s health probe. Cold Rust boots are fast but the same headroom covers
    a loaded machine — and `cargo build` runs before this is called.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["BOOKQ_TEST_MODE"] = "true"
    env["DOWNLOADS_DIR"] = str(downloads_dir)
    env["BOOKQ_STATE_DIR"] = str(state_dir)
    if RS_MODE:
        env.update(
            {
                "BOOKQ_BIND": f"127.0.0.1:{port}",
                "BOOKQ_RS_NATIVE": "all",
                "BOOKQ_RS_NATIVE_STRICT": "1",
                # STRICT requires complete OAuth config even in test mode.
                "GOOGLE_CLIENT_ID": "ci-dummy",
                "GOOGLE_CLIENT_SECRET": "ci-dummy",
                "BASE_URL": base_url,
                "ALLOWED_EMAILS": "test@example.com",
                "SECRET_KEY": "ci-not-a-secret",
                "BOOKQ_APP_DIR": str(REPO_ROOT / "app"),
                "BOOKQ_THUMB_CACHE_DIR": str(Path(state_dir) / "thumbs"),
            }
        )
        cmd = [str(_viewer_rs_binary())]
        # v0 /api/health answers 501 with no upstream.
        health_path = "/api/v1/health"
    else:
        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]
        health_path = "/api/health"

    proc = subprocess.Popen(
        cmd,
        env=env,
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Cold starts (metadata warmup + first index build) take ~7-10s on a
    # loaded machine — 10s is a coin flip, so give real headroom before
    # declaring a startup failure.
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("test backend exited early")
        try:
            urllib.request.urlopen(base_url + health_path, timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.terminate()
        raise RuntimeError("test backend did not become healthy within 30s")
    return proc, base_url
