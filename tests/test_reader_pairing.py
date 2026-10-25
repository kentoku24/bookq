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
    UI_SYNC_RATE_MAX,
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


# ── UI_SYNC ephemeral browse-mirror relay ──────────────────────────


def _ui_payload(**kw):
    p = {"view": "browse", "path": "group/episodes", "top": 120.5,
         "viewH": 600, "mode": "tile"}
    p.update(kw)
    return p


def test_ui_sync_relayed_between_paired_members():
    clock = FakeClock()
    reg = _reg(clock)
    leader, follower = _connect(reg, "leader"), _connect(reg, "follower")
    sid, _, _ = _pair(reg, leader, follower)

    _send(reg, leader, _request(
        "UI_SYNC", _ui_payload(), session_id=sid, request_id="u1"))
    fframes = follower.frames()
    ui = _one(fframes, "UI_SYNC")
    assert ui["from"] == "leader"
    assert ui["payload"] == _ui_payload()
    assert ui["sessionId"] == sid
    # fire-and-forget: the sender never consumes an ACK, so none is sent
    assert _of_type(leader.frames(), "ACK") == []

    # the relay is bidirectional — follower's scroll reaches the leader too
    _send(reg, follower, _request(
        "UI_SYNC", _ui_payload(top=700), session_id=sid, request_id="u2"))
    lframes = leader.frames()
    ui2 = _one(lframes, "UI_SYNC")
    assert ui2["from"] == "follower" and ui2["payload"]["top"] == 700


def test_ui_sync_requires_pair_and_live_peer():
    clock = FakeClock()
    reg = _reg(clock)
    leader = _connect(reg, "leader")
    created, _ = _create_session(reg, leader)
    _send(reg, leader, _request(
        "UI_SYNC", _ui_payload(), session_id=created["sessionId"]))
    rej = _one(leader.frames(), "REJECT")
    assert rej["reason"] == REASON_NOT_PAIRED

    f1 = _connect(reg, "f1")
    _join(reg, f1, created["code"])
    _send(reg, leader, _request("APPROVE", {"memberId": f1.member.id},
                                session_id=created["sessionId"]))
    leader.frames(); f1.frames()
    reg.detach_link(f1)
    # peer offline is transient for an ephemeral scroll frame — dropped
    # silently rather than spamming a REJECT + client notification per frame
    _send(reg, leader, _request(
        "UI_SYNC", _ui_payload(), session_id=created["sessionId"]))
    assert _of_type(leader.frames(), "REJECT") == []


def test_ui_sync_malformed_payloads_rejected():
    clock = FakeClock()
    reg = _reg(clock)
    leader, follower = _connect(reg, "leader"), _connect(reg, "follower")
    sid, _, _ = _pair(reg, leader, follower)

    bad = (
        _ui_payload(view="overview"),            # unknown view
        _ui_payload(path=123),                   # path must be str|null
        _ui_payload(top=-1),                     # negative scroll
        _ui_payload(top="10"),                   # top must be a number
        _ui_payload(top=float("nan")),           # non-finite
        _ui_payload(viewH=0),                    # must be > 0
        _ui_payload(viewH=-4),
        _ui_payload(mode="grid"),                # unknown mode
    )
    for i, payload in enumerate(bad):
        _send(reg, leader, _request(
            "UI_SYNC", payload, session_id=sid, request_id=f"m{i}"))
        rej = _one(leader.frames(), "REJECT")
        assert rej["reason"] == REASON_MALFORMED, payload
        assert rej["requestId"] == f"m{i}"
    # none of them reached the peer
    assert _of_type(follower.frames(), "UI_SYNC") == []


def test_ui_sync_optional_fields_and_root_path():
    """path null (root) and omitted mode are valid minimal frames."""
    clock = FakeClock()
    reg = _reg(clock)
    leader, follower = _connect(reg, "leader"), _connect(reg, "follower")
    sid, _, _ = _pair(reg, leader, follower)
    _send(reg, leader, _request(
        "UI_SYNC", {"view": "browse", "path": None, "top": 0, "viewH": 800},
        session_id=sid))
    ui = _one(follower.frames(), "UI_SYNC")
    assert ui["payload"]["path"] is None and ui["payload"]["top"] == 0


def test_ui_sync_rate_cap_silently_drops_burst():
    clock = FakeClock()
    reg = _reg(clock)
    leader, follower = _connect(reg, "leader"), _connect(reg, "follower")
    sid, _, _ = _pair(reg, leader, follower)

    for i in range(UI_SYNC_RATE_MAX + 30):
        _send(reg, leader, _request(
            "UI_SYNC", _ui_payload(top=i),
            session_id=sid, request_id=f"b{i}"))
    relayed = _of_type(follower.frames(), "UI_SYNC")
    assert len(relayed) == UI_SYNC_RATE_MAX
    # no ACKs at all — UI_SYNC is fire-and-forget on both directions
    acks = _of_type(leader.frames(), "ACK")
    assert acks == []

    # a new 1s window relays again
    clock.advance(1.1)
    _send(reg, leader, _request(
        "UI_SYNC", _ui_payload(top=999), session_id=sid, request_id="after"))
    ui = _one(follower.frames(), "UI_SYNC")
    assert ui["payload"]["top"] == 999


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
