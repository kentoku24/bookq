"""S10 (#85) — runner-side fixture + control server.

A tiny threaded HTTP server the two device surfaces connect back to:

- ``GET /fixture?id=<client>&role=<role>`` — the fixture page (fixture.html)
  with ``__FX_CONFIG_JSON__`` substituted per client: the sync WS URL, the
  control base (this server), and the production module URLs on the target
  origin.
- ``GET /ctrl/next?id=<client>`` — long-poll: the next queued command for
  that client (25 s hold, then ``{}``).
- ``POST /ctrl/hello`` — client boot report (ua, hasRTC, module status).
- ``POST /ctrl/result`` — command result (``{id, seq, res:{ok,data|error}}``).

The runner side uses ``cmd(client, name, **params)`` which returns the
fixture's data payload or raises :class:`CmdError`. Pairing codes flow
through result payloads but are stripped by the ops layer before evidence
is written (spec: コードは収集しない).
"""
from __future__ import annotations

import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

FIXTURE_HTML = Path(__file__).resolve().parent / "fixture.html"


class CmdError(RuntimeError):
    """Control-command failure. ``kind`` feeds the exit-code mapping:
    'env' (device/command unreachable) or 'reject'/'error' (asserted)."""

    def __init__(self, kind: str, message: str):
        super().__init__(f"{kind}: {message}")
        self.kind = kind


class FixtureServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self._httpd.fx = self
        self.host = host
        self.port = self._httpd.server_address[1]
        self._lock = threading.Lock()
        self._clients: dict[str, dict] = {}
        self._seq = 0
        self._thread: threading.Thread | None = None

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.2},
            daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    def base_url(self, host: str | None = None) -> str:
        return f"http://{host or self.host}:{self.port}"

    # ── runner API ───────────────────────────────────────────────────

    def fixture_url(self, client_id: str, role: str, ws_url: str,
                    base_url: str, host: str | None = None) -> str:
        base = self.base_url(host)
        return (base + "/fixture?" + urlencode(
            {"id": client_id, "role": role, "ws": ws_url, "base": base_url}))

    def wait_hello(self, client_id: str, timeout: float = 30.0) -> dict:
        """Wait for the fixture page's boot report."""
        deadline = time.monotonic() + timeout
        with self._lock:
            self._clients.setdefault(client_id, self._new_client())
        while time.monotonic() < deadline:
            with self._lock:
                hello = self._clients[client_id].get("hello")
            if hello is not None:
                return hello
            time.sleep(0.05)
        raise CmdError("env", f"fixture client {client_id} never said hello")

    def cmd(self, client_id: str, name: str, params: dict | None = None,
            timeout: float = 30.0):
        """Queue a command for a fixture client; wait for its result."""
        with self._lock:
            c = self._clients.setdefault(client_id, self._new_client())
            self._seq += 1
            seq = self._seq
            ev = threading.Event()
            c["results"][seq] = None
            c["events"][seq] = ev
        c["queue"].put({"seq": seq, "cmd": name, "params": params or {}})
        if not ev.wait(timeout):
            with self._lock:
                c["events"].pop(seq, None)
                c["results"].pop(seq, None)
            raise CmdError("env", f"cmd {name} to {client_id} timed out "
                                  f"({timeout}s)")
        with self._lock:
            res = c["results"].pop(seq, None)
            c["events"].pop(seq, None)
        if res is None:
            raise CmdError("env", f"cmd {name} to {client_id}: no result")
        if not res.get("ok"):
            raise CmdError("error", str(res.get("error")))
        return res.get("data")

    def client_connected(self, client_id: str) -> bool:
        with self._lock:
            return client_id in self._clients and \
                self._clients[client_id].get("hello") is not None

    # ── handler-facing API (called on server threads) ────────────────

    @staticmethod
    def _new_client() -> dict:
        return {"queue": queue.Queue(), "results": {}, "events": {},
                "hello": None}

    def _client(self, client_id: str) -> dict:
        with self._lock:
            return self._clients.setdefault(client_id, self._new_client())

    def post_hello(self, payload: dict) -> None:
        cid = str(payload.get("id") or "")
        if not cid:
            return
        self._client(cid)["hello"] = payload

    def post_result(self, payload: dict) -> None:
        cid = str(payload.get("id") or "")
        seq = payload.get("seq")
        c = self._client(cid)
        with self._lock:
            c["results"][seq] = payload.get("res")
            ev = c["events"].get(seq)
        if ev is not None:
            ev.set()

    def next_command(self, client_id: str, timeout: float = 25.0) -> dict:
        c = self._client(client_id)
        try:
            return c["queue"].get(timeout=timeout)
        except queue.Empty:
            return {}

    def fixture_page(self, client_id: str, role: str, ws_url: str,
                     base_url: str, req_host: str) -> bytes:
        mods = [base_url.rstrip("/") + "/static/viewer/pairTransport.js",
                base_url.rstrip("/") + "/static/viewer/pairModel.js"]
        cfg = {"id": client_id, "role": role, "wsUrl": ws_url,
               "ctrlBase": f"http://{req_host}/ctrl", "mods": mods}
        html = FIXTURE_HTML.read_text(encoding="utf-8")
        html = html.replace("__FX_CONFIG_JSON__", json.dumps(cfg))
        return html.encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):          # quiet — evidence is explicit
        return

    @property
    def fx(self) -> FixtureServer:
        return self.server.fx

    def _send(self, code: int, body: bytes, ctype="application/json",
              extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/fixture":
            cid = q.get("id", ["?"])[0]
            role = q.get("role", ["leader"])[0]
            ws = q.get("ws", [""])[0]
            base = q.get("base", [""])[0]
            self._send(200, self.fx.fixture_page(
                cid, role, ws, base, self.headers.get("Host", "localhost")),
                ctype="text/html; charset=utf-8")
            return
        if u.path == "/ctrl/next":
            cid = q.get("id", [""])[0]
            cmd = self.fx.next_command(cid, timeout=25.0)
            self._send(200, json.dumps(cmd).encode("utf-8"))
            return
        if u.path == "/health":
            self._send(200, b'{"ok":true}')
            return
        self._send(404, b'{"error":"not found"}')

    def do_POST(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path == "/ctrl/hello":
            self.fx.post_hello(self._read_json())
            self._send(200, b'{"ok":true}')
            return
        if u.path == "/ctrl/result":
            self.fx.post_result(self._read_json())
            self._send(200, b'{"ok":true}')
            return
        self._send(404, b'{"error":"not found"}')
