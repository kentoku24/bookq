    """Shared test-backend spawner — viewer-rs only (#503, #532).

Spawns the `viewer-rs` binary: same synthetic tree, same contract surface,
exactly as production does (all routes native, no upstream).

Builds the binary with `cargo build -p viewer-rs` on first use when
`rust/target/debug/viewer-rs` is missing.
"""

import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


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

    Free port, env, then a 30s health probe. Cold Rust boots are fast but
    the same headroom covers a loaded machine — and `cargo build` runs
    before this is called.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["BOOKQ_TEST_MODE"] = "true"
    env["DOWNLOADS_DIR"] = str(downloads_dir)
    env["BOOKQ_STATE_DIR"] = str(state_dir)
    env.update(
        {
            "BOOKQ_BIND": f"127.0.0.1:{port}",
            "GOOGLE_CLIENT_ID": "ci-dummy",
            "GOOGLE_CLIENT_SECRET": "ci-dummy",
            "BASE_URL": base_url,
            "ALLOWED_EMAILS": "test@example.com",
            "SECRET_KEY": "ci-not-a-secret",
            "BOOKQ_WEB_DIR": str(REPO_ROOT / "web"),
            "BOOKQ_THUMB_CACHE_DIR": str(Path(state_dir) / "thumbs"),
        }
    )
    cmd = [str(_viewer_rs_binary())]
    health_path = "/api/v1/health"

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
