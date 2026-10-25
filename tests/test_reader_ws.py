"""S03 — /ws/reader-sync end-to-end over real WebSocket connections (#78).

Two independent WS clients (websockets sync client) against live uvicorn
servers spawned as subprocesses — one in BOOKQ_TEST_MODE (mock auth) and one
with real signed-cookie auth (SECRET_KEY set, TEST_MODE unset), where two
DIFFERENT emails log in via forged session cookies. Nothing here weakens or
bypasses the auth checks.
"""
import base64
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from websockets.exceptions import InvalidHandshake
from websockets.sync.client import connect as ws_connect

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.reader_sync import protocol  # noqa: E402

_WS_PATH = "/ws/reader-sync"
AUTH_SECRET = "reader-sync-test-secret"
ALICE = {"email": "alice@example.com", "name": "Alice", "picture": ""}
BOB = {"email": "bob@example.com", "name": "Bob", "picture": ""}


def _spawn_server(downloads_tree: Path, extra_env: dict) -> tuple:
    """Same shape as conftest's server fixture but with a custom env."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = os.environ.copy()
    env.pop("BOOKQ_TEST_MODE", None)
    env["DOWNLOADS_DIR"] = str(downloads_tree)
    env.update(extra_env)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        env=env, cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("auth test server exited early")
        try:
            with urllib.request.urlopen(base_url + "/api/health", timeout=1):
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.terminate()
        raise RuntimeError("auth test server did not become healthy in 10s")
    return proc, base_url


@pytest.fixture(scope="module")
def auth_server(downloads_tree):
    """Live server WITHOUT test-mode: real signed-cookie session auth."""
    proc, base_url = _spawn_server(downloads_tree, {
        "SECRET_KEY": AUTH_SECRET,
        "ALLOWED_EMAILS": f"{ALICE['email']},{BOB['email']}",
    })
    yield base_url
    proc.terminate()
    proc.wait(timeout=5)


def _session_cookie(user: dict, secret: str = AUTH_SECRET) -> str:
    """Forge the exact cookie starlette SessionMiddleware emits."""
    import itsdangerous

    signer = itsdangerous.TimestampSigner(secret)
    data = base64.b64encode(json.dumps({"user": user}).encode("utf-8"))
    return signer.sign(data).decode("utf-8")


def _ws(base_url: str, user: dict | None = None):
    headers = {}
    if user is not None:
        headers["Cookie"] = f"session={_session_cookie(user)}"
    return ws_connect(base_url.replace("http", "ws") + _WS_PATH,
                      additional_headers=headers)


def _recv(ws, timeout: float = 5.0) -> dict:
    raw = ws.recv(timeout=timeout)
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    return json.loads(raw)


def _request(rtype: str, payload=None, *, session_id=None, request_id="r1") -> str:
    return json.dumps({
        "v": protocol.PROTOCOL_VERSION,
        "requestId": request_id,
        "sessionId": session_id,
        "workEpoch": 0,
        "baseRevision": 0,
        "type": rtype,
        "payload": payload or {},
    })


def _expect(ws, rtype: str, timeout: float = 5.0) -> dict:
    """Frames may interleave (PING/STATE); skip until the wanted type arrives."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        f = _recv(ws, timeout=max(0.1, deadline - time.time()))
        if f["type"] == rtype:
            return f
    raise AssertionError(f"no {rtype} frame within {timeout}s")


def _full_pair(ws_a, ws_b):
    """CREATE+JOIN+APPROVE; returns (session_id, code, leader_member, follower_member)."""
    ws_a.send(_request("CREATE", request_id="c"))
    ack = _expect(ws_a, "ACK")
    data = ack["data"]
    ws_b.send(_request("JOIN", {"code": data["code"]}, request_id="j"))
    jdata = _expect(ws_b, "ACK")["data"]
    jr = _expect(ws_a, "JOIN_REQUEST")
    assert jr["memberId"] == jdata["memberId"]
    ws_a.send(_request("APPROVE", {"memberId": jdata["memberId"]},
                       session_id=data["sessionId"], request_id="a"))
    sa = _expect(ws_a, "STATE")
    sb = _expect(ws_b, "STATE")
    assert len(sa["presence"]) == 2 and len(sb["presence"]) == 2
    return data["sessionId"], data["code"], data["memberId"], jdata["memberId"]


# ── hello -> ping -> pong -> close ─────────────────────────────────


def test_hello_ping_pong_close(server):
    """The scripted probe flow every transport check relies on."""
    with _ws(server) as ws:
        hello = _recv(ws)
        assert hello["type"] == "HELLO" and hello["v"] == protocol.PROTOCOL_VERSION
        ws.send(_request("PING", {"seq": 1}))
        pong = _expect(ws, "PONG")
        assert pong["seq"] == 1


# ── pairing over real sockets (test-mode auth) ─────────────────────


def test_two_clients_full_pairing_sequence(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        assert _recv(ws_a)["type"] == "HELLO"
        assert _recv(ws_b)["type"] == "HELLO"
        sid, code, leader_mid, follower_mid = _full_pair(ws_a, ws_b)
        assert sid and code.isdigit() and leader_mid != follower_mid


def test_join_wrong_code_rejected(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        ws_a.send(_request("CREATE", request_id="c"))
        ack = _expect(ws_a, "ACK")
        ws_b.send(_request("JOIN", {"code": "000000"}, request_id="j"))
        rej = _expect(ws_b, "REJECT")
        assert rej["reason"] == "bad_code"
        # leader side untouched: session still waiting, members == 1
        st = _expect(ws_a, "STATE")
        assert len(st["presence"]) == 1


def test_signal_relay_over_ws(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, *_ = _full_pair(ws_a, ws_b)
        ws_a.send(_request("SIGNAL", {"kind": "offer", "data": {"sdp": "x"}},
                           session_id=sid, request_id="s"))
        sig = _expect(ws_b, "SIGNAL")
        assert sig["kind"] == "offer" and sig["from"] == "leader"
        assert sig["data"] == {"sdp": "x"}


def test_ui_sync_relay_over_ws(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, *_ = _full_pair(ws_a, ws_b)
        payload = {"view": "browse", "path": "group", "top": 42,
                   "viewH": 700, "mode": "tile"}
        ws_a.send(_request("UI_SYNC", payload, session_id=sid, request_id="u"))
        ui = _expect(ws_b, "UI_SYNC")
        assert ui["from"] == "leader" and ui["payload"] == payload
        # scroll bursts must not wedge the link — heartbeat still answers
        ws_a.send(_request("PING", {"seq": 9}))
        assert _expect(ws_a, "PONG")["seq"] == 9


def test_oversized_frame_closed(server):
    with _ws(server) as ws:
        _recv(ws)
        ws.send("x" * (protocol.MAX_FRAME_BYTES + 1))
        # server must close: recv raises ConnectionClosed; the close code is
        # the explicit too-big code.
        with pytest.raises(Exception) as ei:
            while True:
                _recv(ws, timeout=3)
        assert "1009" in str(ei.value)


def test_malformed_request_rejected_not_closed(server):
    """A request frame that fails full validation earns a typed REJECT —
    not a 1011 crash. Regression for _reject() indexing req["requestId"]
    when the caller only had the requestId fragment (S11 raw probes hit
    this and the link died with code 1011)."""
    with _ws(server) as ws:
        _recv(ws)
        # requestId/type present but the rest of the request envelope is
        # missing — parse_request raises, _reject must still echo both.
        ws.send(json.dumps({"v": protocol.PROTOCOL_VERSION,
                            "requestId": "bad1", "type": "RESUME"}))
        rej = _expect(ws, "REJECT")
        assert rej["reason"] == "malformed"
        assert rej["requestId"] == "bad1"
        assert rej["request"] == "RESUME"
        # the link survived: heartbeat still answers after the reject.
        ws.send(_request("PING", {"seq": 7}))
        assert _expect(ws, "PONG")["seq"] == 7


def test_unparseable_frame_rejected_not_closed(server):
    """A frame that isn't even a request-shaped dict also gets a typed
    REJECT with null echoes, and the link stays usable."""
    with _ws(server) as ws:
        _recv(ws)
        ws.send(json.dumps([1, 2, 3]))
        rej = _expect(ws, "REJECT")
        assert rej["reason"] == "malformed"
        assert rej["requestId"] is None and rej["request"] is None
        ws.send(_request("PING", {"seq": 8}))
        assert _expect(ws, "PONG")["seq"] == 8


# ── real signed-cookie auth (no test-mode bypass) ──────────────────


def test_ws_rejected_without_session_cookie(auth_server):
    with pytest.raises(InvalidHandshake) as ei:
        _ws(auth_server)
    assert "403" in str(ei.value)


def test_ws_rejected_with_forged_cookie(auth_server):
    bad = _session_cookie(ALICE, secret="wrong-secret")
    with pytest.raises(InvalidHandshake) as ei:
        ws_connect(auth_server.replace("http", "ws") + _WS_PATH,
                   additional_headers={"Cookie": f"session={bad}"})
    assert "403" in str(ei.value)


def test_two_different_logged_in_emails_can_pair(auth_server):
    """alice + bob — distinct real sessions, same pairing frame sequence."""
    with _ws(auth_server, ALICE) as ws_a, _ws(auth_server, BOB) as ws_b:
        assert _recv(ws_a)["type"] == "HELLO"
        assert _recv(ws_b)["type"] == "HELLO"
        sid, code, leader_mid, follower_mid = _full_pair(ws_a, ws_b)
        assert sid and len(code) == 6
        # and the pair survives a signal exchange + explicit leave
        ws_b.send(_request("LEAVE", session_id=sid, request_id="l"))
        assert _expect(ws_a, "END")["reason"] == "left"
        assert _expect(ws_b, "END")["reason"] == "left"
