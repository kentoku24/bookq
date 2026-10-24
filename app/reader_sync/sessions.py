"""Session registry for dual-reader pairing (#78 / S03).

Parent issue #75 §3.5: 6-digit code (5min TTL, single-use on success), one
leader + one follower slot, leader approval, 5min resume window, heartbeat
5s / no-response 15s, explicit LEAVE ends the session.

Design contract:

- Transport-free. A ``PeerLink`` is a bounded outbound queue plus a liveness
  stamp; ``transport.py`` binds it to a starlette WebSocket and tests bind it
  to nothing. All dispatch is synchronous — sockets never appear here.
- Clock is injectable (``clock`` callable); ``tick(now)`` performs every
  time-driven transition (heartbeat PING, no-response close, code expiry,
  resume expiry), so fake-clock tests need no sleeping.
- Registry state lives only in app.state (single worker); ``aclose()``
  leaves zero links, sessions, or tracked tasks.
- Logging: only server-issued opaque ids, event names, and reason codes.
  Never log codes, resume tokens, frame payloads, SDP/ICE, or folder names.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field

from app.reader_sync import protocol

logger = logging.getLogger(__name__)

# ── wire constants (parent #75 §3.5 initial values) ────────────────
HEARTBEAT_INTERVAL = 5.0       # server PING cadence
NO_RESPONSE_TIMEOUT = 15.0     # silence -> disconnect
CODE_TTL_SECONDS = 300.0       # pairing code lifetime
RESUME_WINDOW_SECONDS = 300.0  # member may reattach within this window
SEND_QUEUE_MAX = 64            # bounded per-link outbound queue (frames)
CODE_DIGITS = 6

WS_CLOSE_POLICY = 1008         # policy violation
WS_CLOSE_TOO_BIG = 1009        # frame over the 64KiB cap
WS_CLOSE_GOING_AWAY = 1001     # shutdown / server going away

# ── REJECT reasons (machine-readable; stable for clients) ──────────
REASON_MALFORMED = "malformed"
REASON_NO_SESSION = "no_session"            # session-scoped frame, no member bound
REASON_WRONG_SESSION = "wrong_session"      # frame sessionId != bound session
REASON_ALREADY_BOUND = "already_bound"      # CREATE/JOIN/RESUME while a member
REASON_NOT_PAIRED = "not_paired"            # needs an established pair
REASON_NOT_LEADER = "not_leader"
REASON_NO_PENDING = "no_pending"            # APPROVE/REJECT with nobody waiting
REASON_BAD_CODE = "bad_code"
REASON_CODE_CONSUMED = "code_consumed"
REASON_CODE_EXPIRED = "code_expired"
REASON_BAD_RESUME = "bad_resume"            # unknown session/member/token
REASON_RESUME_EXPIRED = "resume_expired"
REASON_RESUME_IN_USE = "resume_in_use"      # member already has a live link
REASON_PEER_OFFLINE = "peer_offline"
REASON_UNIMPLEMENTED = "unimplemented"      # S05+ request types

# ── END reasons ────────────────────────────────────────────────────
END_REJECTED = "rejected"          # leader REJECTed the pending join
END_LEFT = "left"                  # explicit LEAVE by any member/candidate
END_CODE_EXPIRED = "code_expired"  # nobody joined before the TTL
END_RESUME_EXPIRED = "resume_expired"
END_SHUTDOWN = "shutdown"

# request types handled at session scope but not yet implemented (S05)
_DEFERRED_TYPES = frozenset({
    "TURN", "GOTO", "SET_OFFSET", "SWAP_SIDES", "OPEN_WORK", "READY", "APPLIED",
})

_TRANSPORT_REPORT = frozenset({"up", "down"})


def _new_id() -> str:
    return secrets.token_hex(8)


def _new_token() -> str:
    return secrets.token_urlsafe(24)


def _new_code() -> str:
    return f"{secrets.randbelow(1_000_000):0{CODE_DIGITS}d}"


class PeerLink:
    """One transport attachment. Synchronous, socket-free.

    ``_enqueue`` puts an encoded frame on the bounded outq; a full queue means
    a slow or dead peer — the link is closed rather than blocking the sender.
    ``request_transport_close`` is the transport hook (WsLink overrides it to
    close the socket; test fakes record it).
    """

    def __init__(self, registry: "SessionRegistry"):
        self.registry = registry
        self.id = _new_id()
        self.user: dict | None = None   # populated by the transport layer
        self.member: Member | None = None
        self.alive = True
        self.last_rx = registry._now()
        self.next_ping_due = self.last_rx + registry.heartbeat_interval
        self.ping_seq = 0
        self.outq: asyncio.Queue = asyncio.Queue(maxsize=registry.send_queue_max)

    # -- outbound ---------------------------------------------------

    def _enqueue(self, raw: str) -> bool:
        try:
            self.outq.put_nowait(raw)
            return True
        except asyncio.QueueFull:
            return False

    def send_obj(self, obj: dict) -> bool:
        """Queue one frame; overflow disconnects the peer (bounded send)."""
        if not self.alive:
            return False
        try:
            raw = protocol.encode_frame(obj).decode("utf-8")
        except protocol.ProtocolError:
            logger.warning("reader-sync link=%s drop oversized outbound frame", self.id)
            return False
        if not self._enqueue(raw):
            self.registry.close_link(self, "send_queue_full", code=WS_CLOSE_POLICY)
            return False
        return True

    def request_transport_close(self, code: int, reason: str) -> None:
        """Transport hook: close the underlying channel. Overridden by the
        WS binding; the base records nothing (fake links capture it)."""


@dataclass
class Member:
    """A session occupant. Never implicitly replaced: resume reattaches the
    same Member to a new link; there is no take-over path."""
    id: str
    role: str                    # "leader" | "follower"
    name: str
    resume_token: str
    session: "PairSession | None" = None
    link: PeerLink | None = None
    status: str = "active"       # active | suspended | offline | pending
    offline_since: float | None = None
    dc_state: str = "down"       # reported webrtc-dc status: up | down


@dataclass
class PairSession:
    id: str
    code: str
    code_expires_at: float
    code_consumed: bool = False
    members: dict = field(default_factory=dict)   # member_id -> Member
    pending: Member | None = None                  # joined, awaiting APPROVE
    revision: int = 0
    work_epoch: int = 0
    manifest_version: int = 0
    phase: str = "waiting"
    spread_id: str | None = None
    offset: int = 0
    leader_side: str = "right"
    data_plane: str = "server-ws"                  # server-ws | webrtc-dc

    @property
    def paired(self) -> bool:
        return self.pending is None and len(self.members) == 2

    def leader(self) -> Member | None:
        for m in self.members.values():
            if m.role == "leader":
                return m
        return None


class SessionRegistry:
    """All pairing/session state. Lives on app.state; single worker only."""

    def __init__(self, *, clock=None,
                 heartbeat_interval: float = HEARTBEAT_INTERVAL,
                 no_response_timeout: float = NO_RESPONSE_TIMEOUT,
                 code_ttl: float = CODE_TTL_SECONDS,
                 resume_window: float = RESUME_WINDOW_SECONDS,
                 send_queue_max: int = SEND_QUEUE_MAX):
        self._clock = clock or time.monotonic
        self.heartbeat_interval = heartbeat_interval
        self.no_response_timeout = no_response_timeout
        self.code_ttl = code_ttl
        self.resume_window = resume_window
        self.send_queue_max = send_queue_max

        self.sessions: dict[str, PairSession] = {}
        self.links: dict[str, PeerLink] = {}
        self._by_code: dict[str, PairSession] = {}

        self.closed = False
        self._tasks: set[asyncio.Task] = set()
        self._ticker: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None

    # ── plumbing ----------------------------------------------------

    def _now(self) -> float:
        return self._clock()

    def live_task_count(self) -> int:
        return len(self._tasks)

    def track_task(self, t: asyncio.Task) -> None:
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def _wake_ticker(self) -> None:
        if self._wake is not None:
            self._wake.set()

    # ── link lifecycle ───────────────────────────────────────────────

    def register_link(self, link: PeerLink) -> None:
        """Attach a fresh transport; greets it with HELLO."""
        self.links[link.id] = link
        link.last_rx = self._now()
        link.next_ping_due = link.last_rx + self.heartbeat_interval
        link.send_obj({"v": protocol.PROTOCOL_VERSION, "type": "HELLO", "link": link.id})
        logger.debug("reader-sync link=%s registered", link.id)
        self._wake_ticker()

    def detach_link(self, link: PeerLink) -> None:
        """Transport reports the socket is gone. Membership is kept for the
        resume window (approved member -> offline) unless it was only a
        pending candidate (dropped, leader notified)."""
        if link.id not in self.links and not link.alive:
            return
        self.links.pop(link.id, None)
        link.alive = False
        member = link.member
        link.member = None
        if member is None:
            logger.debug("reader-sync link=%s detached (anonymous)", link.id)
            return
        member.link = None
        session = member.session
        if session is None:
            return
        if session.pending is member:
            session.pending = None
            leader = session.leader()
            if leader is not None and leader.link is not None:
                leader.link.send_obj({
                    "v": protocol.PROTOCOL_VERSION, "type": "JOIN_CANCEL",
                    "sessionId": session.id, "memberId": member.id})
            logger.debug("reader-sync link=%s pending candidate lost member=%s",
                         link.id, member.id)
            return
        member.status = "offline"
        member.offline_since = self._now()
        logger.debug("reader-sync link=%s member=%s offline", link.id, member.id)
        self._broadcast_state(session)

    def close_link(self, link: PeerLink, reason: str, code: int = WS_CLOSE_POLICY) -> None:
        """Registry-initiated close: detach membership + ask transport to end."""
        was_tracked = link.id in self.links
        if link.alive or was_tracked:
            self.detach_link(link)
            link.alive = False
            try:
                link.request_transport_close(code, reason)
            except Exception:
                logger.debug("reader-sync link=%s transport close raised", link.id)
            if was_tracked:
                logger.debug("reader-sync link=%s closed reason=%s", link.id, reason)
        self._wake_ticker()

    # ── inbound dispatch ─────────────────────────────────────────────

    def handle_raw(self, link: PeerLink, raw) -> None:
        """One inbound frame. Stamps liveness, enforces the 64KiB cap via
        protocol.parse_frame, then routes PING/PONG early (bare-shape frames
        are legal for heartbeat) before full request validation."""
        if not link.alive or self.closed:
            return
        link.last_rx = self._now()
        try:
            frame = protocol.parse_frame(raw)
        except protocol.ProtocolError as e:
            if e.code == protocol.ERR_SIZE:
                self.close_link(link, "frame_too_big", code=WS_CLOSE_TOO_BIG)
            else:
                self._reject(link, None, None, REASON_MALFORMED)
            return
        t = frame.get("type")
        if t == "PONG":
            return  # liveness stamp above is the whole job
        if t == "PING":
            seq = frame.get("seq")
            if seq is None and isinstance(frame.get("payload"), dict):
                seq = frame["payload"].get("seq")
            pong = {"v": protocol.PROTOCOL_VERSION, "type": "PONG"}
            if seq is not None:
                pong["seq"] = seq
            link.send_obj(pong)
            return
        try:
            req = protocol.parse_request(frame)
        except protocol.ProtocolError:
            self._reject(link,
                         frame.get("requestId") if isinstance(frame.get("requestId"), str) else None,
                         t if isinstance(t, str) else None,
                         REASON_MALFORMED)
            return
        self._dispatch(link, req)

    def _dispatch(self, link: PeerLink, req: dict) -> None:
        t = req["type"]
        member = link.member

        if t in ("CREATE", "JOIN", "RESUME"):
            if member is not None:
                self._reject(link, req, t, REASON_ALREADY_BOUND)
                return
            if t == "CREATE":
                self._create(link, req)
            elif t == "JOIN":
                self._join(link, req)
            else:
                self._resume(link, req)
            return

        # every other request requires a bound member of the addressed session
        if member is None or member.session is None:
            self._reject(link, req, t, REASON_NO_SESSION)
            return
        session = member.session
        if req["sessionId"] != session.id:
            self._reject(link, req, t, REASON_WRONG_SESSION)
            return

        if session.pending is member:
            # unapproved candidate: no state flows to it; only bail out
            if t == "LEAVE":
                self._end_session(session, END_LEFT)
            else:
                self._reject(link, req, t, REASON_NOT_PAIRED)
            return

        if t == "LEAVE":
            self._end_session(session, END_LEFT)
        elif t in ("APPROVE", "REJECT"):
            self._approve_or_reject(link, req, session, member, approve=(t == "APPROVE"))
        elif t == "SET_ACTIVITY":
            self._set_activity(link, req, session, member)
        elif t == "SIGNAL":
            self._signal(link, req, session, member)
        elif t == "TRANSPORT_STATE":
            self._transport_state(link, req, session, member)
        elif t in _DEFERRED_TYPES:
            if not session.paired:
                self._reject(link, req, t, REASON_NOT_PAIRED)
            else:
                self._reject(link, req, t, REASON_UNIMPLEMENTED)
        else:
            self._reject(link, req, t, REASON_MALFORMED)

    # ── session ops ──────────────────────────────────────────────────

    def _ack(self, link: PeerLink, req: dict, data: dict | None = None,
             session_id=None) -> None:
        link.send_obj({
            "v": protocol.PROTOCOL_VERSION, "type": "ACK",
            "requestId": req["requestId"], "request": req["type"],
            "sessionId": session_id, "data": data or {}})

    def _reject(self, link: PeerLink, req: dict | None, rtype: str | None,
                reason: str, session_id=None) -> None:
        link.send_obj({
            "v": protocol.PROTOCOL_VERSION, "type": "REJECT",
            "requestId": req["requestId"] if req else None,
            "request": (req["type"] if req else rtype),
            "reason": reason, "sessionId": session_id})

    def _create(self, link: PeerLink, req: dict) -> None:
        code = _new_code()
        while code in self._by_code:
            code = _new_code()
        session = PairSession(
            id=_new_id(), code=code,
            code_expires_at=self._now() + self.code_ttl)
        user = link.user or {}
        member = Member(id=_new_id(), role="leader",
                        name=user.get("name") or "", resume_token=_new_token(),
                        session=session, link=link)
        session.members[member.id] = member
        link.member = member
        self.sessions[session.id] = session
        self._by_code[code] = session
        self._ack(link, req, {
            "sessionId": session.id, "code": code, "memberId": member.id,
            "role": "leader", "resumeToken": member.resume_token,
            "expiresInSec": int(self.code_ttl)}, session_id=session.id)
        self._broadcast_state(session)
        logger.info("reader-sync link=%s member=%s created session", link.id, member.id)
        self._wake_ticker()

    def _join(self, link: PeerLink, req: dict) -> None:
        code = req["payload"]["code"]
        session = self._by_code.get(code)
        if session is None:
            self._reject(link, req, "JOIN", REASON_BAD_CODE)
            return
        if session.code_consumed:
            self._reject(link, req, "JOIN", REASON_CODE_CONSUMED,
                         session_id=session.id)
            return
        if self._now() >= session.code_expires_at:
            self._reject(link, req, "JOIN", REASON_CODE_EXPIRED,
                         session_id=session.id)
            return
        user = link.user or {}
        member = Member(id=_new_id(), role="follower",
                        name=user.get("name") or "", resume_token=_new_token(),
                        session=session, link=link, status="pending")
        session.pending = member
        session.code_consumed = True   # single-use on success
        link.member = member
        self._ack(link, req, {
            "sessionId": session.id, "memberId": member.id,
            "role": "follower", "resumeToken": member.resume_token},
            session_id=session.id)
        leader = session.leader()
        if leader is not None and leader.link is not None:
            leader.link.send_obj({
                "v": protocol.PROTOCOL_VERSION, "type": "JOIN_REQUEST",
                "sessionId": session.id, "memberId": member.id,
                "name": member.name})
        logger.info("reader-sync link=%s member=%s pending join", link.id, member.id)

    def _approve_or_reject(self, link: PeerLink, req: dict, session: PairSession,
                           member: Member, *, approve: bool) -> None:
        if member.role != "leader":
            self._reject(link, req, req["type"], REASON_NOT_LEADER,
                         session_id=session.id)
            return
        pending = session.pending
        if pending is None:
            self._reject(link, req, req["type"], REASON_NO_PENDING,
                         session_id=session.id)
            return
        if not approve:
            self._end_session(session, END_REJECTED)
            return
        session.pending = None
        pending.status = "active"
        session.members[pending.id] = pending
        self._ack(link, req, session_id=session.id)
        self._broadcast_state(session)
        logger.info("reader-sync member=%s approved member=%s", member.id, pending.id)

    def _resume(self, link: PeerLink, req: dict) -> None:
        payload = req["payload"]
        session = self.sessions.get(req["sessionId"] or "")
        member_id = payload.get("memberId")
        token = payload.get("resumeToken")
        member = session.members.get(member_id) if session else None
        if member is None or not isinstance(token, str) or member.resume_token != token:
            self._reject(link, req, "RESUME", REASON_BAD_RESUME)
            return
        if member.link is not None and member.link.alive:
            self._reject(link, req, "RESUME", REASON_RESUME_IN_USE,
                         session_id=session.id)
            return
        if member.offline_since is not None and \
                self._now() - member.offline_since >= self.resume_window:
            self._reject(link, req, "RESUME", REASON_RESUME_EXPIRED,
                         session_id=session.id)
            return
        member.link = link
        link.member = member
        member.status = "active"
        member.offline_since = None
        self._ack(link, req, {
            "sessionId": session.id, "memberId": member.id, "role": member.role,
            "resumeToken": member.resume_token}, session_id=session.id)
        self._broadcast_state(session)
        logger.info("reader-sync link=%s member=%s resumed", link.id, member.id)
        self._wake_ticker()

    def _set_activity(self, link: PeerLink, req: dict, session: PairSession,
                      member: Member) -> None:
        state = req["payload"].get("state")
        if state not in ("active", "suspended"):
            self._reject(link, req, "SET_ACTIVITY", REASON_MALFORMED,
                         session_id=session.id)
            return
        if member.status != "offline":
            member.status = state
        self._ack(link, req, session_id=session.id)
        self._broadcast_state(session)

    def _signal(self, link: PeerLink, req: dict, session: PairSession,
                member: Member) -> None:
        if not session.paired:
            self._reject(link, req, "SIGNAL", REASON_NOT_PAIRED,
                         session_id=session.id)
            return
        other = next((m for m in session.members.values() if m is not member), None)
        if other is None or other.link is None or not other.link.alive:
            self._reject(link, req, "SIGNAL", REASON_PEER_OFFLINE,
                         session_id=session.id)
            return
        other.link.send_obj({
            "v": protocol.PROTOCOL_VERSION, "type": "SIGNAL",
            "sessionId": session.id, "from": member.role,
            "kind": req["payload"]["kind"], "data": req["payload"].get("data")})
        self._ack(link, req, session_id=session.id)

    def _transport_state(self, link: PeerLink, req: dict, session: PairSession,
                         member: Member) -> None:
        state = req["payload"].get("state")
        if state not in _TRANSPORT_REPORT:
            self._reject(link, req, "TRANSPORT_STATE", REASON_MALFORMED,
                         session_id=session.id)
            return
        member.dc_state = state
        self._ack(link, req, session_id=session.id)
        # Plane switch happens only at a revision boundary: recompute, and if
        # it changed, the next STATE revision carries it for both members.
        new_plane = "webrtc-dc" if (
            session.paired
            and all(m.dc_state == "up" for m in session.members.values())) \
            else "server-ws"
        if new_plane != session.data_plane:
            session.data_plane = new_plane
            self._broadcast_state(session)
            self._broadcast(session, {
                "v": protocol.PROTOCOL_VERSION, "type": "TRANSPORT_STATE",
                "sessionId": session.id, "dataPlane": session.data_plane,
                "revision": session.revision})
            logger.info("reader-sync member=%s transport=%s rev=%d",
                        member.id, session.data_plane, session.revision)

    # ── broadcast / teardown ─────────────────────────────────────────

    def _state_frame(self, session: PairSession) -> dict:
        session.revision += 1
        return {
            "v": protocol.PROTOCOL_VERSION, "type": "STATE",
            "sessionId": session.id, "revision": session.revision,
            "workEpoch": session.work_epoch,
            "manifestVersion": session.manifest_version,
            "phase": session.phase, "spreadId": session.spread_id,
            "offset": session.offset, "leaderSide": session.leader_side,
            "presence": {
                m.id: {"role": m.role, "status": m.status, "name": m.name}
                for m in session.members.values()},
            "dataPlane": session.data_plane,
        }

    def _broadcast(self, session: PairSession, frame: dict) -> None:
        for m in session.members.values():
            if m.link is not None:
                m.link.send_obj(frame)

    def _broadcast_state(self, session: PairSession) -> None:
        self._broadcast(session, self._state_frame(session))

    def _end_session(self, session: PairSession, reason: str) -> None:
        if session.id not in self.sessions:
            return
        self.sessions.pop(session.id, None)
        if self._by_code.get(session.code) is session:
            self._by_code.pop(session.code, None)
        frame = {"v": protocol.PROTOCOL_VERSION, "type": "END",
                 "sessionId": session.id, "reason": reason}
        targets = list(session.members.values())
        if session.pending is not None:
            targets.append(session.pending)
            session.pending = None
        for m in targets:
            if m.link is not None:
                m.link.send_obj(frame)
                m.link.member = None
            m.link = None
            m.session = None
        session.members.clear()
        logger.info("reader-sync session end reason=%s members=%d", reason, len(targets))
        self._wake_ticker()

    # ── clock-driven transitions ─────────────────────────────────────

    def tick(self, now: float | None = None) -> None:
        """All time-based transitions. Called by the real ticker and by
        fake-clock tests directly."""
        if self.closed:
            return
        now = self._now() if now is None else now
        for link in list(self.links.values()):
            if not link.alive:
                continue
            if now - link.last_rx >= self.no_response_timeout:
                self.close_link(link, "no_response", code=WS_CLOSE_POLICY)
                continue
            if now >= link.next_ping_due:
                link.ping_seq += 1
                link.send_obj({"v": protocol.PROTOCOL_VERSION, "type": "PING",
                               "seq": link.ping_seq})
                link.next_ping_due = now + self.heartbeat_interval
        for session in list(self.sessions.values()):
            if not session.paired and now >= session.code_expires_at:
                self._end_session(session, END_CODE_EXPIRED)
                continue
            if any(m.status == "offline" and m.offline_since is not None
                   and now - m.offline_since >= self.resume_window
                   for m in session.members.values()):
                self._end_session(session, END_RESUME_EXPIRED)

    def next_wakeup_delay(self, fallback: float = 60.0) -> float:
        """Seconds until the next due tick event; the ticker sleeps this long
        so heartbeat cadence is exact rather than poll-quantized."""
        now = self._now()
        dues: list[float] = []
        for link in self.links.values():
            if not link.alive:
                continue
            dues.append(link.next_ping_due)
            dues.append(link.last_rx + self.no_response_timeout)
        for s in self.sessions.values():
            if not s.paired:
                dues.append(s.code_expires_at)
            for m in s.members.values():
                if m.status == "offline" and m.offline_since is not None:
                    dues.append(m.offline_since + self.resume_window)
        if not dues:
            return fallback
        return max(0.05, min(dues) - now)

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the single ticker task. Idempotent; must run inside a loop."""
        if self._ticker is not None or self.closed:
            return
        self._wake = asyncio.Event()
        self._ticker = asyncio.create_task(self._ticker_loop(),
                                           name="reader-sync-ticker")
        self.track_task(self._ticker)

    async def _ticker_loop(self) -> None:
        while True:
            # clear *before* tick: a registration landing mid-tick stays set
            # and triggers an immediate re-evaluation next pass.
            self._wake.clear()
            self.tick()
            try:
                await asyncio.wait_for(self._wake.wait(),
                                       timeout=self.next_wakeup_delay())
            except asyncio.TimeoutError:
                pass

    async def aclose(self) -> None:
        """Teardown: cancel ticker, close every link, drop all sessions, then
        wait out tracked close/drain tasks. After this, live_task_count()==0
        and no sockets or sessions survive."""
        self.closed = True
        if self._ticker is not None:
            self._ticker.cancel()
            try:
                await self._ticker
            except (asyncio.CancelledError, Exception):
                pass
            self._ticker = None
        for link in list(self.links.values()):
            self.close_link(link, END_SHUTDOWN, code=WS_CLOSE_GOING_AWAY)
        self.links.clear()
        for session in list(self.sessions.values()):
            for m in list(session.members.values()) + ([session.pending] if session.pending else []):
                if m.link is not None:
                    m.link.member = None
                m.link = None
                m.session = None
            session.members.clear()
            session.pending = None
        self.sessions.clear()
        self._by_code.clear()
        pending = [t for t in self._tasks if t is not asyncio.current_task()]
        if pending:
            done, still = await asyncio.wait(pending, timeout=2.0)
            for t in still:
                t.cancel()
            if still:
                await asyncio.gather(*still, return_exceptions=True)
            for t in done:
                try:
                    t.result()
                except (asyncio.CancelledError, Exception):
                    pass
            self._tasks.difference_update(pending)
