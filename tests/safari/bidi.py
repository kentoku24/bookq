"""WebDriver BiDi channel for real-realm script evaluation.

Classic WebDriver ``execute_script`` runs in safaridriver's isolated
automation world: reads fall through to the real page (DOM, globals,
cookies) but writes stay in the automation realm — ``localStorage``
mutations roll back as soon as the call returns, and globals set by test
init scripts are invisible to the app. ``script.evaluate`` over BiDi runs
in the page's real realm, so storage persists and test-injected globals
are app-visible. ``awaitPromise`` also resolves natively, replacing the
window-side promise parking the classic path needs.

Element arguments/returns are unsupported (safaridriver serializes DOM
nodes as empty objects) — callers keep those on classic execute_script.
"""

from __future__ import annotations

import json
import threading
import time

import websocket
from selenium.common.exceptions import WebDriverException


def _to_bidi(v):
    if v is None:
        return {"type": "undefined"}
    if isinstance(v, bool):
        return {"type": "boolean", "value": v}
    if isinstance(v, (int, float)):
        if v != v:
            return {"type": "number", "value": "NaN"}
        if v == float("inf"):
            return {"type": "number", "value": "+Infinity"}
        if v == float("-inf"):
            return {"type": "number", "value": "-Infinity"}
        return {"type": "number", "value": v}
    if isinstance(v, str):
        return {"type": "string", "value": v}
    if isinstance(v, (list, tuple)):
        return {"type": "array", "value": [_to_bidi(x) for x in v]}
    if isinstance(v, dict):
        return {
            "type": "object",
            "value": [[k, _to_bidi(x)] for k, x in v.items()],
        }
    return {"type": "undefined"}


def _from_bidi(rob):
    if not isinstance(rob, dict):
        return rob
    t = rob.get("type")
    if t in ("undefined", "null"):
        return None
    if t in ("boolean", "number", "bigint", "string"):
        return rob.get("value")
    if t == "array":
        return [_from_bidi(x) for x in rob.get("value") or []]
    if t in ("object", "map"):
        out = {}
        for pair in rob.get("value") or []:
            k, v = pair[0], _from_bidi(pair[1])
            out[k] = v
        return out
    # function/node/symbol/window/handles — not serializable
    return None


class BidiTransportError(WebDriverException):
    """The BiDi socket itself failed (vs an in-page script exception)."""


class BidiChannel:
    """One BiDi websocket per WebDriver session."""

    def __init__(self, ws_url: str):
        self._ws = websocket.create_connection(ws_url, timeout=15)
        self._lock = threading.Lock()
        self._next_id = 0

    @classmethod
    def connect(cls, capabilities: dict) -> "BidiChannel | None":
        url = capabilities.get("webSocketUrl")
        if not url:
            return None
        try:
            return cls(url)
        except Exception:
            return None

    def _rpc(self, method: str, params: dict):
        with self._lock:
            self._next_id += 1
            mid = self._next_id
            self._ws.send(json.dumps({"id": mid, "method": method, "params": params}))
            deadline = time.time() + 30
            while time.time() < deadline:
                try:
                    msg = json.loads(self._ws.recv())
                except Exception as e:
                    raise BidiTransportError(f"BiDi transport error: {e}")
                if msg.get("id") != mid:
                    continue  # events or interleaved responses — skip
                if msg.get("type") == "error" or "error" in msg:
                    raise BidiTransportError(
                        f"BiDi {method} failed: {msg.get('error')} {msg.get('message')}"
                    )
                return msg.get("result", {})
        raise BidiTransportError(f"BiDi {method} timed out")

    def evaluate(self, context: str, expression: str, arguments=None, await_promise=False):
        params = {
            "expression": expression,
            "target": {"context": context},
            "awaitPromise": bool(await_promise),
        }
        if arguments:
            params["arguments"] = [_to_bidi(a) for a in arguments]
        res = self._rpc("script.evaluate", params)
        if res.get("type") == "exception":
            det = res.get("exceptionDetails") or {}
            exc = (det.get("exception") or {}).get("value") or {}
            msg = exc.get("message") or det.get("text") or "evaluation failed"
            raise WebDriverException(msg)
        return _from_bidi((res.get("result") or {}))

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass
