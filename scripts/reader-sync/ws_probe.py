#!/usr/bin/env python3
"""reader-sync WS probe (#78/S03): hello -> ping -> pong -> close transcript.

Proves the /ws/reader-sync path end-to-end through a given base URL
(tailscale serve https URL, LAN http URL, or local test server). Prints a
machine-readable transcript; never logs secrets (there are none — dev/test
envs run BOOKQ_TEST_MODE so no cookie is needed).

Usage:
    uv run --with-requirements requirements-dev.txt --with websockets \
        python scripts/reader-sync/ws_probe.py ws://127.0.0.1:8080
    uv run --with-requirements requirements-dev.txt --with websockets \
        python scripts/reader-sync/ws_probe.py wss://dev-03.example.ts.net

Exit 0 when the full hello->ping->pong->close sequence completes; 1 with the
failure printed otherwise.
"""
from __future__ import annotations

import json
import sys
import time

from websockets.sync.client import connect

PATH = "/ws/reader-sync"


def _recv(ws, timeout: float = 10.0):
    raw = ws.recv(timeout=timeout)
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    return json.loads(raw)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: ws_probe.py <base-url>   (ws:// or wss://, no path)", file=sys.stderr)
        return 2
    base = sys.argv[1].rstrip("/")
    url = base + PATH
    t0 = time.time()

    def log(event: str, **kv):
        print(f"{time.time() - t0:7.3f}s  {event:<10} {json.dumps(kv, ensure_ascii=False)}")

    try:
        log("connect", url=url)
        with connect(url) as ws:
            hello = _recv(ws)
            log("hello", type=hello.get("type"), v=hello.get("v"))
            assert hello.get("type") == "HELLO", hello

            ping = {"v": 1, "requestId": "probe-1", "sessionId": None,
                    "workEpoch": 0, "baseRevision": 0, "type": "PING",
                    "payload": {"seq": 1}}
            ws.send(json.dumps(ping))
            log("ping", type="PING", seq=1)

            deadline = time.time() + 10
            while True:
                f = _recv(ws, timeout=max(0.1, deadline - time.time()))
                log("recv", type=f.get("type"))
                if f.get("type") == "PONG":
                    assert f.get("seq") == 1, f
                    break
            ws.close()
            log("close", ok=True)
    except Exception as exc:  # noqa: BLE001 — probe reports any failure
        log("error", kind=type(exc).__name__, detail=str(exc)[:200])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
