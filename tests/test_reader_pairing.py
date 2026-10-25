"""S03 — dual-reader pairing/session registry tests (#78).

Transport-free: exercises ``app.reader_sync.sessions`` directly with a fake
clock and fake links (bounded outq drained by hand). No WS, no network.
"""
import asyncio
import json
import logging
import sys
from pathlib import Path

import pytest

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from app.reader_sync import protocol  # noqa: E402
from app.reader_sync.sessions import (  # noqa: E402
    END_CODE_EXPIRED,
    END_LEFT,
    END_REJECTED,
    END_RESUME_EXPIRED,
    HEARTBEAT_INTERVAL,
    MATCH_WINDOW_SECONDS,
    NO_RESPONSE_TIMEOUT,
    REASON_ALREADY_BOUND,
    REASON_BAD_CODE,
    REASON_BAD_RESUME,
    REASON_CODE_CONSUMED,
    REASON_CODE_EXPIRED,
    REASON_MALFORMED,
    REASON_NOT_LEADER,
    REASON_NOT_PAIRED,
    REASON_NO_PENDING,
    REASON_NO_SESSION,
    REASON_PEER_OFFLINE,
    REASON_RESUME_EXPIRED,
    REASON_RESUME_IN_USE,
    REASON_WRONG_SESSION,
    RESUME_WINDOW_SECONDS,
    PeerLink,
    SessionRegistry,
    WS_CLOSE_TOO_BIG,
)


class FakeClock:
    def __init__(self, t: float = 10_000.0):
        self.t = t

    def now(self) -> float:
        return self.t

    def advance(self, dt: float) -> float:
        self.t += dt
        return self.t


class FakeLink(PeerLink):
    """PeerLink whose outq is drained by the test; close is recorded."""

    def __init__(self, registry: SessionRegistry, user=None, name: str = ""):
        super().__init__(registry)
        self.user = user or {"email": f"{name}@example.com", "name": name or "user", "picture": ""}
        self.name = name
        self.close_code = None
        self.close_reason = None
        self.captured: list[dict] = []   # non-PING frames drained by helpers

    def request_transport_close(self, code: int, reason: str) -> None:
        self.close_code = code
        self.close_reason = reason

    def frames(self) -> list[dict]:
        out = []
        while True:
            try:
                raw = self.outq.get_nowait()
            except asyncio.QueueEmpty:
                break
            out.append(json.loads(raw))
        return out

    def types(self) -> list[str]:
        return [f["type"] for f in self.frames()]


def _reg(clock: FakeClock, **kw) -> SessionRegistry:
    return SessionRegistry(clock=clock.now, **kw)


def _request(rtype: str, payload=None, *, session_id=None, request_id="req-1",
             work_epoch=0, base_revision=0) -> dict:
    return {
        "v": protocol.PROTOCOL_VERSION,
        "requestId": request_id,
        "sessionId": session_id,
        "workEpoch": work_epoch,
        "baseRevision": base_revision,
        "type": rtype,
        "payload": payload or {},
    }


def _send(reg: SessionRegistry, link: FakeLink, frame: dict) -> None:
    reg.handle_raw(link, json.dumps(frame))


def _connect(reg: SessionRegistry, name: str, user=None) -> FakeLink:
    link = FakeLink(reg, user=user, name=name)
    reg.register_link(link)
    hello = link.frames()
    assert [f["type"] for f in hello] == ["HELLO"]
    return link


def _keepalive(reg: SessionRegistry, clock: FakeClock, link: FakeLink,
               seconds: float, step: float = 10.0) -> None:
    """Advance the clock while auto-answering server PINGs so `link` stays
    alive — real clients PONG the heartbeat; silent links die at 15s."""
    remaining = seconds
    while remaining > 0:
        clock.advance(min(step, remaining))
        remaining -= step
        reg.tick()
        for f in link.frames():
            if f["type"] == "PING":
                _send(reg, link, {"v": 1, "type": "PONG", "seq": f["seq"]})
            else:
                link.captured.append(f)


def _advance_alive(reg: SessionRegistry, clock: FakeClock, link: FakeLink,
                   seconds: float, step: float = 10.0) -> None:
    """Advance the clock keeping `link` live via inbound PONGs WITHOUT running
    tick() — for tests that must reach past an expiry boundary before the
    sweep would have fired."""
    remaining = seconds
    while remaining > 0:
        clock.advance(min(step, remaining))
        remaining -= step
        _send(reg, link, {"v": 1, "type": "PONG", "seq": 0})
        link.captured.extend(link.frames())


def _of_type(frames: list[dict], t: str) -> list[dict]:
    return [f for f in frames if f["type"] == t]


def _one(frames: list[dict], t: str) -> dict:
    got = _of_type(frames, t)
    assert len(got) == 1, f"expected exactly one {t}, got {frames!r}"
    return got[0]


def _create_session(reg: SessionRegistry, leader: FakeLink) -> tuple[dict, dict]:
    """CREATE -> (ACK data, first STATE)."""
    _send(reg, leader, _request("CREATE", request_id="c1"))
    frames = leader.frames()
    ack = _one(frames, "ACK")
    assert ack["request"] == "CREATE" and ack["requestId"] == "c1"
    data = ack["data"]
    assert isinstance(data["code"], str) and len(data["code"]) == 6 and data["code"].isdigit()
    assert data["role"] == "leader"
    assert data["resumeToken"]
    st = _one(frames, "STATE")
    assert len(st["presence"]) == 1
    return data, st


def _join(reg: SessionRegistry, follower: FakeLink, code: str) -> dict:
    _send(reg, follower, _request("JOIN", {"code": code}, request_id="j1"))
    ack = _one(follower.frames(), "ACK")
    assert ack["request"] == "JOIN"
    return ack["data"]


def _pair(reg: SessionRegistry, leader: FakeLink, follower: FakeLink):
    """Full happy path; returns (session_id, leader_member, follower_member)."""
    created, _ = _create_session(reg, leader)
    joined = _join(reg, follower, created["code"])
    # leader sees the pending join request
    jr = _one(leader.frames(), "JOIN_REQUEST")
    assert jr["memberId"] == joined["memberId"]
    _send(reg, leader, _request("APPROVE", {"memberId": joined["memberId"]},
                              session_id=created["sessionId"], request_id="a1"))
    lframes = leader.frames()
    fframes = follower.frames()
    lst = _one(lframes, "STATE")
    fst = _one(fframes, "STATE")
    assert len(lst["presence"]) == 2 and len(fst["presence"]) == 2
    assert lst["sessionId"] == created["sessionId"] == fst["sessionId"]
    return created["sessionId"], created, joined


def _run(coro) -> None:
    """Drive a coroutine on a fresh loop in a worker thread — earlier suite
    tests may leave a running loop on the main thread, where asyncio.run
    refuses to nest."""
    import threading
    box: dict = {}

    def go():
        try:
            box["ok"] = asyncio.run(coro)
        except BaseException as e:  # noqa: BLE001 — re-raise in test
            box["err"] = e

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(timeout=10)
    if "err" in box:
        raise box["err"]
    assert not t.is_alive(), "coroutine did not finish within 10s"


# ── happy path ─────────────────────────────────────────────────────


def test_create_join_approve_state_sequence():
    clock = FakeClock()
    reg = _reg(clock)
    leader = _connect(reg, "leader")
    follower = _connect(reg, "follower")

    session_id, created, joined = _pair(reg, leader, follower)

    roles = {m.id: m.role for m in reg.sessions[session_id].members.values()}
    assert created["memberId"] in roles and joined["memberId"] in roles
    assert roles[created["memberId"]] == "leader"
    assert roles[joined["memberId"]] == "follower"
    # the session state is well-formed per the S01 wire contract
    _send(reg, follower, _request("SET_ACTIVITY", {"state": "suspended"}, session_id=session_id))
    state = _one(leader.frames(), "STATE")
    assert state["phase"] in protocol.PHASES
    assert state["revision"] >= 2
    protocol.validate_state(state)


def test_ping_pong():
    clock = FakeClock()
    reg = _reg(clock)
    link = _connect(reg, "a")
    _send(reg, link, _request("PING", {"seq": 7}))
    frames = link.frames()
    pong = _one(frames, "PONG")
    assert pong["seq"] == 7


# ── reject paths ───────────────────────────────────────────────────


def test_join_wrong_code_rejected():
    clock = FakeClock()
    reg = _reg(clock)
    leader = _connect(reg, "leader")
    _create_session(reg, leader)
    stranger = _connect(reg, "stranger")

    _send(reg, stranger, _request("JOIN", {"code": "000000"}))
    rej = _one(stranger.frames(), "REJECT")
    assert rej["reason"] == REASON_BAD_CODE

    # existing session untouched
    session = next(iter(reg.sessions.values()))
    assert len(session.members) == 1 and session.pending is None


def test_join_expired_code_rejected():
    clock = FakeClock()
    reg = _reg(clock)
    leader = _connect(reg, "leader")
    created, _ = _create_session(reg, leader)
    _advance_alive(reg, clock, leader, 301.0)

    stranger = _connect(reg, "stranger")
    _send(reg, stranger, _request("JOIN", {"code": created["code"]}))
    rej = _one(stranger.frames(), "REJECT")
    assert rej["reason"] == REASON_CODE_EXPIRED
    # ...and once the sweep has run, the dead code is indistinguishable
    reg.tick()
    late = _connect(reg, "late")
    _send(reg, late, _request("JOIN", {"code": created["code"]}))
    assert _one(late.frames(), "REJECT")["reason"] == REASON_BAD_CODE


def test_tick_expires_waiting_session():
    clock = FakeClock()
    reg = _reg(clock)
    leader = _connect(reg, "leader")
    _create_session(reg, leader)
    _keepalive(reg, clock, leader, 301.0)
    end = _one(leader.captured + leader.frames(), "END")
    assert end["reason"] == END_CODE_EXPIRED
    assert not reg.sessions


def test_consumed_code_reuse_rejected():
    clock = FakeClock()
    reg = _reg(clock)
    leader, follower = _connect(reg, "leader"), _connect(reg, "follower")
    session_id, created, _ = _pair(reg, leader, follower)

    third = _connect(reg, "third")
    _send(reg, third, _request("JOIN", {"code": created["code"]}))
    rej = _one(third.frames(), "REJECT")
    assert rej["reason"] == REASON_CODE_CONSUMED
    assert len(reg.sessions[session_id].members) == 2


def test_second_join_while_pending_rejected():
    clock = FakeClock()
    reg = _reg(clock)
    leader = _connect(reg, "leader")
    created, _ = _create_session(reg, leader)
    f1 = _connect(reg, "f1")
    _join(reg, f1, created["code"])

    f2 = _connect(reg, "f2")
    _send(reg, f2, _request("JOIN", {"code": created["code"]}))
    rej = _one(f2.frames(), "REJECT")
    assert rej["reason"] == REASON_CODE_CONSUMED


def test_reject_ends_session():
    clock = FakeClock()
    reg = _reg(clock)
    leader = _connect(reg, "leader")
    created, _ = _create_session(reg, leader)
    f1 = _connect(reg, "f1")
    joined = _join(reg, f1, created["code"])
    leader.frames()  # JOIN_REQUEST

    _send(reg, leader, _request("REJECT", {"memberId": joined["memberId"]},
                              session_id=created["sessionId"]))
    lend = _one(leader.frames(), "END")
    fend = _one(f1.frames(), "END")
    assert lend["reason"] == END_REJECTED == fend["reason"]
    assert not reg.sessions
    assert leader.member is None and f1.member is None


def test_leave_ends_session():
    clock = FakeClock()
    reg = _reg(clock)
    leader, follower = _connect(reg, "leader"), _connect(reg, "follower")
    session_id, _, _ = _pair(reg, leader, follower)

    _send(reg, follower, _request("LEAVE", session_id=session_id))
    for link in (leader, follower):
        end = _one(link.frames(), "END")
        assert end["reason"] == END_LEFT
    assert not reg.sessions


def test_pending_leave_ends_attempt():
    clock = FakeClock()
    reg = _reg(clock)
    leader = _connect(reg, "leader")
    created, _ = _create_session(reg, leader)
    f1 = _connect(reg, "f1")
    joined = _join(reg, f1, created["code"])
    leader.frames()

    _send(reg, f1, _request("LEAVE", session_id=created["sessionId"]))
    assert _one(f1.frames(), "END")
    assert _one(leader.frames(), "END")
    assert not reg.sessions


def test_cross_session_frame_rejected():
    clock = FakeClock()
    reg = _reg(clock)
    la, fa = _connect(reg, "la"), _connect(reg, "fa")
    sid_a, _, _ = _pair(reg, la, fa)
    lb, fb = _connect(reg, "lb"), _connect(reg, "fb")
    sid_b, _, _ = _pair(reg, lb, fb)

    # member of A sends a frame addressed at session B
    _send(reg, la, _request("LEAVE", session_id=sid_b))
    rej = _one(la.frames(), "REJECT")
    assert rej["reason"] == REASON_WRONG_SESSION
    # both sessions survive untouched
    assert len(reg.sessions[sid_a].members) == 2
    assert len(reg.sessions[sid_b].members) == 2


def test_session_frame_without_session_rejected():
    clock = FakeClock()
    reg = _reg(clock)
    lone = _connect(reg, "lone")
    _send(reg, lone, _request("TURN", {"delta": 1, "crossChapter": True, "fromChapter": None},
                            session_id="nope"))
    rej = _one(lone.frames(), "REJECT")
    assert rej["reason"] == REASON_NO_SESSION


def test_unapproved_member_reads_rejected():
    clock = FakeClock()
    reg = _reg(clock)
    leader = _connect(reg, "leader")
    created, _ = _create_session(reg, leader)
    f1 = _connect(reg, "f1")
    _join(reg, f1, created["code"])
    leader.frames()

    for t, payload in (
        ("TURN", {"delta": 1, "crossChapter": True, "fromChapter": None}),
        ("OPEN_WORK", {"folder": "x", "page": None}),
        ("GOTO", {"page": {"folder": "x", "name": "p1.png"}}),
        ("SIGNAL", {"kind": "offer", "data": {"sdp": "x"}}),
    ):
        _send(reg, f1, _request(t, payload, session_id=created["sessionId"]))
    rejects = _of_type(f1.frames(), "REJECT")
    assert len(rejects) == 4
    assert all(r["reason"] == REASON_NOT_PAIRED for r in rejects)
    # leader session untouched: no STATE went out
    assert _of_type(leader.frames(), "STATE") == []


def test_approve_requires_leader_and_pending():
    clock = FakeClock()
    reg = _reg(clock)
    leader, follower = _connect(reg, "leader"), _connect(reg, "follower")
    sid, _, joined = _pair(reg, leader, follower)

    # paired follower cannot approve
    _send(reg, follower, _request("APPROVE", {"memberId": "x"}, session_id=sid))
    rej = _one(follower.frames(), "REJECT")
    assert rej["reason"] == REASON_NOT_LEADER

    # leader with nobody pending
    _send(reg, leader, _request("APPROVE", {"memberId": "x"}, session_id=sid))
    rej = _one(leader.frames(), "REJECT")
    assert rej["reason"] == REASON_NO_PENDING


def test_already_bound_cannot_create_or_join():
    clock = FakeClock()
    reg = _reg(clock)
    leader = _connect(reg, "leader")
    created, _ = _create_session(reg, leader)
    _send(reg, leader, _request("CREATE"))
    rej = _one(leader.frames(), "REJECT")
    assert rej["reason"] == REASON_ALREADY_BOUND


def test_sequencing_requests_gated_until_work_loaded():
    # S05 (#80): TURN et al. are real sequenced ops now — pre-manifest they
    # reach the sequencer and are gated by phase ("no_work"), not deferred.
    clock = FakeClock()
    reg = _reg(clock)
    leader, follower = _connect(reg, "leader"), _connect(reg, "follower")
    sid, _, _ = _pair(reg, leader, follower)
    _send(reg, follower, _request("TURN", {"delta": 1, "crossChapter": True, "fromChapter": None},
                                session_id=sid))
    rej = _one(follower.frames(), "REJECT")
    assert rej["reason"] == "no_work"
    assert rej["snapshot"]["revision"] == reg.sessions[sid].revision
    assert len(reg.sessions[sid].members) == 2


# ── heartbeat / presence / resume ──────────────────────────────────


def test_heartbeat_ping_after_interval():
    clock = FakeClock()
    reg = _reg(clock)
    link = _connect(reg, "a")
    link.frames()
    clock.advance(HEARTBEAT_INTERVAL)
    reg.tick()
    ping = _one(link.frames(), "PING")
    assert ping["v"] == protocol.PROTOCOL_VERSION
    # pong resets liveness
    _send(reg, link, {"v": 1, "type": "PONG", "seq": ping["seq"]})
    clock.advance(NO_RESPONSE_TIMEOUT - 1)
    reg.tick()
    assert link.alive


def test_no_response_disconnects_and_marks_offline():
    clock = FakeClock()
    reg = _reg(clock)
    leader, follower = _connect(reg, "leader"), _connect(reg, "follower")
    sid, _, _ = _pair(reg, leader, follower)

    # follower goes silent: leader keeps answering heartbeat, follower doesn't
    _keepalive(reg, clock, leader, NO_RESPONSE_TIMEOUT + 0.5)
    assert not follower.alive
    assert follower.close_code is not None

    st = _one(leader.captured + leader.frames(), "STATE")
    member = next(m for m in reg.sessions[sid].members.values() if m.role == "follower")
    assert st["presence"][member.id]["status"] == "offline"


def test_resume_within_window():
    clock = FakeClock()
    reg = _reg(clock)
    leader = _connect(reg, "leader")
    created, _ = _create_session(reg, leader)

    reg.detach_link(leader)
    clock.advance(60.0)
    assert not reg.sessions[created["sessionId"]].members[created["memberId"]].link

    back = _connect(reg, "leader-again")
    _send(reg, back, _request(
        "RESUME", {"memberId": created["memberId"], "resumeToken": created["resumeToken"]},
        session_id=created["sessionId"]))
    frames = back.frames()
    ack = _one(frames, "ACK")
    assert ack["request"] == "RESUME"
    st = _one(frames, "STATE")
    assert st["presence"][created["memberId"]]["status"] == "active"
    assert back.member is reg.sessions[created["sessionId"]].members[created["memberId"]]


def test_resume_bad_token_and_expired_window():
    clock = FakeClock()
    reg = _reg(clock)
    leader = _connect(reg, "leader")
    created, _ = _create_session(reg, leader)
    sid = created["sessionId"]

    reg.detach_link(leader)
    bad = _connect(reg, "bad")
    _send(reg, bad, _request(
        "RESUME", {"memberId": created["memberId"], "resumeToken": "WRONG"},
        session_id=sid))
    rej = _one(bad.frames(), "REJECT")
    assert rej["reason"] == REASON_BAD_RESUME

    # past the resume window -> explicit resume_expired on the request path
    clock.advance(RESUME_WINDOW_SECONDS + 1)
    late = _connect(reg, "late")
    _send(reg, late, _request(
        "RESUME", {"memberId": created["memberId"], "resumeToken": created["resumeToken"]},
        session_id=sid))
    rej = _one(late.frames(), "REJECT")
    assert rej["reason"] == REASON_RESUME_EXPIRED

    # the tick sweep reaps the dead session
    reg.tick()
    assert sid not in reg.sessions


def test_resume_expiry_ends_session_via_tick():
    clock = FakeClock()
    reg = _reg(clock)
    leader, follower = _connect(reg, "leader"), _connect(reg, "follower")
    sid, _, _ = _pair(reg, leader, follower)
    reg.detach_link(follower)
    _keepalive(reg, clock, leader, RESUME_WINDOW_SECONDS + 1)
    assert sid not in reg.sessions
    end = _one(leader.captured + leader.frames(), "END")
    assert end["reason"] == END_RESUME_EXPIRED


def test_resume_in_use_rejected():
    clock = FakeClock()
    reg = _reg(clock)
    leader = _connect(reg, "leader")
    created, _ = _create_session(reg, leader)
    other = _connect(reg, "other")
    _send(reg, other, _request(
        "RESUME", {"memberId": created["memberId"], "resumeToken": created["resumeToken"]},
        session_id=created["sessionId"]))
    rej = _one(other.frames(), "REJECT")
    assert rej["reason"] == REASON_RESUME_IN_USE


def test_set_activity_updates_presence():
    clock = FakeClock()
    reg = _reg(clock)
    leader, follower = _connect(reg, "leader"), _connect(reg, "follower")
    sid, _, joined = _pair(reg, leader, follower)

    _send(reg, follower, _request("SET_ACTIVITY", {"state": "suspended"}, session_id=sid))
    st = _one(leader.frames(), "STATE")
    assert st["presence"][joined["memberId"]]["status"] == "suspended"

    _send(reg, follower, _request("SET_ACTIVITY", {"state": "active"}, session_id=sid))
    st = _one(leader.frames(), "STATE")
    assert st["presence"][joined["memberId"]]["status"] == "active"


# ── bounded receive / send queue ───────────────────────────────────


def test_oversized_frame_closes_link():
    clock = FakeClock()
    reg = _reg(clock)
    link = _connect(reg, "a")
    reg.handle_raw(link, "x" * (protocol.MAX_FRAME_BYTES + 1))
    assert not link.alive
    assert link.close_code == WS_CLOSE_TOO_BIG


def test_malformed_frame_rejected_not_closed():
    clock = FakeClock()
    reg = _reg(clock)
    link = _connect(reg, "a")
    reg.handle_raw(link, "definitely not json")
    rej = _one(link.frames(), "REJECT")
    assert rej["reason"] == REASON_MALFORMED
    assert link.alive


def test_send_queue_overflow_disconnects_peer():
    clock = FakeClock()
    reg = _reg(clock, send_queue_max=4)
    link = _connect(reg, "a")
    link.frames()
    # nobody drains the fake link: 4 queued + the next send overflows
    for _ in range(4):
        link.send_obj({"v": 1, "type": "PING", "seq": 0})
    assert link.alive
    link.send_obj({"v": 1, "type": "PING", "seq": 1})
    assert not link.alive
    assert link.close_code is not None


def test_slow_peer_does_not_block_other_member():
    clock = FakeClock()
    reg = _reg(clock, send_queue_max=2)
    leader, follower = _connect(reg, "leader"), _connect(reg, "follower")
    sid, _, _ = _pair(reg, leader, follower)
    # follower stalls and never drains; leader keeps up. Each SET_ACTIVITY
    # broadcast queues one STATE per member — the 3rd overflows the bound.
    seen = []
    for i in range(3):
        _send(reg, leader, _request(
            "SET_ACTIVITY", {"state": "suspended" if i % 2 == 0 else "active"},
            session_id=sid, request_id=f"sa{i}"))
        seen += leader.frames()
    assert not follower.alive
    assert any(f["type"] == "STATE" for f in seen)


# ── signaling & transport switching ────────────────────────────────


def test_signal_relayed_between_paired_members():
    clock = FakeClock()
    reg = _reg(clock)
    leader, follower = _connect(reg, "leader"), _connect(reg, "follower")
    sid, _, _ = _pair(reg, leader, follower)

    _send(reg, leader, _request(
        "SIGNAL", {"kind": "offer", "data": {"sdp": "SDP-SECRET-1"}},
        session_id=sid, request_id="sig1"))
    fframes = follower.frames()
    sig = _one(fframes, "SIGNAL")
    assert sig["kind"] == "offer" and sig["from"] == "leader"
    assert sig["data"] == {"sdp": "SDP-SECRET-1"}
    assert _one(leader.frames(), "ACK")["requestId"] == "sig1"


def test_signal_requires_pair_and_live_peer():
    clock = FakeClock()
    reg = _reg(clock)
    leader = _connect(reg, "leader")
    created, _ = _create_session(reg, leader)
    _send(reg, leader, _request(
        "SIGNAL", {"kind": "offer", "data": {}}, session_id=created["sessionId"]))
    rej = _one(leader.frames(), "REJECT")
    assert rej["reason"] == REASON_NOT_PAIRED

    f1 = _connect(reg, "f1")
    _join(reg, f1, created["code"])
    _send(reg, leader, _request("APPROVE", {"memberId": f1.member.id},
                              session_id=created["sessionId"]))
    leader.frames(); f1.frames()
    reg.detach_link(f1)
    _send(reg, leader, _request(
        "SIGNAL", {"kind": "ice", "data": {}}, session_id=created["sessionId"]))
    rej = _one(leader.frames(), "REJECT")
    assert rej["reason"] == REASON_PEER_OFFLINE


def test_transport_switch_at_revision_boundary():
    clock = FakeClock()
    reg = _reg(clock)
    leader, follower = _connect(reg, "leader"), _connect(reg, "follower")
    sid, _, _ = _pair(reg, leader, follower)
    session = reg.sessions[sid]
    rev0 = session.revision

    _send(reg, leader, _request("TRANSPORT_STATE", {"state": "up"}, session_id=sid))
    _send(reg, follower, _request("TRANSPORT_STATE", {"state": "up"}, session_id=sid))
    assert session.data_plane == "webrtc-dc"
    lframes = leader.frames()
    st = _one(lframes, "STATE")
    ts = _one(lframes, "TRANSPORT_STATE")
    assert ts["dataPlane"] == "webrtc-dc"
    assert ts["revision"] == st["revision"] > rev0

    _send(reg, follower, _request("TRANSPORT_STATE", {"state": "down"}, session_id=sid))
    assert session.data_plane == "server-ws"
    lframes = leader.frames()
    st2 = _one(lframes, "STATE")
    ts2 = _one(lframes, "TRANSPORT_STATE")
    assert ts2["dataPlane"] == "server-ws"
    assert st2["revision"] == ts2["revision"] > st["revision"]
    # members untouched by the flap
    assert len(session.members) == 2


# ── pending member link death ──────────────────────────────────────


def test_pending_link_death_notifies_leader():
    clock = FakeClock()
    reg = _reg(clock)
    leader = _connect(reg, "leader")
    created, _ = _create_session(reg, leader)
    f1 = _connect(reg, "f1")
    joined = _join(reg, f1, created["code"])
    leader.frames()

    reg.detach_link(f1)
    jc = _one(leader.frames(), "JOIN_CANCEL")
    assert jc["memberId"] == joined["memberId"]
    assert reg.sessions[created["sessionId"]].pending is None


# ── logging hygiene ────────────────────────────────────────────────


def test_logs_never_contain_secrets(caplog):
    clock = FakeClock()
    reg = _reg(clock)
    leader, follower = _connect(reg, "leader"), _connect(reg, "follower")

    with caplog.at_level(logging.DEBUG, logger="app.reader_sync"):
        sid, created, joined = _pair(reg, leader, follower)
        _send(reg, leader, _request(
            "SIGNAL", {"kind": "offer", "data": {"sdp": "TOPSECRET_SDP_BLOB"}},
            session_id=sid))
        _send(reg, follower, _request("LEAVE", session_id=sid))
        reg.tick()

    text = caplog.text
    for secret in (created["code"], created["resumeToken"], joined["resumeToken"],
                   "TOPSECRET_SDP_BLOB", sid):
        assert secret not in text


# ── shutdown ───────────────────────────────────────────────────────


def test_aclose_leaves_no_tasks_links_or_sessions():
    clock = FakeClock()
    reg = _reg(clock)
    leader, follower = _connect(reg, "leader"), _connect(reg, "follower")
    sid, _, _ = _pair(reg, leader, follower)

    async def go():
        await reg.aclose()

    _run(go())
    assert not reg.sessions
    assert not reg.links
    assert reg.live_task_count() == 0
    assert not leader.alive and not follower.alive


def test_ticker_start_and_shutdown():
    """Real-loop smoke: start() spawns exactly one ticker that aclose() reaps."""
    clock = FakeClock()
    reg = _reg(clock)

    async def go():
        reg.start()
        link = FakeLink(reg, name="x")
        reg.register_link(link)
        assert reg.live_task_count() >= 1
        await reg.aclose()
        assert reg.live_task_count() == 0
        assert not reg.links

    _run(go())


# ── MATCH (one-tap pairing) ────────────────────────────────────────


def _match(reg: SessionRegistry, link: FakeLink, request_id="m1") -> dict:
    _send(reg, link, _request("MATCH", request_id=request_id))
    ack = _one(link.frames(), "ACK")
    assert ack["request"] == "MATCH"
    return ack["data"]


def test_match_becomes_waiting_leader_when_nobody_waits():
    clock = FakeClock()
    reg = _reg(clock)
    a = _connect(reg, "a")
    data = _match(reg, a)
    assert data["role"] == "leader"
    assert data["code"].isdigit() and len(data["code"]) == 6
    assert len(reg.sessions) == 1
    # the waiting session still exposes its code — JOIN/QR fallback works
    b = _connect(reg, "b")
    _join(reg, b, data["code"])
    assert _one(a.frames(), "JOIN_REQUEST")["memberId"]


def test_match_joins_fresh_waiting_session():
    clock = FakeClock()
    reg = _reg(clock)
    a, b = _connect(reg, "a"), _connect(reg, "b")
    created = _match(reg, a)
    joined = _match(reg, b)
    assert joined["role"] == "follower"
    assert joined["sessionId"] == created["sessionId"]
    assert joined["leaderName"]
    assert _one(a.frames(), "JOIN_REQUEST")["memberId"] == joined["memberId"]


def test_match_ignores_session_older_than_window():
    clock = FakeClock()
    reg = _reg(clock)
    a = _connect(reg, "a")
    created = _match(reg, a)
    _keepalive(reg, clock, a, MATCH_WINDOW_SECONDS + 5)
    b = _connect(reg, "b")
    joined = _match(reg, b)
    assert joined["role"] == "leader"
    assert joined["sessionId"] != created["sessionId"]


def test_match_excludes_paired_and_pending_sessions():
    clock = FakeClock()
    reg = _reg(clock)
    a, b, c = _connect(reg, "a"), _connect(reg, "b"), _connect(reg, "c")
    sid, _, _ = _pair(reg, a, b)
    d = _connect(reg, "d")
    data = _match(reg, d)
    assert data["role"] == "leader"
    assert data["sessionId"] != sid
    # a session with a pending (unapproved) join is not a candidate either
    _join(reg, c, data["code"])
    e = _connect(reg, "e")
    data2 = _match(reg, e)
    assert data2["role"] == "leader"
    assert data2["sessionId"] != data["sessionId"]


def test_match_never_hijacks_a_create_session():
    """A CREATE waiting room belongs to whoever the code was shown to —
    one-tap MATCH must not poach it, even while it is fresh."""
    clock = FakeClock()
    reg = _reg(clock)
    a = _connect(reg, "a")
    created, _ = _create_session(reg, a)
    b = _connect(reg, "b")
    data = _match(reg, b)
    assert data["role"] == "leader"
    assert data["sessionId"] != created["sessionId"]
    # the code holder still joins the CREATE session normally
    c = _connect(reg, "c")
    joined = _join(reg, c, created["code"])
    assert _one(a.frames(), "JOIN_REQUEST")["memberId"] == joined["memberId"]


def test_match_ambiguous_returns_tokens_and_stays_unbound():
    clock = FakeClock()
    reg = _reg(clock)
    a, b = _connect(reg, "a"), _connect(reg, "b")
    # Two 'match'-origin waiters can only coexist through a leader
    # outage: B's MATCH runs while A's leader link is dead, then A
    # resumes on a fresh link — two eligible candidates.
    ca = _match(reg, a)
    reg.detach_link(a)
    cb = _match(reg, b)
    assert cb["role"] == "leader" and cb["sessionId"] != ca["sessionId"]
    back = _connect(reg, "a2")
    _send(reg, back, _request(
        "RESUME",
        {"memberId": ca["memberId"], "resumeToken": ca["resumeToken"]},
        session_id=ca["sessionId"]))
    assert _one(back.frames(), "ACK")["request"] == "RESUME"

    c = _connect(reg, "c")
    data = _match(reg, c)
    assert data["ambiguous"] is True
    assert len(data["candidates"]) == 2
    # candidates carry an opaque pick token and NEVER the human code —
    # leaking it would let a picker consume the code a real partner is
    # still typing.
    assert all("code" not in cand for cand in data["candidates"])
    assert all(isinstance(cand["id"], str) and cand["id"]
               for cand in data["candidates"])
    assert {cand["leaderName"] for cand in data["candidates"]} == {"a", "b"}
    assert c.member is None

    # the picker re-MATCHes with the chosen token
    pick = data["candidates"][0]["id"]   # oldest first == session A
    _send(reg, c, _request("MATCH", {"pick": pick}, request_id="m2"))
    ack = _one(c.frames(), "ACK")
    assert ack["request"] == "MATCH"
    assert ack["data"]["role"] == "follower"
    assert ack["data"]["sessionId"] == ca["sessionId"]
    assert _one(back.frames(), "JOIN_REQUEST")["memberId"] == c.member.id


def test_match_pick_rejects_unknown_or_gone_token():
    clock = FakeClock()
    reg = _reg(clock)
    c = _connect(reg, "c")
    _send(reg, c, _request("MATCH", {"pick": "no-such-token"}, request_id="m1"))
    rej = _one(c.frames(), "REJECT")
    assert rej["reason"] == REASON_BAD_CODE

    # a token for a session that got paired in the meantime is stale
    a, b = _connect(reg, "a"), _connect(reg, "b")
    ca = _match(reg, a)
    _match(reg, b)   # pairs with A
    _send(reg, c, _request(
        "MATCH", {"pick": reg.sessions[ca["sessionId"]].match_token},
        request_id="m2"))
    rej = _one(c.frames(), "REJECT")
    assert rej["reason"] == REASON_BAD_CODE


def test_match_excludes_session_with_dead_leader():
    clock = FakeClock()
    reg = _reg(clock)
    a = _connect(reg, "a")
    _match(reg, a)
    reg.detach_link(a)
    b = _connect(reg, "b")
    assert _match(reg, b)["role"] == "leader"


def test_match_rejected_when_already_bound():
    clock = FakeClock()
    reg = _reg(clock)
    a, b = _connect(reg, "a"), _connect(reg, "b")
    sid, _, _ = _pair(reg, a, b)
    _send(reg, a, _request("MATCH", request_id="m", session_id=sid))
    assert _one(a.frames(), "REJECT")["reason"] == REASON_ALREADY_BOUND
