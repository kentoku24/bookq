"""Shared test-backend spawner — viewer-rs only (#503, #532).

Spawns the `viewer-rs` binary: same synthetic tree, same contract surface,
exactly as production does (all routes native, no upstream).

Runs `cargo build -p viewer-rs` before every spawn — cheap when fresh —
because the binary at target/debug/viewer-rs can be another worktree's
build when the target dir is shared. The spawned path is resolved to the
fingerprint-hashed deps/viewer_rs-<hash> so a concurrent build in another
worktree cannot swap the binary underneath us.

`rust/target` is normally a symlink to one shared target dir on an
external volume (BOOKQ_SHARED_TARGET_DIR, no default — machine-specific
paths stay out of the repo) so parallel worktrees reuse each other's dep
artifacts instead of each writing ~1.5GB to the internal SSD. The symlink
is created here / by scripts/ensure-cargo-target.sh / by dev.sh up.

Set BOOKQ_TARGET_DIR (or CARGO_TARGET_DIR / CARGO_TARGET_DIR_BASE) to a
directory on an external volume to opt back into the #542 layout instead:
the value is a root; a per-checkout subdir is appended so parallel
worktrees never share one target dir.
"""

import filecmp
import os
import socket
import subprocess
import time
import urllib.error
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


def _ensure_shared_target() -> None:
    """Point rust/target at the shared external target dir via symlink.

    No-op when rust/target already exists (link or real dir — a real dir is
    left alone so nothing is ever deleted here) or when the shared root is
    not on a mounted external volume. Kept in sync with
    scripts/ensure-cargo-target.sh.
    """
    target = REPO_ROOT / "rust" / "target"
    if target.is_symlink() or target.exists():
        return
    root = os.environ.get("BOOKQ_SHARED_TARGET_DIR")
    if not root:
        return
    shared = Path(root).expanduser()
    if not _external_root_ok(shared.parent):
        return
    try:
        shared.mkdir(parents=True, exist_ok=True)
        target.symlink_to(shared)
    except OSError:
        pass


def _stable_binary(bin_path: Path) -> Path:
    """Resolve `debug/viewer-rs` to the identical `deps/viewer_rs-<hash>`.

    The debug/ path is a single name per target dir — a build in another
    worktree sharing the dir replaces it. The deps/ artifact's name carries
    the build fingerprint, so it survives other worktrees' builds. Cargo
    serializes builds on the shared dir, so right after our build returns
    bin_path still holds *our* bytes; we find the deps file with identical
    content (inode comparison is unreliable — APFS clonefile gives the
    copy a new inode). Falls back to bin_path when no match is found.
    """
    try:
        size = bin_path.stat().st_size
    except OSError:
        return bin_path
    try:
        candidates = sorted(
            (p for p in (bin_path.parent / "deps").iterdir()
             if p.name.startswith("viewer_rs-") and p.suffix == ""),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return bin_path
    for p in candidates:
        try:
            if p.stat().st_size == size and filecmp.cmp(bin_path, p, shallow=False):
                return p
        except OSError:
            continue
    return bin_path


def viewer_rs_binary() -> Path:
    target = _target_dir()
    if target == REPO_ROOT / "rust" / "target":
        _ensure_shared_target()
    subprocess.run(
        ["cargo", "build", "-p", "viewer-rs"],
        cwd=REPO_ROOT / "rust",
        env={**os.environ, "CARGO_TARGET_DIR": str(target)},
        check=True,
    )
    return _stable_binary(target / "debug" / "viewer-rs")


def spawn_backend(
    *,
    library_dir: Path,
    state_dir: str | Path,
    test_mode: bool = True,
    env_extra: dict[str, str] | None = None,
) -> tuple:
    """Spawn the test backend against *library_dir*; returns (proc, base_url).

    Free port, env, then a 30s health probe. Cold Rust boots are fast but
    the same headroom covers a loaded machine — and `cargo build` runs
    before this is called.

    ``test_mode=False`` spawns with real signed-cookie/Bearer auth. There is
    no env allowlist; enroll users with ``users_cli()`` before requesting a
    session (#610). ``env_extra`` adds/overrides env vars for targeted
    regression tests (e.g. a retired variable must be ignored).
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    if test_mode:
        env["BOOKQ_TEST_MODE"] = "true"
    else:
        env.pop("BOOKQ_TEST_MODE", None)
    env["LIBRARY_DIR"] = str(library_dir)
    env["BOOKQ_STATE_DIR"] = str(state_dir)
    env.update(
        {
            "BOOKQ_BIND": f"127.0.0.1:{port}",
            "GOOGLE_CLIENT_ID": "ci-dummy",
            "GOOGLE_CLIENT_SECRET": "ci-dummy",
            "BASE_URL": base_url,
            "SECRET_KEY": "ci-not-a-secret",
            "BOOKQ_WEB_DIR": str(REPO_ROOT / "web"),
            "BOOKQ_THUMB_CACHE_DIR": str(Path(state_dir) / "thumbs"),
        }
    )
    if env_extra:
        env.update(env_extra)
    cmd = [str(viewer_rs_binary())]
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
    # Health answers before the metadata index finishes building — the tree
    # endpoint keeps returning 202 + Retry-After until then, and a browser
    # test that loads inside that window makes the client's own 202-retry
    # loop emit an extra bare /api/v1/tree fetch (flaked
    # test_startup_fetches_folders_exactly_twice_and_seeds_etag). Wait for
    # the first non-202 so session-scoped fixtures hand tests a settled
    # backend. Bounded: a wedged build degrades to the old behavior rather
    # than hanging the whole suite.
    tree_deadline = time.time() + 30
    while time.time() < tree_deadline:
        if proc.poll() is not None:
            raise RuntimeError("test backend exited early")
        try:
            resp = urllib.request.urlopen(base_url + "/api/v1/tree", timeout=2)
            resp.close()
            if resp.status != 202:
                break
        except urllib.error.HTTPError as e:
            if e.code != 202:
                break
        except OSError:
            pass
        time.sleep(0.25)
    return proc, base_url


def users_cli(
    state_dir: str | Path,
    *args: str,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run `viewer-rs users …` against *state_dir* (#608).

    The CLI writes through the same store the `/admin` UI uses; tests drive
    the CLI directly. ``cargo build`` runs via ``viewer_rs_binary()``
    (fresh check) so the CLI always matches the spawned backend.
    stdout/stderr are captured for exit-code/output tests. ``env_extra``
    overrides the inherited env for targeted regression tests.
    """
    env = os.environ.copy()
    env["BOOKQ_STATE_DIR"] = str(state_dir)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [str(viewer_rs_binary()), "users", *args],
        env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
