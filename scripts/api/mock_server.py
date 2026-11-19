#!/usr/bin/env python3
"""Stdlib REST mock serving api/dist/examples.json (#237).

Contract-first client dev without a real server:
    ./scripts/api/mock.sh --port 4010
    # PWA:  open http://localhost:8080/?api_base=http://localhost:4010
    # iOS:  xcrun simctl launch <udid> dev.bookq.reader -serverURL http://localhost:4010

Scenario switching (Prism-compatible subset):
    Prefer: example=<name>        # e.g. Prefer: example=empty
    ?__example=<name>             # query override (PWA-friendly)
    ?__status=<code>              # force a declared status (202/400/404/409/...)

No dependencies, no auth, CORS `*`. Binary media returns 1px placeholders.
Stateful bits (visibility owner/revision) live in memory so toggle UI works.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parents[2]

# 1px placeholders (dev-only; real bytes come from the server).
_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
_JPEG_1PX = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=")


def load_bundle(path: Path):
    bundle = json.loads(path.read_text(encoding="utf-8"))
    # (operationId, status, name) -> value ; plus ordered names per (op,status)
    table: dict[tuple, object] = {}
    names: dict[tuple, list] = {}
    for e in bundle.get("examples", []):
        key = (e["operationId"], str(e["status"]), e["name"])
        table[key] = e["value"]
        names.setdefault((e["operationId"], str(e["status"])), [])
        if e["name"] not in names[(e["operationId"], str(e["status"]))]:
            names[(e["operationId"], str(e["status"]))].append(e["name"])
    return table, names


class MockState:
    def __init__(self, table, names):
        self.table = table
        self.names = names
        tree = table.get(("getTree", "200", "default"), {})
        self.generation = tree.get("generation", 7) if isinstance(tree, dict) else 7
        vis = table.get(("getFolderVisibility", "200", "default"), {})
        self.vis_owner = vis.get("owner", "owner-abc123") if isinstance(vis, dict) else "owner-abc123"
        self.vis_revision = vis.get("revision", 4) if isinstance(vis, dict) else 4
        self.vis_hidden = list(vis.get("hiddenRoots", [])) if isinstance(vis, dict) else []


def pick(state: MockState, op: str, status: str, name: str | None):
    avail = state.names.get((op, status), [])
    if not avail:
        return None, None
    want = name or "default"
    if want not in avail:
        want = avail[0]
    return state.table[(op, status, want)], want


def error_example(state: MockState, op: str, status: str):
    """First declared example for (op,status) — shared $ref responses land here."""
    avail = state.names.get((op, status), [])
    if not avail:
        return {"detail": f"mock {status}"}
    return state.table[(op, status, avail[0])]


class Handler(BaseHTTPRequestHandler):
    state: MockState = None  # set in serve()
    server_version = "bookq-mock/1"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[mock] {self.command} {self.path} " + (fmt % args) + "\n")

    # -- helpers ------------------------------------------------------
    def _parsed(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        flat = {k: v[0] for k, v in qs.items()}
        prefer = self.headers.get("Prefer", "")
        m = re.search(r"example=([A-Za-z0-9_\-]+)", prefer)
        name = flat.get("__example") or (m.group(1) if m else None)
        force_status = flat.get("__status")
        return u.path, flat, name, force_status

    def _send_json(self, code: int, obj, extra: dict | None = None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_status(self, code: int, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _bytes(self, code: int, data: bytes, ctype: str):
        if self.headers.get("If-None-Match") == '"mock-etag"':
            return self._send_status(304)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", '"mock-etag"')
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _body_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*, Authorization, Prefer, X-Bookq-Visibility-Owner")
        self.end_headers()

    # -- routes --------------------------------------------------------
    def do_GET(self):
        path, q, name, force = self._parsed()
        st = self.state
        if path == "/":
            return self._send_json(200, {"mock": "bookq", "scenarios": "Prefer: example=<name> or ?__example=<name>, ?__status=<code>",
                                        "ws": "run scripts/api/mock.sh ws"})
        if path == "/api/v1/health":
            v, _ = pick(st, "getHealth", "200", name)
            return self._send_json(200, v)
        if path == "/api/v1/version":
            v, _ = pick(st, "getVersion", "200", name)
            return self._send_json(200, v)
        if path == "/api/v1/tree":
            if force and force != "200":
                return self._send_json(int(force), error_example(st, "getTree", force),
                                       {"Retry-After": "2"} if force == "202" else None)
            if q.get("since") is not None:
                try:
                    if int(q["since"]) == st.generation:
                        return self._send_status(304)
                except ValueError:
                    pass
            if q.get("depth") or name == "children":
                v, _ = pick(st, "getTree", "200", "children")
                return self._send_json(200, v, {"ETag": f'"mock-g{st.generation}"'})
            v, _ = pick(st, "getTree", "200", name)
            return self._send_json(200, v, {"ETag": f'"mock-g{st.generation}"'})
        if path == "/api/v1/images":
            folder = q.get("folder", "")
            if not folder:
                return self._send_json(400, error_example(st, "listImages", "400"))
            if folder in ("missing", "nope", "not-found"):
                return self._send_json(404, error_example(st, "listImages", "404"))
            if force and force != "200":
                return self._send_json(int(force), error_example(st, "listImages", force))
            if name == "empty" or folder.endswith("/empty"):
                v, _ = pick(st, "listImages", "200", "empty")
            else:
                v, _ = pick(st, "listImages", "200", None)
                v = dict(v, folder=folder) if isinstance(v, dict) else v
            return self._send_json(200, v)
        if path == "/api/v1/search":
            if force == "202":
                return self._send_json(202, error_example(st, "search", "202"), {"Retry-After": "2"})
            v, _ = pick(st, "search", "200", name)
            return self._send_json(200, v)
        if path == "/api/v1/facets":
            if force == "202":
                return self._send_json(202, error_example(st, "getFacets", "202"), {"Retry-After": "2"})
            v, _ = pick(st, "getFacets", "200", name)
            return self._send_json(200, v)
        if path == "/api/v1/reading-positions":
            if q.get("folder"):
                v, _ = pick(st, "listReadingPositions", "200", "single")
                return self._send_json(200, v)
            v, _ = pick(st, "listReadingPositions", "200", name)
            return self._send_json(200, v)
        if path == "/api/v1/folder-visibility":
            if name == "empty":
                return self._send_json(200, {"owner": st.vis_owner, "revision": st.vis_revision, "hiddenRoots": []})
            return self._send_json(200, {"owner": st.vis_owner, "revision": st.vis_revision,
                                         "hiddenRoots": st.vis_hidden})
        if path == "/api/v1/thumb-sprite":
            return self._bytes(200, _JPEG_1PX, "image/jpeg")
        if path.startswith("/thumb/") or path.startswith("/view/"):
            return self._bytes(200, _JPEG_1PX, "image/jpeg")
        if path.startswith("/image/"):
            rng = self.headers.get("Range")
            if rng and not rng.startswith("bytes="):
                return self._send_json(400, {"detail": "bad range"})
            inm = self.headers.get("If-None-Match")
            if inm == '"mock-etag"':
                return self._send_status(304)
            if rng:
                data = _JPEG_1PX
                self.send_response(206)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Range", f"bytes 0-0/{len(data)}")
                self.send_header("Content-Length", "1")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                return self.wfile.write(data[:1])
            return self._bytes(200, _JPEG_1PX, "image/jpeg")
        # v0 aliases
        if path == "/api/health":
            v, _ = pick(st, "getHealth", "200", name)
            return self._send_json(200, v)
        if path == "/api/folders":
            v, _ = pick(st, "getTree", "200", name if name in ("default", "children") else None)
            return self._send_json(200, v)
        if path == "/api/images":
            v, _ = pick(st, "listImages", "200", None)
            imgs = v.get("images", []) if isinstance(v, dict) else []
            return self._send_json(200, imgs)
        if path == "/api/search":
            v, _ = pick(st, "search", "200", name)
            return self._send_json(200, v)
        if path == "/api/metadata/facets":
            v, _ = pick(st, "getFacets", "200", name)
            return self._send_json(200, v)
        if path == "/api/folder-visibility":
            return self._send_json(200, {"owner": st.vis_owner, "revision": st.vis_revision,
                                         "hiddenRoots": st.vis_hidden})
        return self._send_json(404, {"detail": f"mock: no route GET {path}"})

    def do_POST(self):
        path, q, name, force = self._parsed()
        st = self.state
        if path == "/api/v1/index/refresh":
            v, _ = pick(st, "refreshIndex", "200", name)
            return self._send_json(200, v)
        if path == "/api/v1/auth/token":
            body = self._body_json()
            if not body.get("grant_type"):
                return self._send_json(400, error_example(st, "exchangeToken", "400"))
            v, _ = pick(st, "exchangeToken", "200", name)
            return self._send_json(200, v)
        if path == "/api/refresh":
            v, _ = pick(st, "refreshIndex", "200", name)
            return self._send_json(200, v)
        return self._send_json(404, {"detail": f"mock: no route POST {path}"})

    def do_PUT(self):
        path, q, name, force = self._parsed()
        st = self.state
        if path == "/api/v1/reading-positions":
            if name == "stale" or force == "stale":
                v, _ = pick(st, "putReadingPosition", "200", "stale")
                return self._send_json(200, v)
            v, _ = pick(st, "putReadingPosition", "200", "ok")
            return self._send_json(200, v)
        if path == "/api/v1/folder-visibility":
            body = self._body_json()
            owner = self.headers.get("X-Bookq-Visibility-Owner", "")
            if not body or "root" not in body or "hidden" not in body:
                return self._send_json(400, error_example(st, "putFolderVisibility", "400"))
            if name == "conflict" or force == "409" or (owner and owner != st.vis_owner):
                st.vis_revision += 1
                return self._send_json(409, error_example(st, "putFolderVisibility", "409"))
            root, hidden = body["root"], body["hidden"]
            if hidden and root not in st.vis_hidden:
                st.vis_hidden.append(root)
            if not hidden and root in st.vis_hidden:
                st.vis_hidden.remove(root)
            st.vis_revision += 1
            st.vis_owner = f"owner-mock{st.vis_revision}"
            return self._send_json(200, {"owner": st.vis_owner, "revision": st.vis_revision,
                                         "hiddenRoots": st.vis_hidden})
        if path == "/api/folder-visibility":
            return self.do_PUT.__wrapped__(self) if hasattr(self.do_PUT, "__wrapped__") else self._send_json(404, {"detail": "mock: use v1"})
        return self._send_json(404, {"detail": f"mock: no route PUT {path}"})

    def do_DELETE(self):
        path, q, name, force = self._parsed()
        st = self.state
        if path == "/api/v1/reading-positions":
            v, _ = pick(st, "deleteReadingPosition", "200", name)
            return self._send_json(200, v)
        return self._send_json(404, {"detail": f"mock: no route DELETE {path}"})


def serve(port: int, bundle_path: Path):
    table, names = load_bundle(bundle_path)
    Handler.state = MockState(table, names)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[mock] bookq REST mock on http://127.0.0.1:{port} "
          f"({len(table)} examples from {bundle_path.name})")
    print("[mock] scenarios: Prefer: example=<name> | ?__example=<name> | ?__status=<code>")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=4010)
    ap.add_argument("--bundle", default="api/dist/examples.json")
    args = ap.parse_args()
    bundle = Path(args.bundle)
    if not bundle.is_absolute():
        bundle = REPO / bundle
    if not bundle.exists():
        print(f"[mock] bundle {bundle} missing — run scripts/api/extract-examples.py first",
              file=sys.stderr)
        return 2
    serve(args.port, bundle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
