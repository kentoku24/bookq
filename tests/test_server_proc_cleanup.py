"""Regression tests for tests/_server_proc.py + tests/_serve.py (#139).

The orphan scenario is reproduced for real (no mocks): an intermediate
process plays the pytest role — it calls ``_server_proc.spawn`` and is
then SIGKILLed, which fixture teardowns can never intercept. The
watchdog must still take the server subtree down.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))

import _server_proc  # noqa: E402


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _wait_dead(check, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not check():
            return True
        time.sleep(0.05)
    return not check()


def test_spawned_server_dies_with_parent_sigkill(tmp_path):
    """Intermediate (pytest surrogate) is SIGKILLed → watchdog sees pipe
    EOF → the spawned server must be gone within 3s (#139 acceptance)."""
    pidfile = tmp_path / "server.pid"
    child_cmd = [
        sys.executable,
        "-c",
        "import os,time;"
        f"open({str(pidfile)!r},'w').write(str(os.getpid()));"
        "time.sleep(600)",
    ]
    intermediate = tmp_path / "intermediate.py"
    intermediate.write_text(
        "import subprocess, sys, time\n"
        f"sys.path.insert(0, {str(Path(__file__).parent)!r})\n"
        "import _server_proc\n"
        f"_server_proc.spawn({child_cmd!r}, stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL)\n"
        "time.sleep(600)\n"
    )
    parent = subprocess.Popen(
        [sys.executable, str(intermediate)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 15
        while not pidfile.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert pidfile.exists(), "spawned server never wrote its pid"
        child_pid = int(pidfile.read_text().strip())
        assert _pid_alive(child_pid)

        parent.kill()  # SIGKILL — teardown code can never run
        parent.wait(timeout=5)

        assert _wait_dead(lambda: _pid_alive(child_pid), 3.0), (
            f"server pid {child_pid} survived parent SIGKILL"
        )
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)


def test_stop_kills_whole_process_group(tmp_path):
    """stop() must reap the group, not just the direct child: the server
    cmd forks a grandchild that would survive a plain terminate()."""
    child_cmd = [
        sys.executable,
        "-c",
        "import subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(600)']);"
        "time.sleep(600)",
    ]
    proc = _server_proc.spawn(
        child_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pgid = proc.pid  # launcher is the process-group leader
    try:
        proc.terminate()
        assert proc.poll() is not None
        # Group-empty check catches grandchildren a direct-child kill
        # would leave behind (reparented orphans keep the pgid alive).
        assert _wait_dead(lambda: _group_alive(pgid), 5.0), (
            f"process group {pgid} still has members after stop()"
        )
    finally:
        proc.stop()


def test_spawned_process_exits_cleanly_and_unregisters(tmp_path):
    """A server that exits on its own propagates liveness through the
    launcher (spawn_backend's 'exited early' probe depends on it) and
    leaves no registry entry behind."""
    proc = _server_proc.spawn(
        [sys.executable, "-c", "import sys; sys.exit(3)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 10
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.05)
    assert proc.poll() is not None, "launcher did not follow child exit"
    assert proc.returncode == 3, f"child exit code not propagated: {proc.returncode}"
    proc.stop()
