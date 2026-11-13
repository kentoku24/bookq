"""Spec-example fixtures for client-only PWA tests (#237).

``api/dist/examples.json`` (built by scripts/api/extract-examples.py) is the
single fixture source. ``install_spec_routes`` answers every ``/api/v1/*``
(and media) request from that bundle, so the test below proves the UI runs
on spec examples alone — no server implementation involved.

Scenario switching mirrors scripts/api/mock_server.py:
``scenario={"tree": "children"}`` picks a named example per operation,
``status_overrides={"tree": 202}`` forces a declared status. A scenario
value may also be a per-hit list like ``{"getTree": [(202, "index_building"),
(200, "default")]}`` to replay edge-case sequences (202 index-building
then recovery) client-side.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
BUNDLE_PATH = REPO_ROOT / "api" / "dist" / "examples.json"

# 1px PNG placeholder for /image /thumb /view /thumb-sprite (dev-only).
_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

_bundle_cache = None


def bundle():
    global _bundle_cache
    if _bundle_cache is None:
        _bundle_cache = json.loads(BUNDLE_PATH.read_text(encoding="utf-8"))
    return _bundle_cache


def example(operation_id: str, status: str = "200", name: str = "default"):
    """First bundle value matching (operation_id, status, name)."""
    for e in bundle()["examples"]:
        if e["operationId"] == operation_id and str(e["status"]) == str(status) and e["name"] == name:
            return e["value"]
    raise KeyError(f"no spec example {operation_id} {status} [{name}]")


def example_names(operation_id: str, status: str = "200"):
    return [e["name"] for e in bundle()["examples"]
            if e["operationId"] == operation_id and str(e["status"]) == str(status)]


# request path prefix -> (operationId, default example name)
_ROUTES = [
    ("/api/v1/tree", "getTree", "default"),
    ("/api/v1/images", "listImages", "default"),
    ("/api/v1/search", "search", "default"),
    ("/api/v1/facets", "getFacets", "default"),
    ("/api/v1/index/refresh", "refreshIndex", "default"),
    ("/api/v1/reading-positions", None, "default"),  # method-dependent
    ("/api/v1/folder-visibility", None, "default"),  # method-dependent
    ("/api/v1/auth/token", "exchangeToken", "default"),
    ("/api/v1/version", "getVersion", "default"),
    ("/api/v1/health", "getHealth", "default"),
]


def _op_for(method: str, path: str):
    if path.startswith("/api/v1/reading-positions"):
        return {"GET": "listReadingPositions", "PUT": "putReadingPosition",
                "DELETE": "deleteReadingPosition"}.get(method, "listReadingPositions")
    if path.startswith("/api/v1/folder-visibility"):
        return "getFolderVisibility" if method == "GET" else "putFolderVisibility"
    for prefix, op, _ in _ROUTES:
        if path.startswith(prefix) and op is not None:
            return op
    return None


def install_spec_routes(page, scenario=None, status_overrides=None):
    """Route all API + media traffic to spec examples. Returns hit counter."""
    scenario = scenario or {}
    status_overrides = status_overrides or {}
    hits: dict = {}

    def handler(route):
        req = route.request
        u = urlparse(req.url)
        path, method = u.path, req.method
        # `**/api/**` also matches static assets under /static/api/ (the
        # generated endpoints + typed client). Those are shell assets, not
        # data — pass them through to the real server.
        if path.startswith("/static/"):
            return route.continue_()
        hits[path] = hits.get(path, 0) + 1
        qs = parse_qs(u.query)

        # Binary media: 1px placeholder (mock_server.py parity).
        if (path.startswith("/image/") or path.startswith("/thumb/")
                or path.startswith("/view/") or path == "/api/v1/thumb-sprite"):
            route.fulfill(status=200, content_type="image/png", body=_PNG_1PX)
            return

        op = _op_for(method, path)
        if op is None:
            route.fulfill(status=404, content_type="application/json",
                          body=json.dumps({"detail": f"no spec route {path}"}))
            return

        # depth=1 tree fetch is the shallow slot: it never consumes the
        # full-tree sequence (real-server parity: children example).
        is_shallow = op == "getTree" and bool(qs.get("depth"))
        slot = op + (":shallow" if is_shallow else "")

        def _str(key):
            v = scenario.get(key)
            return v if isinstance(v, str) else None

        # Per-hit sequence e.g. [(202, "index_building"), (200, "default")].
        seq_name, seq_status = None, None
        seq_keys = (slot, op, path) if not is_shallow else (slot,)
        for key in seq_keys:
            seq = scenario.get(key)
            if isinstance(seq, list) and seq:
                seq_status, seq_name = seq.pop(0)
                break

        name = (qs.get("__example", [None])[0] or seq_name
                or _str(slot) or _str(path) or _str(op)
                or _str("default"))
        if is_shallow and not seq_name and not qs.get("__example") and name is None:
            name = _str("shallow") or "children"
        # ?folder= narrows to the single-record example (server parity).
        if (op == "listReadingPositions" and qs.get("folder")
                and not seq_name and not qs.get("__example") and name is None):
            name = "single"

        # since=generation -> 304 parity with the real server.
        if op == "getTree" and method == "GET" and qs.get("since"):
            try:
                gen = example("getTree", "200", "default")["generation"]
                if int(qs["since"][0]) == gen:
                    route.fulfill(status=304, body="")
                    return
            except (KeyError, ValueError):
                pass

        status = str(seq_status or status_overrides.get(path)
                     or status_overrides.get(op)
                     or ("202" if (qs.get("__status", [None])[0] == "202") else "200"))
        names = example_names(op, status)
        pick = name if name in names else (names[0] if names else None)
        if pick is None:
            route.fulfill(status=404, content_type="application/json",
                          body=json.dumps({"detail": f"no spec example {op} {status}"}))
            return
        headers = {"Access-Control-Allow-Origin": "*"}
        if status == "202":
            headers["Retry-After"] = "0"
        route.fulfill(status=int(status), content_type="application/json",
                      headers=headers,
                      body=json.dumps(example(op, status, pick), ensure_ascii=False))

    page.route("**/api/**", handler)
    page.route("**/image/**", handler)
    page.route("**/thumb/**", handler)
    page.route("**/view/**", handler)
    return hits
