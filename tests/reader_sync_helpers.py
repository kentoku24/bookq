"""S10 (#85) — helpers for tests of the reader-sync verification runner.

Imports the runner modules (they live outside the package tree) and
provides small builders used across test_reader_verify.py. Everything
here is stdlib + the runner's own pure core — no Playwright, no simctl.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_DIR = REPO_ROOT / "scripts" / "reader-sync"

for p in (str(REPO_ROOT), str(RUNNER_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


def runner_module(name: str):
    """Import a scripts/reader-sync module (verify_core, verify, ...)."""
    return importlib.import_module(name)


def mini_result(**over) -> dict:
    """Smallest result JSON that satisfies verify_core.result_required_ok."""
    r = {
        "run_id": "t-run",
        "driver": "chrome",
        "build_sha": "deadbeef",
        "devices": {"leader": {}, "follower": {}},
        "os": {"system": "TestOS"},
        "scenarios": [
            {"name": "s1", "transport": "server-ws", "status": "pass",
             "assertions": [{"name": "a1", "ok": True, "detail": ""}]},
        ],
    }
    r.update(over)
    return r


def fx_control_client(server, client_id: str):
    """Tiny in-process stand-in for fixture.html's control loop — used to
    exercise FixtureServer's cmd/result plumbing without a browser."""
    import json
    import urllib.request

    base = server.base_url()

    def post(path, obj):
        req = urllib.request.Request(
            base + path, data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"{}")

    def get(path):
        with urllib.request.urlopen(base + path, timeout=30) as r:
            return json.loads(r.read() or b"{}")

    post("/ctrl/hello", {"id": client_id, "ua": "unit-test", "mods": True})

    def pump_once(handler):
        cmd = get(f"/ctrl/next?id={client_id}")
        if not cmd.get("seq"):
            return None
        try:
            data = handler(cmd["cmd"], cmd.get("params") or {})
            res = {"ok": True, "data": data}
        except Exception as e:
            res = {"ok": False, "error": str(e)}
        post("/ctrl/result", {"id": client_id, "seq": cmd["seq"], "res": res})
        return cmd["cmd"]

    return type("FxCtl", (), {"post": post, "get": get,
                              "pump_once": pump_once})
