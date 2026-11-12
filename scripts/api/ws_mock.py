#!/usr/bin/env python3
"""Script WS mock replaying api/reader-sync-cases.json (#237, #210 companion).

The REST mock (mock_server.py) covers HTTP; this covers `/ws/reader-sync`
for dual-reader UI dev without a real server:

    python3 scripts/api/ws_mock.py list            # golden cases (#210)
    python3 scripts/api/ws_mock.py show <case-id>  # print frames
    python3 scripts/api/ws_mock.py serve --port 4011
    # PWA/iOS then open a WS to ws://127.0.0.1:4011/ws/reader-sync

serve implements a minimal RFC6455 text-frame server (stdlib only):
HELLO on connect, then canned CREATE/JOIN/APPROVE/PING handling with
deterministic ids (sess-mock-01, code 123456). Full golden-frame replay
stays in scripts/reader-sync/verify*.py — this mock is for UI wiring,
not protocol conformance.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import socket
import struct
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CASES = REPO / "api" / "reader-sync-cases.json"

SESSION = "sess-mock-01"
CODE = "123456"


def load_cases():
    return json.loads(CASES.read_text(encoding="utf-8"))


def cmd_list() -> int:
    data = load_cases()
    for c in data.get("cases", []):
        print(f"{c['id']}\t{c.get('title', '')}")
    return 0


def cmd_show(case_id: str) -> int:
    data = load_cases()
    for c in data.get("cases", []):
        if c["id"] == case_id:
            print(json.dumps(c, ensure_ascii=False, indent=2))
            return 0
    print(f"unknown case {case_id!r}", file=sys.stderr)
    return 2


# -- minimal WS framing (text frames only) ------------------------------
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _recv_frame(conn: socket.socket):
    hdr = b""
    while len(hdr) < 2:
        chunk = conn.recv(2 - len(hdr))
        if not chunk:
            return None, None
        hdr += chunk
    b1, b2 = hdr
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack(">H", _recvn(conn, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _recvn(conn, 8))[0]
    mask = _recvn(conn, 4) if masked else None
    payload = _recvn(conn, length) if length else b""
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def _recvn(conn: socket.socket, n: int) -> bytes:
    out = b""
    while len(out) < n:
        chunk = conn.recv(n - len(out))
        if not chunk:
            raise ConnectionError("closed")
        out += chunk
    return out


def _send_text(conn: socket.socket, obj) -> None:
    raw = json.dumps(obj, ensure_ascii=False).encode()
    hdr = bytes([0x81])
    if len(raw) < 126:
        hdr += bytes([len(raw)])
    elif len(raw) < 65536:
        hdr += bytes([126]) + struct.pack(">H", len(raw))
    else:
        hdr += bytes([127]) + struct.pack(">Q", len(raw))
    conn.sendall(hdr + raw)


def _handle(conn: socket.socket):
    try:
        req = b""
        while b"\r\n\r\n" not in req:
            chunk = conn.recv(4096)
            if not chunk:
                return
            req += chunk
        head = req.decode("latin1")
        if "/ws/reader-sync" not in head.split("\r\n", 1)[0]:
            conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            return
        key = ""
        for line in head.split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\n"
                      "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        _send_text(conn, {"v": 1, "type": "HELLO", "link": "L-mock-1"})
        members = {}
        while True:
            opcode, payload = _recv_frame(conn)
            if opcode is None or opcode == 0x8:
                return
            if opcode == 0x9:  # ping
                conn.sendall(bytes([0x8A, 0x00]))
                continue
            if opcode != 0x1:
                continue
            try:
                msg = json.loads(payload.decode())
            except Exception:
                _send_text(conn, {"v": 1, "type": "ERROR", "code": "ERR_MALFORMED"})
                continue
            rtype, rid = msg.get("type"), msg.get("requestId", "r0")
            if rtype == "PING":
                _send_text(conn, {"v": 1, "type": "PONG",
                                  "requestId": rid, "seq": (msg.get("payload") or {}).get("seq", 0)})
            elif rtype == "CREATE":
                members["leader"] = {"role": "leader", "status": "active", "name": "Mock", "ready": False}
                _send_text(conn, {"v": 1, "type": "ACK", "requestId": rid, "request": "CREATE",
                                  "sessionId": SESSION,
                                  "data": {"sessionId": SESSION, "code": CODE, "memberId": "m-mock-leader",
                                           "role": "leader", "resumeToken": "tok-mock-leader", "expiresInSec": 300}})
                _send_text(conn, {"v": 1, "type": "STATE", "sessionId": SESSION, "revision": 1,
                                  "workEpoch": 0, "manifestVersion": 0, "phase": "waiting",
                                  "spreadId": None, "offset": 0, "leaderSide": "left",
                                  "dataPlane": "server-ws", "presence": {"m-mock-leader": members["leader"]}})
            elif rtype == "JOIN":
                _send_text(conn, {"v": 1, "type": "ACK", "requestId": rid, "request": "JOIN",
                                  "sessionId": SESSION,
                                  "data": {"sessionId": SESSION, "memberId": "m-mock-follower",
                                           "role": "follower", "resumeToken": "tok-mock-follower",
                                           "leaderName": "Mock"}})
                _send_text(conn, {"v": 1, "type": "JOIN_REQUEST", "sessionId": SESSION,
                                  "memberId": "m-mock-follower", "name": "Follower"})
            elif rtype in ("APPROVE", "REJECT"):
                ok = rtype == "APPROVE"
                _send_text(conn, {"v": 1, "type": "ACK", "requestId": rid, "request": rtype,
                                  "sessionId": SESSION, "data": {}})
                if ok:
                    _send_text(conn, {"v": 1, "type": "STATE", "sessionId": SESSION, "revision": 2,
                                      "workEpoch": 0, "manifestVersion": 0, "phase": "paired",
                                      "spreadId": None, "offset": 0, "leaderSide": "left",
                                      "dataPlane": "server-ws",
                                      "presence": {"m-mock-leader": {"role": "leader", "status": "active"},
                                                 "m-mock-follower": {"role": "follower", "status": "active"}}})
            else:
                _send_text(conn, {"v": 1, "type": "ACK", "requestId": rid,
                                  "request": rtype, "sessionId": SESSION, "data": {}})
    except (ConnectionError, OSError):
        return


def cmd_serve(port: int) -> int:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(16)
    print(f"[ws-mock] bookq WS mock on ws://127.0.0.1:{port}/ws/reader-sync "
          f"(cases: {CASES.name}) — Ctrl-C to stop")
    try:
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=_handle, args=(conn,), daemon=True).start()
    except KeyboardInterrupt:
        pass
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    show = sub.add_parser("show")
    show.add_argument("case_id")
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=4011)
    args = ap.parse_args()
    if args.cmd == "list":
        return cmd_list()
    if args.cmd == "show":
        return cmd_show(args.case_id)
    return cmd_serve(args.port)


if __name__ == "__main__":
    raise SystemExit(main())
