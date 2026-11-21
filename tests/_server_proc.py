"""Parent-death-safe spawn/stop for pytest-spawned servers (#139).

``spawn()`` wraps the real command in ``tests/_serve.py`` — a watchdog
that kills the whole process group when the pytest-side pipe write end
closes. That covers pytest death by ANY means: fixture teardowns never
run on SIGKILL/timeouts, and macOS has no ``PR_SET_PDEATHSIG``.

``stop()`` is the explicit path — closing the pipe makes the watchdog
group-TERM the server subtree; the launcher group then gets SIGKILL as
backstop. Idempotent. ``stop_all()`` runs from ``atexit`` and
``pytest_sessionfinish`` for anything a crashed fixture abandoned.
"""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_LAUNCHER = REPO_ROOT / "tests" / "_serve.py"

_PROCS: "dict[int, ServerProc]" = {}


class ServerProc:
    """Popen-compatible facade over the watchdog launcher.

    ``terminate()``/``kill()`` run the full group stop, so existing
    fixture teardowns (``proc.terminate(); proc.wait(timeout=5)``)
    keep working while actually reaping the whole subtree.
    """

    def __init__(self, proc: subprocess.Popen, wfd: int) -> None:
        self._proc = proc
        self._wfd = wfd
        self._stopped = False

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def returncode(self):
        return self._proc.returncode

    def poll(self):
        return self._proc.poll()

    def wait(self, timeout=None):
        return self._proc.wait(timeout=timeout)

    def terminate(self) -> None:
        self.stop()

    def kill(self) -> None:
        self.stop()

    def stop(self, grace: float = 5.0) -> None:
        if self._stopped:
            return
        self._stopped = True
        try:
            # EOF on the watchdog's read end → group-TERM the subtree.
            os.close(self._wfd)
        except OSError:
            pass
        self._wfd = -1
        deadline = time.time() + grace
        while self._proc.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        if self._proc.poll() is None:
            # Watchdog wedged — kill the launcher group outright; the
            # server subtree shares that group.
            try:
                os.killpg(self._proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        _PROCS.pop(self._proc.pid, None)


def spawn(cmd, *, env=None, cwd=None, stdout=None, stderr=None) -> ServerProc:
    """Spawn *cmd* under the parent-death watchdog; returns ServerProc."""
    r, w = os.pipe()
    try:
        proc = subprocess.Popen(
            [sys.executable, str(_LAUNCHER), str(r)]
            + [str(c) for c in cmd],
            env=env,
            cwd=cwd,
            stdout=stdout,
            stderr=stderr,
            pass_fds=(r,),
            start_new_session=True,
        )
    except BaseException:
        os.close(r)
        os.close(w)
        raise
    os.close(r)
    sp = ServerProc(proc, w)
    _PROCS[proc.pid] = sp
    return sp


def stop_all() -> None:
    """Stop every live spawned server — atexit + pytest_sessionfinish."""
    for sp in list(_PROCS.values()):
        sp.stop()
    _PROCS.clear()


atexit.register(stop_all)
