"""S10 (#85) / S11 (#86) — runner-side fixture + control server.

A tiny threaded HTTP server the two device surfaces connect back to:

- ``GET /fixture?id=<client>&role=<role>`` — the fixture page (fixture.html)
  with ``__FX_CONFIG_JSON__`` substituted per client: the sync WS URL, the
  control base (this server), the image base, and the production module
  URLs on the target origin.
- ``GET /manifest.json?...`` / ``GET /icon.png`` — a standalone web-app
  manifest + icon for the fixture page itself, so it installs on the iOS
  home screen as a REAL standalone PWA (``navigator.standalone === true``)
  and the runner can drive Safari-vs-PWA surface combinations (#86).
- ``GET /ctrl/next?id=<client>`` — long-poll: the next queued command for
  that client (25 s hold, then ``{}``).
- ``POST /ctrl/hello`` — client boot report (ua, hasRTC, standalone,
  module status). ``hello_seq`` counts boots per client so a PWA relaunch
  (a fresh page load inside the web clip) is distinguishable.
- ``POST /ctrl/result`` — command result (``{id, seq, res:{ok,data|error}}``).

The runner side uses ``cmd(client, name, **params)`` which returns the
fixture's data payload or raises :class:`CmdError`. Pairing codes flow
through result payloads but are stripped by the ops layer before evidence
is written (spec: コードは収集しない).
"""
from __future__ import annotations

import json
import queue
import struct
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

FIXTURE_HTML = Path(__file__).resolve().parent / "fixture.html"


def _png_solid(w: int, h: int, rgb: tuple[int, int, int]) -> bytes:
    """Minimal stdlib PNG encoder — a solid-colour square for the webclip
    icon. Keeps the fixture dependency-free (no PIL)."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


_ICON_PNG = _png_solid(180, 180, (36, 64, 128))


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

    def wait_hello(self, client_id: str, timeout: float = 30.0,
                   min_seq: int = 1) -> dict:
        """Wait for the fixture page's boot report. ``min_seq`` lets a
        caller wait for a LATER boot — e.g. the page reloading inside the
        installed PWA web clip after the Safari bootstrap load."""
        deadline = time.monotonic() + timeout
        with self._lock:
            self._clients.setdefault(client_id, self._new_client())
        while time.monotonic() < deadline:
            with self._lock:
                hello = self._clients[client_id].get("hello")
                seq = self._clients[client_id].get("hello_seq", 0)
            if hello is not None and seq >= min_seq:
                return hello
            time.sleep(0.05)
        raise CmdError("env", f"fixture client {client_id} never said hello "
                              f"(seq {seq} < {min_seq})")

    def hello_seq(self, client_id: str) -> int:
        with self._lock:
            c = self._clients.get(client_id)
            return c.get("hello_seq", 0) if c else 0

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
                "hello": None, "hello_seq": 0}

    def _client(self, client_id: str) -> dict:
        with self._lock:
            return self._clients.setdefault(client_id, self._new_client())

    def post_hello(self, payload: dict) -> None:
        cid = str(payload.get("id") or "")
        if not cid:
            return
        c = self._client(cid)
        c["hello"] = payload
        c["hello_seq"] = c.get("hello_seq", 0) + 1

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
                base_url.rstrip("/") + "/static/viewer/pairModel.js",
                base_url.rstrip("/") + "/static/viewer/pairClient.js"]
        base = base_url.rstrip("/")
        query = urlencode({"id": client_id, "role": role, "ws": ws_url,
                           "base": base})
        ctrl = f"http://{req_host}"
        cfg = {"id": client_id, "role": role, "wsUrl": ws_url,
               "ctrlBase": ctrl + "/ctrl", "imgBase": base + "/image",
               "manifestUrl": ctrl + "/manifest.json?" + query,
               "iconUrl": ctrl + "/icon.png",
               "mods": mods}
        html = FIXTURE_HTML.read_text(encoding="utf-8")
        html = html.replace("__FX_CONFIG_JSON__", json.dumps(cfg))
        html = html.replace("__FX_MANIFEST_URL__",
                            json.dumps(cfg["manifestUrl"])[1:-1])
        html = html.replace("__FX_ICON_URL__",
                            json.dumps(cfg["iconUrl"])[1:-1])
        return html.encode("utf-8")

    def web_manifest(self, query: str) -> bytes:
        """Installable manifest for the fixture page itself. start_url
        carries the same client parameters so the installed web clip
        re-opens THIS client's fixture page (standalone)."""
        q = parse_qs(query)
        cid = q.get("id", ["?"])[0]
        role = q.get("role", ["leader"])[0]
        ws = q.get("ws", [""])[0]
        base = q.get("base", [""])[0]
        start = "/fixture?" + urlencode(
            {"id": cid, "role": role, "ws": ws, "base": base})
        manifest = {
            "name": f"fx-reader-sync-{role}",
            "short_name": f"fx-{role}",
            "start_url": start,
            "scope": "/",
            "display": "standalone",
            "background_color": "#111111",
            "theme_color": "#111111",
            "icons": [
                {"src": "/icon.png", "sizes": "180x180", "type": "image/png"},
                {"src": "/icon.png", "sizes": "512x512", "type": "image/png",
                 "purpose": "any"},
            ],
        }
        return json.dumps(manifest).encode("utf-8")


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
        if u.path == "/manifest.json":
            self._send(200, self.fx.web_manifest(u.query),
                       ctype="application/manifest+json")
            return
        if u.path == "/icon.png":
            self._send(200, _ICON_PNG, ctype="image/png")
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
