"""S03 — pairing/session registry over the live server (#78, #485 Phase 2c port).

Two real reader-sync sockets against the session server fixture: the
wire-visible pairing contract — CREATE/JOIN/APPROVE handshake, code errors,
pending lifecycle, SIGNAL/UI_SYNC relay, MATCH one-tap pairing, RESUME and
transport switching.

The in-process registry unit matrix (fake clock expiry windows, queue
bounds, logging hygiene) stays in test_reader_pairing_internals.py, which
retires with the Python backend. Wire constants and reason strings are
inlined from the asyncapi contract instead of the Python package so this
file runs in both lanes (uvicorn + BOOKQ_TEST_SERVER=rs).
"""
import json
import time

import pytest
from websockets.sync.client import connect as ws_connect

# Wire constants inlined from the asyncapi contract (RequestFrame.v const 1,
# 64KiB max frame) — importing the Python package here would quarantine
# this file under BOOKQ_TEST_SERVER=rs.
PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 64 * 1024

_WS_PATH = "/ws/reader-sync"


def _ws(base_url: str):
    return ws_connect(base_url.replace("http", "ws") + _WS_PATH)


def _recv(ws, timeout: float = 10.0) -> dict:
    raw = ws.recv(timeout=timeout)
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    return json.loads(raw)


def _req(rtype, payload=None, *, sid=None, rid="r1", epoch=0, base=0) -> str:
    return json.dumps({
        "v": PROTOCOL_VERSION,
        "requestId": rid,
        "sessionId": sid,
        "workEpoch": epoch,
        "baseRevision": base,
        "type": rtype,
        "payload": payload or {},
    })


def _drain_until(ws, pred, timeout: float = 15.0):
    deadline = time.time() + timeout
    seen = []
    while time.time() < deadline:
        f = _recv(ws, timeout=max(0.1, deadline - time.time()))
        seen.append(f)
        if pred(f):
            return f, seen
    raise AssertionError("no matching frame within %ss; saw %s"
                         % (timeout, [f.get("type") for f in seen]))


def _expect(ws, rtype: str, timeout: float = 15.0):
    return _drain_until(ws, lambda f: f.get("type") == rtype, timeout)[0]


def _of(frames, rtype):
    return [f for f in frames if f.get("type") == rtype]


def _pair(ws_a, ws_b):
    """Full happy path; returns (session_id, leader_data, follower_data)."""
    ws_a.send(_req("CREATE", rid="c"))
    created = _expect(ws_a, "ACK")["data"]
    assert len(created["code"]) == 6 and created["code"].isdigit()
    assert created["role"] == "leader"
    _expect(ws_a, "STATE")
    ws_b.send(_req("JOIN", {"code": created["code"]}, rid="j"))
    joined = _expect(ws_b, "ACK")["data"]
    _expect(ws_a, "JOIN_REQUEST")
    ws_a.send(_req("APPROVE", {"memberId": joined["memberId"]},
                   sid=created["sessionId"], rid="a"))
    _expect(ws_a, "ACK")
    st_a = _expect(ws_a, "STATE")
    st_b = _expect(ws_b, "STATE")
    assert len(st_a["presence"]) == 2 == len(st_b["presence"])
    assert st_a["sessionId"] == created["sessionId"] == st_b["sessionId"]
    return created["sessionId"], created, joined


# ── happy path ─────────────────────────────────────────────────────


def test_create_join_approve_state_sequence(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        assert _recv(ws_a)["type"] == "HELLO"
        assert _recv(ws_b)["type"] == "HELLO"
        sid, created, joined = _pair(ws_a, ws_b)
        assert created["sessionId"] == sid
        assert joined["sessionId"] == sid if "sessionId" in joined else True
        ws_b.send(_req("SET_ACTIVITY", {"state": "suspended"}, sid=sid, rid="s"))
        st = _expect(ws_a, "STATE")
        assert st["phase"] in ("selecting", "paused", "loading", "reading")


def test_ping_pong(server):
    with _ws(server) as ws:
        _recv(ws)
        ws.send(_req("PING", {"seq": 7}, rid="p"))
        assert _expect(ws, "PONG")["seq"] == 7


# ── reject paths ───────────────────────────────────────────────────


def test_join_wrong_code_rejected(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        ws_a.send(_req("CREATE", rid="c"))
        created = _expect(ws_a, "ACK")["data"]
        _expect(ws_a, "STATE")
        ws_b.send(_req("JOIN", {"code": "000000"}, rid="j"))
        rej = _expect(ws_b, "REJECT")
        assert rej["reason"] == "bad_code"
        # the waiting session still accepts the real code afterwards
        ws_b.send(_req("JOIN", {"code": created["code"]}, rid="j2"))
        assert _expect(ws_b, "ACK")["request"] == "JOIN"


def test_second_join_while_pending_rejected(server):
    with _ws(server) as ws_a, _ws(server) as ws_b, _ws(server) as ws_c:
        _recv(ws_a); _recv(ws_b); _recv(ws_c)
        ws_a.send(_req("CREATE", rid="c"))
        created = _expect(ws_a, "ACK")["data"]
        _expect(ws_a, "STATE")
        ws_b.send(_req("JOIN", {"code": created["code"]}, rid="j"))
        _expect(ws_b, "ACK")
        _expect(ws_a, "JOIN_REQUEST")
        ws_c.send(_req("JOIN", {"code": created["code"]}, rid="j"))
        assert _expect(ws_c, "REJECT")["reason"] == "code_consumed"


def test_reject_ends_session(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        ws_a.send(_req("CREATE", rid="c"))
        created = _expect(ws_a, "ACK")["data"]
        _expect(ws_a, "STATE")
        ws_b.send(_req("JOIN", {"code": created["code"]}, rid="j"))
        joined = _expect(ws_b, "ACK")["data"]
        _expect(ws_a, "JOIN_REQUEST")
        ws_a.send(_req("REJECT", {"memberId": joined["memberId"]},
                       sid=created["sessionId"], rid="r"))
        assert _expect(ws_a, "END")["reason"] == "rejected"
        assert _expect(ws_b, "END")["reason"] == "rejected"


def test_leave_ends_session(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, _, _ = _pair(ws_a, ws_b)
        ws_b.send(_req("LEAVE", sid=sid, rid="l"))
        assert _expect(ws_a, "END")["reason"] == "left"
        assert _expect(ws_b, "END")["reason"] == "left"


def test_cross_session_frame_rejected(server):
    with _ws(server) as ws_a, _ws(server) as ws_b, \
            _ws(server) as ws_c, _ws(server) as ws_d:
        for ws in (ws_a, ws_b, ws_c, ws_d):
            _recv(ws)
        sid_a, _, _ = _pair(ws_a, ws_b)
        sid_b, _, _ = _pair(ws_c, ws_d)
        assert sid_a != sid_b
        ws_a.send(_req("LEAVE", sid=sid_b, rid="x"))
        assert _expect(ws_a, "REJECT")["reason"] == "wrong_session"


def test_session_frame_without_session_rejected(server):
    with _ws(server) as ws:
        _recv(ws)
        ws.send(_req("TURN", {"delta": 1, "crossChapter": True,
                              "fromChapter": None},
                     sid="no-such-session", rid="x"))
        assert _expect(ws, "REJECT")["reason"] in ("no_session", "wrong_session")


def test_sequenced_requests_gated_before_work(server):
    """TURN before any manifest reaches the sequencer and is gated."""
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, _, _ = _pair(ws_a, ws_b)
        ws_b.send(_req("TURN", {"delta": 1, "crossChapter": True,
                                "fromChapter": None},
                       sid=sid, rid="t"))
        rej = _expect(ws_b, "REJECT")
        assert rej["reason"] == "no_work"
        assert rej["snapshot"]["revision"] >= 0


# ── signaling / transport ──────────────────────────────────────────


def test_signal_relayed_between_paired_members(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, _, _ = _pair(ws_a, ws_b)
        ws_a.send(_req("SIGNAL", {"kind": "offer", "data": {"sdp": "live-1"}},
                       sid=sid, rid="sig"))
        sig = _expect(ws_b, "SIGNAL")
        assert sig["kind"] == "offer" and sig["from"] == "leader"
        assert sig["data"] == {"sdp": "live-1"}
        assert _expect(ws_a, "ACK")["requestId"] == "sig"


def test_signal_requires_pair(server):
    with _ws(server) as ws:
        _recv(ws)
        ws.send(_req("CREATE", rid="c"))
        created = _expect(ws, "ACK")["data"]
        _expect(ws, "STATE")
        ws.send(_req("SIGNAL", {"kind": "offer", "data": {}},
                     sid=created["sessionId"], rid="s"))
        assert _expect(ws, "REJECT")["reason"] == "not_paired"


def test_transport_switch_at_revision_boundary(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, _, _ = _pair(ws_a, ws_b)
        ws_a.send(_req("TRANSPORT_STATE", {"state": "up"}, sid=sid, rid="u1"))
        ws_b.send(_req("TRANSPORT_STATE", {"state": "up"}, sid=sid, rid="u2"))
        ts, seen = _drain_until(
            ws_a, lambda f: f.get("type") == "TRANSPORT_STATE"
            and f.get("dataPlane") == "webrtc-dc")
        revs = [f["revision"] for f in seen if f["type"] == "STATE"]
        assert ts["revision"] >= (revs[-1] if revs else 0)
        ws_b.send(_req("TRANSPORT_STATE", {"state": "down"}, sid=sid, rid="d"))
        ts2, _ = _drain_until(
            ws_b, lambda f: f.get("type") == "TRANSPORT_STATE"
            and f.get("dataPlane") == "server-ws")
        assert ts2["revision"] >= ts["revision"]


# ── UI_SYNC ephemeral relay ────────────────────────────────────────


def _ui_payload(**kw):
    p = {"view": "browse", "path": "group/episodes", "top": 120.5,
         "viewH": 600, "scrollH": 4200, "mode": "tile"}
    p.update(kw)
    return p


def test_ui_sync_relayed_between_paired_members(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, _, _ = _pair(ws_a, ws_b)
        ws_a.send(_req("UI_SYNC", _ui_payload(), sid=sid, rid="u1"))
        ui = _expect(ws_b, "UI_SYNC")
        assert ui["from"] == "leader"
        assert ui["payload"] == _ui_payload()
        assert ui["sessionId"] == sid


def test_ui_sync_malformed_payload_rejected(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, _, _ = _pair(ws_a, ws_b)
        ws_a.send(_req("UI_SYNC", _ui_payload(view="overview"),
                       sid=sid, rid="m1"))
        rej = _expect(ws_a, "REJECT")
        assert rej["reason"] == "malformed"
        assert rej["requestId"] == "m1"


def test_ui_sync_optional_fields_and_root_path(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, _, _ = _pair(ws_a, ws_b)
        ws_a.send(_req("UI_SYNC",
                       {"view": "browse", "path": None, "top": 0, "viewH": 800},
                       sid=sid, rid="u"))
        ui = _expect(ws_b, "UI_SYNC")
        assert ui["payload"]["path"] is None and ui["payload"]["top"] == 0


# ── MATCH one-tap pairing ──────────────────────────────────────────


def _match(ws, rid="m1"):
    ws.send(_req("MATCH", rid=rid))
    ack = _expect(ws, "ACK")
    assert ack["request"] == "MATCH"
    return ack["data"]


def test_match_becomes_waiting_leader_when_nobody_waits(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        created = _match(ws_a)
        assert created["role"] == "leader"
        assert created["code"].isdigit() and len(created["code"]) == 6
        ws_b.send(_req("JOIN", {"code": created["code"]}, rid="j"))
        assert _expect(ws_b, "ACK")["request"] == "JOIN"
        assert _expect(ws_a, "JOIN_REQUEST")["memberId"]


def test_match_joins_fresh_waiting_session(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        created = _match(ws_a)
        joined = _match(ws_b, rid="m2")
        assert joined["role"] == "follower"
        assert joined["sessionId"] == created["sessionId"]
        assert joined["leaderName"]
        assert _expect(ws_a, "JOIN_REQUEST")["memberId"] == joined["memberId"]


def test_match_never_hijacks_a_create_session(server):
    with _ws(server) as ws_a, _ws(server) as ws_b, _ws(server) as ws_c:
        _recv(ws_a); _recv(ws_b); _recv(ws_c)
        ws_a.send(_req("CREATE", rid="c"))
        created = _expect(ws_a, "ACK")["data"]
        _expect(ws_a, "STATE")
        data = _match(ws_b)
        assert data["role"] == "leader"
        assert data["sessionId"] != created["sessionId"]
        ws_c.send(_req("JOIN", {"code": created["code"]}, rid="j"))
        joined = _expect(ws_c, "ACK")["data"]
        assert _expect(ws_a, "JOIN_REQUEST")["memberId"] == joined["memberId"]


def test_match_rejected_when_already_bound(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, _, _ = _pair(ws_a, ws_b)
        ws_a.send(_req("MATCH", sid=sid, rid="m"))
        assert _expect(ws_a, "REJECT")["reason"] == "already_bound"


# ── pending lifecycle / resume ─────────────────────────────────────


def test_pending_leave_frees_code_for_retry(server):
    with _ws(server) as ws_a, _ws(server) as ws_b, _ws(server) as ws_c:
        _recv(ws_a); _recv(ws_b); _recv(ws_c)
        ws_a.send(_req("CREATE", rid="c"))
        created = _expect(ws_a, "ACK")["data"]
        _expect(ws_a, "STATE")
        ws_b.send(_req("JOIN", {"code": created["code"]}, rid="j"))
        joined = _expect(ws_b, "ACK")["data"]
        _expect(ws_a, "JOIN_REQUEST")
        ws_b.send(_req("LEAVE", sid=created["sessionId"], rid="l"))
        jc = _expect(ws_a, "JOIN_CANCEL")
        assert jc["memberId"] == joined["memberId"]
        ws_c.send(_req("JOIN", {"code": created["code"]}, rid="j2"))
        assert _expect(ws_c, "ACK")["request"] == "JOIN"


def test_resume_within_window(server):
    with _ws(server) as ws_a:
        _recv(ws_a)
        ws_a.send(_req("CREATE", rid="c"))
        created = _expect(ws_a, "ACK")["data"]
        _expect(ws_a, "STATE")
        member_id, token = created["memberId"], created["resumeToken"]
        sid = created["sessionId"]
    with _ws(server) as ws_back:
        _recv(ws_back)
        ws_back.send(_req("RESUME", {"memberId": member_id,
                                     "resumeToken": token},
                          sid=sid, rid="re"))
        ack = _expect(ws_back, "ACK")
        assert ack["request"] == "RESUME"
        st = _expect(ws_back, "STATE")
        assert st["presence"][member_id]["status"] == "active"


def test_resume_bad_token_rejected(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        ws_a.send(_req("CREATE", rid="c"))
        created = _expect(ws_a, "ACK")["data"]
        _expect(ws_a, "STATE")
    with _ws(server) as ws_bad:
        _recv(ws_bad)
        ws_bad.send(_req("RESUME", {"memberId": created["memberId"],
                                    "resumeToken": "WRONG"},
                         sid=created["sessionId"], rid="re"))
        assert _expect(ws_bad, "REJECT")["reason"] == "bad_resume"


# ── frame hygiene ──────────────────────────────────────────────────


def test_oversized_frame_closes_link(server):
    with _ws(server) as ws:
        _recv(ws)
        ws.send("x" * (MAX_FRAME_BYTES + 1))
        with pytest.raises(Exception):
            while True:
                _recv(ws, timeout=5.0)


def test_malformed_frame_rejected_not_closed(server):
    with _ws(server) as ws:
        _recv(ws)
        ws.send(json.dumps({"v": PROTOCOL_VERSION,
                            "requestId": "bad1", "type": "RESUME"}))
        rej = _expect(ws, "REJECT")
        assert rej["reason"] == "malformed"
        ws.send(_req("PING", {"seq": 1}, rid="p"))
        assert _expect(ws, "PONG")["seq"] == 1
