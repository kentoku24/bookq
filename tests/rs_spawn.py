"""Shared test-backend spawner — viewer-rs only (#503, #532).

Spawns the `viewer-rs` binary: same synthetic tree, same contract surface,
exactly as production does (all routes native, no upstream).

Builds the binary with `cargo build -p viewer-rs` on first use when
`rust/target/debug/viewer-rs` is missing. Set BOOKQ_TARGET_DIR (or
CARGO_TARGET_DIR / CARGO_TARGET_DIR_BASE) to a directory on an external
volume to keep the ~1.5GB target dir off the internal SSD — the value is
a root; a per-checkout subdir is appended so parallel worktrees never
share one target dir.
"""

import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _external_root_ok(root: Path) -> bool:
    """True when some ancestor of `root` (itself included) is a real mount
    point under /Volumes/. Guards against writing into the phantom
    /Volumes/<name> dir an unmount can leave on the startup disk."""
    p = root.resolve()
    while True:
        if str(p).startswith("/Volumes/") and os.path.ismount(p):
            return True
        if p.parent == p:
            return False
        p = p.parent


def _target_dir() -> Path:
    """Cargo target dir for this checkout.

    BOOKQ_TARGET_DIR / CARGO_TARGET_DIR_BASE / CARGO_TARGET_DIR is treated
    as a *root* — the checkout name is appended so parallel worktrees can
    never fight over one target dir and overwrite each other's binary.
    Falls back to rust/target when unset or no external volume is mounted.
    """
    root = (
        os.environ.get("BOOKQ_TARGET_DIR")
        or os.environ.get("CARGO_TARGET_DIR_BASE")
        or os.environ.get("CARGO_TARGET_DIR")
    )
    if root:
        root_path = Path(root).expanduser()
        if _external_root_ok(root_path):
            return root_path / REPO_ROOT.name
        print(f"rs_spawn: {root} is not a mounted external volume — using rust/target")
    return REPO_ROOT / "rust" / "target"


def _viewer_rs_binary() -> Path:
    target = _target_dir()
    bin_path = target / "debug" / "viewer-rs"
    if not bin_path.exists():
        subprocess.run(
            ["cargo", "build", "-p", "viewer-rs"],
            cwd=REPO_ROOT / "rust",
            env={**os.environ, "CARGO_TARGET_DIR": str(target)},
            check=True,
        )
    return bin_path


def spawn_backend(*, library_dir: Path, state_dir: str | Path) -> tuple:
    """Spawn the test backend against *library_dir*; returns (proc, base_url).

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
    env["LIBRARY_DIR"] = str(library_dir)
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
