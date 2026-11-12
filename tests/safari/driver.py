"""safaridriver process lifecycle + WebDriver session factory.

safaridriver pairs one WebDriver session with THE Safari.app instance —
concurrent sessions are impossible, so the whole suite runs serially against
one safaridriver process. Each test gets a fresh session (new window) for
isolation; the shared profile is scrubbed per test by the caller.
"""

import socket
import subprocess
import time
import urllib.request

from selenium import webdriver
from selenium.webdriver.common.options import ArgOptions


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SafariDriverManager:
    """Owns the safaridriver process for the pytest session."""

    def __init__(self):
        self._port = _free_port()
        self.executor = f"http://127.0.0.1:{self._port}"
        self._proc: subprocess.Popen | None = None
        self._live_sessions = 0

    def start(self) -> None:
        self._proc = subprocess.Popen(
            ["safaridriver", "-p", str(self._port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    "safaridriver exited immediately — run `sudo safaridriver --enable` first"
                )
            try:
                urllib.request.urlopen(f"{self.executor}/status", timeout=0.5).read()
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("safaridriver did not become ready within 15s")

    def new_session(self):
        """Create a WebDriver session (spawns a Safari window)."""
        if self._proc is None:
            self.start()
        opts = ArgOptions()
        opts.browser_name = "safari"
        driver = webdriver.Remote(command_executor=self.executor, options=opts)
        self._live_sessions += 1
        return driver

    def session_released(self) -> None:
        self._live_sessions = max(0, self._live_sessions - 1)

    @property
    def live_sessions(self) -> int:
        return self._live_sessions

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
