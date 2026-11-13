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

    # Safari leaks one pairing slot per WebDriver session (~20 lifetime) —
    # recycle the driver process before the well runs dry.
    _RECYCLE_AFTER = 15

    def __init__(self):
        self._port = _free_port()
        self.executor = f"http://127.0.0.1:{self._port}"
        self._proc: subprocess.Popen | None = None
        self._live_sessions = 0
        self._served = 0
        self._last_driver = None

    def recycle(self) -> None:
        """Restart safaridriver (and Safari.app, which holds leaked
        pairings) — recovers from the ~20-session pairing exhaustion."""
        self.stop()
        self._proc = None
        self._live_sessions = 0
        self._served = 0
        subprocess.run(
            ["osascript", "-e", 'tell application "Safari" to quit'],
            capture_output=True, timeout=10,
        )
        time.sleep(1.0)
        subprocess.run(["pkill", "-x", "Safari"], capture_output=True)
        time.sleep(0.5)

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
        if self._served >= self._RECYCLE_AFTER:
            self.recycle()
        if self._proc is None:
            self.start()
        opts = ArgOptions()
        opts.browser_name = "safari"
        # BiDi gives script.evaluate in the page's REAL realm — classic
        # execute_script runs in an isolated automation world where
        # localStorage writes roll back and globals stay invisible to the app.
        opts.set_capability("webSocketUrl", True)
        opts.set_capability("safari:experimentalWebSocketUrl", True)
        driver = webdriver.Remote(command_executor=self.executor, options=opts)
        self._live_sessions += 1
        self._served += 1
        self._last_driver = driver
        return driver

    def force_quit_stale(self) -> None:
        """Safari kept pairing a session whose quit() silently failed —
        the driver handle is still valid, so retry quitting it."""
        drv = self._last_driver
        if drv is not None:
            try:
                drv.quit()
            except Exception:
                pass
        # Safari.app can also just be slow to un-pair; give it a beat.
        time.sleep(0.8)

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
            self._proc = None
