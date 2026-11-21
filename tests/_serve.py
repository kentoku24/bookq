"""Parent-death watchdog launcher for test-spawned servers (#139).

argv: ``<_serve.py> <pipe_read_fd> <cmd> [args...]``

The pytest process holds the write end of the pipe; this process holds
the read end. EOF — parent exit of ANY kind including SIGKILL, which
fixture teardowns never see on macOS (no PR_SET_PDEATHSIG) — triggers a
process-group SIGTERM, then SIGKILL after a grace period.

The server child stays in THIS launcher's process group (the launcher
is the session/group leader via ``start_new_session`` in
``_server_proc.spawn``), so one ``killpg`` covers launcher + server +
any grandchildren the server itself forked.
"""

import os
import signal
import subprocess
import sys
import threading
import time


def _kill_group(sig: int) -> None:
    try:
        os.killpg(os.getpgrp(), sig)
    except (ProcessLookupError, PermissionError):
        pass


def main() -> int:
    fd = int(sys.argv[1])
    proc = subprocess.Popen(sys.argv[2:])

    # The group SIGTERM below must reach the server subtree without
    # killing the launcher mid-cleanup — SIGTERM is ignored here;
    # SIGKILL is not ignorable and remains the final backstop.
    # This MUST come after Popen: ignored dispositions are inherited
    # across exec, so setting it earlier would leave the server
    # unkillable by SIGTERM (only the 5s-delayed SIGKILL backstop
    # would work — too slow for the 3s orphan-detection contract).
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

    parent_gone = threading.Event()

    def watch() -> None:
        # The parent never writes — read blocks until the write end
        # closes (EOF) or errors. No select(): pipe fds can exceed
        # FD_SETSIZE in a long pytest process.
        try:
            while os.read(fd, 65536):
                pass
        except OSError:
            pass
        parent_gone.set()

    threading.Thread(target=watch, daemon=True).start()

    while proc.poll() is None and not parent_gone.is_set():
        time.sleep(0.1)

    if parent_gone.is_set() and proc.poll() is None:
        _kill_group(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        # Unconditional escalation: group members that ignored SIGTERM
        # (or grandchildren outliving the direct child) still die.
        _kill_group(signal.SIGKILL)
        proc.wait()

    return proc.returncode if proc.returncode is not None else 0


if __name__ == "__main__":
    sys.exit(main())
