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
import inspect
import logging
import secrets
import time
from dataclasses import dataclass, field

from app.reader_sync import protocol
from app.reader_sync import state as rstate

logger = logging.getLogger(__name__)

# ── wire constants (parent #75 §3.5 initial values) ────────────────
HEARTBEAT_INTERVAL = 5.0       # server PING cadence
NO_RESPONSE_TIMEOUT = 15.0     # silence -> disconnect
CODE_TTL_SECONDS = 300.0       # pairing code lifetime
RESUME_WINDOW_SECONDS = 300.0  # member may reattach within this window
MATCH_WINDOW_SECONDS = 120.0   # only sessions this fresh are auto-joinable
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

# ── END reasons ────────────────────────────────────────────────────
END_REJECTED = "rejected"          # leader REJECTed the pending join
END_LEFT = "left"                  # explicit LEAVE by any member/candidate
END_CODE_EXPIRED = "code_expired"  # nobody joined before the TTL
END_RESUME_EXPIRED = "resume_expired"
END_SHUTDOWN = "shutdown"

# S05 (#80): sequenced request types — the vocabulary the authoritative
# sequencer/state layer owns (app/reader_sync/state.py).
_SEQUENCED_TYPES = rstate.SEQUENCED_TYPES

# requestId dedup retention: full outcomes for the last DEDUP_WINDOW keys,
# evicted keys remembered as tombstones for the workEpoch's whole lifetime
# ("dedup保持期間外の古い要求は再適用せず拒否" — §3.4).
DEDUP_WINDOW = 256
DEDUP_TOMBSTONE_MAX = 8192

_TRANSPORT_REPORT = frozenset({"up", "down"})

# UI_SYNC is an ephemeral high-frequency peer relay (browse scroll). Cap it
# per member so a buggy sender can't fill the peer's bounded outq (queue-full
# closes the *recipient's* link) — excess frames are dropped silently.
UI_SYNC_WINDOW_SEC = 1.0
UI_SYNC_RATE_MAX = 50


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
        self.connection = None          # transport's connection object (WS) —
                                        # lets the work resolver reach
                                        # connection.app.state without the
                                        # registry importing starlette
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
    last_applied: dict | None = None  # last APPLIED report {requestId, revision}
    ui_sync_window_start: float = 0.0  # 1s rate window for UI_SYNC relay
    ui_sync_count: int = 0


@dataclass
class PairSession:
    id: str
    code: str
    code_expires_at: float
    created_at: float = 0.0
    # 'create' sessions are reserved for the code holder (classic JOIN);
    # 'match' sessions are open one-tap waiters and the only MATCH
    # candidates — keeps code-shared pairing immune to drive-by joins.
    origin: str = "create"
    match_token: str = ""
    code_consumed: bool = False
    members: dict = field(default_factory=dict)   # member_id -> Member
    pending: Member | None = None                  # joined, awaiting APPROVE
    revision: int = 0
    work_epoch: int = 0
    manifest_version: int = 0
    phase: str = "waiting"
    spread_id: str | None = None
    offset: int = 0
    leader_side: str = "left"
    data_plane: str = "server-ws"                  # server-ws | webrtc-dc
    # ── S05 committed work state ────────────────────────────────────
    manifest: dict | None = None        # committed shared manifest (S04 shape)
    plan: list | None = None            # Spread objects parallel to manifest
    ready: set = field(default_factory=set)      # member ids READY on manifest
    dedup: dict = field(default_factory=dict)    # "role:requestId" -> response
    dedup_seen: dict = field(default_factory=dict)  # evicted keys (tombstones)
    load_seq: int = 0                   # generation counter for load jobs
    pending_load: dict | None = None    # in-flight resolve job descriptor
    selecting: bool = False             # leader is browsing the work list
    apply_seq: int = 0                  # server-side apply-order counter
    apply_log: list = field(default_factory=list)  # accepted-op records

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
                 send_queue_max: int = SEND_QUEUE_MAX,
                 match_window: float = MATCH_WINDOW_SECONDS,
                 work_resolver=None):
        self._clock = clock or time.monotonic
        # S05: OPEN_WORK / SET_OFFSET manifest resolution — a callable
        # ``resolver(link, job) -> manifest dict | awaitable``. Injected so
        # this module stays transport/IO-free; main.py wires catalog.
        self.work_resolver = work_resolver
        self.heartbeat_interval = heartbeat_interval
        self.no_response_timeout = no_response_timeout
        self.code_ttl = code_ttl
        self.resume_window = resume_window
        self.send_queue_max = send_queue_max
        self.match_window = match_window

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
        # S05: a detached member must READY again after RESUME (manifest is
        # re-sent unicast), and its dc report is dead with the link — a dead
        # member may not hold the webrtc-dc plane up.
        session.ready.discard(member.id)
        member.dc_state = "down"
        flipped = self._eval_data_plane(session)
        logger.debug("reader-sync link=%s member=%s offline", link.id, member.id)
        self._broadcast_state(session)
        if flipped:
            self._broadcast(session, {
                "v": protocol.PROTOCOL_VERSION, "type": "TRANSPORT_STATE",
                "sessionId": session.id, "dataPlane": session.data_plane,
                "revision": session.revision})

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

        if t in ("CREATE", "JOIN", "RESUME", "MATCH"):
            if member is not None:
                self._reject(link, req, t, REASON_ALREADY_BOUND)
                return
            if t == "CREATE":
                self._create(link, req)
            elif t == "JOIN":
                self._join(link, req)
            elif t == "MATCH":
                self._match(link, req)
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
        elif t == "UI_SYNC":
            self._ui_sync(link, req, session, member)
        elif t == "TRANSPORT_STATE":
            self._transport_state(link, req, session, member)
        elif t in _SEQUENCED_TYPES:
            if not session.paired:
                self._reject(link, req, t, REASON_NOT_PAIRED)
            else:
                self._sequenced_op(link, req, session, member)
        else:
            self._reject(link, req, t, REASON_MALFORMED)

    # ── session ops ──────────────────────────────────────────────────

    def _ack(self, link: PeerLink, req: dict, data: dict | None = None,
             session_id=None) -> None:
        link.send_obj({
            "v": protocol.PROTOCOL_VERSION, "type": "ACK",
            "requestId": req["requestId"], "request": req["type"],
            "sessionId": session_id, "data": data or {}})

    def _reject(self, link: PeerLink, req: dict | str | None,
                rtype: str | None, reason: str, session_id=None) -> None:
        # req is normally the request dict, but the malformed-parse path can
        # only pass the requestId fragment (or None) — a str here crashed a
        # live link with a 1011 instead of returning the typed REJECT.
        if isinstance(req, dict):
            rid, rname = req.get("requestId"), req.get("type")
        else:
            rid = req if isinstance(req, str) else None
            rname = rtype
        link.send_obj({
            "v": protocol.PROTOCOL_VERSION, "type": "REJECT",
            "requestId": rid,
            "request": rname,
            "reason": reason, "sessionId": session_id})

    def _create(self, link: PeerLink, req: dict, origin: str = "create") -> None:
        code = _new_code()
        while code in self._by_code:
            code = _new_code()
        session = PairSession(
            id=_new_id(), code=code,
            code_expires_at=self._now() + self.code_ttl,
            created_at=self._now(), origin=origin,
            match_token=_new_token())
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
        self._bind_follower(link, req, session)

    def _bind_follower(self, link: PeerLink, req: dict,
                       session: PairSession) -> None:
        user = link.user or {}
        member = Member(id=_new_id(), role="follower",
                        name=user.get("name") or "", resume_token=_new_token(),
                        session=session, link=link, status="pending")
        session.pending = member
        session.code_consumed = True   # single-use on success
        link.member = member
        leader = session.leader()
        self._ack(link, req, {
            "sessionId": session.id, "memberId": member.id,
            "role": "follower", "resumeToken": member.resume_token,
            "leaderName": leader.name if leader else ""},
            session_id=session.id)
        if leader is not None and leader.link is not None:
            leader.link.send_obj({
                "v": protocol.PROTOCOL_VERSION, "type": "JOIN_REQUEST",
                "sessionId": session.id, "memberId": member.id,
                "name": member.name})
        logger.info("reader-sync link=%s member=%s pending join", link.id, member.id)

    def _match(self, link: PeerLink, req: dict) -> None:
        """One-tap pairing: join a fresh waiting session if exactly one is
        eligible, else become a waiting leader. Runs on the registry's
        single-threaded dispatch so the find-or-create is atomic — the
        earlier tapper wins the leader role.

        Only 'match'-origin sessions are candidates: a CREATE waiting room
        is reserved for whoever the code was shown to. Two eligible
        sessions can coexist (e.g. a dead-leader window let a second
        waiter be created, then the first leader resumed), so the
        ambiguous reply hands out opaque match tokens — never codes, which
        would let the picker bypass JOIN consent and consume the code a
        real partner might still be typing."""
        pick = req["payload"].get("pick")
        if pick is not None:
            target = next(
                (s for s in self.sessions.values()
                 if s.match_token and s.match_token == pick), None)
            if target is None or not self._matchable(target, self._now()):
                self._reject(link, req, "MATCH", REASON_BAD_CODE)
                return
            self._bind_follower(link, req, target)
            return
        now = self._now()
        candidates = [s for s in self.sessions.values()
                      if self._matchable(s, now)]
        if not candidates:
            self._create(link, req, origin="match")
            return
        candidates.sort(key=lambda s: s.created_at)
        if len(candidates) > 1:
            self._ack(link, req, {
                "ambiguous": True,
                "candidates": [{
                    "id": s.match_token,
                    "leaderName": s.leader().name,
                    "ageSec": int(now - s.created_at),
                } for s in candidates[:8]]})
            return
        self._bind_follower(link, req, candidates[0])

    def _matchable(self, s: PairSession, now: float) -> bool:
        if (s.origin != "match" or s.pending is not None or
                len(s.members) != 1 or s.code_consumed):
            return False
        if now >= s.code_expires_at or now - s.created_at > self.match_window:
            return False
        leader = s.leader()
        return (leader is not None and leader.link is not None
                and leader.link.alive)

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
        # §3.3: the manifest is re-delivered on resume so the member can
        # verify + READY against the committed workEpoch/manifestVersion.
        if session.manifest is not None:
            self._send_manifest(link, session)
        logger.info("reader-sync link=%s member=%s resumed", link.id, member.id)
        self._wake_ticker()

    def _set_activity(self, link: PeerLink, req: dict, session: PairSession,
                      member: Member) -> None:
        state = req["payload"].get("state")
        if state not in ("active", "suspended", "selecting"):
            self._reject(link, req, "SET_ACTIVITY", REASON_MALFORMED,
                         session_id=session.id)
            return
        if state == "selecting":
            # "selecting" is session phase control, not a member status — only
            # the leader's browse flag drives it (#75 §3.6).
            if member.role != "leader":
                self._reject(link, req, "SET_ACTIVITY", REASON_NOT_LEADER,
                             session_id=session.id)
                return
            session.selecting = True
        else:
            if member.status != "offline":
                member.status = state
            if member.role == "leader":
                session.selecting = False
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

    def _ui_sync(self, link: PeerLink, req: dict, session: PairSession,
                 member: Member) -> None:
        """Ephemeral mirrored-UI relay (browse scroll/position). Same shape
        as SIGNAL — forwarded verbatim to the peer, never sequenced or
        stored, and fire-and-forget: no ACK on success (the sender never
        tracks or consumes it) and no response when the peer is offline
        (transient state, and a REJECT per scroll frame would spam client
        notifications). NOT_PAIRED still rejects — it is a real misuse.
        A silent 1s-window cap keeps a runaway sender from filling the
        peer's outq (overflow would close the *recipient's* link)."""
        if not session.paired:
            self._reject(link, req, "UI_SYNC", REASON_NOT_PAIRED,
                         session_id=session.id)
            return
        other = next((m for m in session.members.values() if m is not member), None)
        if other is None or other.link is None or not other.link.alive:
            return
        now = self._now()
        if now - member.ui_sync_window_start >= UI_SYNC_WINDOW_SEC:
            member.ui_sync_window_start = now
            member.ui_sync_count = 0
        member.ui_sync_count += 1
        if member.ui_sync_count > UI_SYNC_RATE_MAX:
            return
        other.link.send_obj({
            "v": protocol.PROTOCOL_VERSION, "type": "UI_SYNC",
            "sessionId": session.id, "from": member.role,
            "payload": req["payload"]})

    def _transport_state(self, link: PeerLink, req: dict, session: PairSession,
                         member: Member) -> None:
        state = req["payload"].get("state")
        if state not in _TRANSPORT_REPORT:
            self._reject(link, req, "TRANSPORT_STATE", REASON_MALFORMED,
                         session_id=session.id)
            return
        member.dc_state = state
        self._ack(link, req, session_id=session.id)
        self._announce_plane_flip(session)

    def _eval_data_plane(self, session: PairSession) -> bool:
        """Recompute the data plane; returns True when it flipped."""
        new_plane = "webrtc-dc" if (
            session.paired
            and all(m.dc_state == "up" for m in session.members.values())) \
            else "server-ws"
        if new_plane != session.data_plane:
            session.data_plane = new_plane
            return True
        return False

    def _announce_plane_flip(self, session: PairSession) -> None:
        """Plane switch happens only at a revision boundary: when it changed,
        the next STATE revision carries it for both members, followed by an
        explicit TRANSPORT_STATE notification pinned to that revision."""
        if self._eval_data_plane(session):
            self._broadcast_state(session)
            self._broadcast(session, {
                "v": protocol.PROTOCOL_VERSION, "type": "TRANSPORT_STATE",
                "sessionId": session.id, "dataPlane": session.data_plane,
                "revision": session.revision})
            logger.info("reader-sync session transport=%s rev=%d",
                        session.data_plane, session.revision)

    # ── sequenced ops (S05): validate -> apply -> commit ────────────

    def _dedup_key(self, req: dict, role: str) -> str:
        """requestId scoped by effective role — a resend keeps the same key
        whether it arrives directly or relayed by the leader."""
        return f"{role}:{req['requestId']}"

    def _dedup_store(self, session: PairSession, key: str, frame: dict) -> None:
        session.dedup[key] = frame
        while len(session.dedup) > DEDUP_WINDOW:
            oldest = next(iter(session.dedup))
            if oldest == key:
                break  # the just-stored entry is never evicted in-place
            session.dedup.pop(oldest)
            session.dedup_seen[oldest] = None
            while len(session.dedup_seen) > DEDUP_TOMBSTONE_MAX:
                session.dedup_seen.pop(next(iter(session.dedup_seen)))

    def _reject_snapshot(self, link: PeerLink, req: dict, session: PairSession,
                         role: str, reason: str) -> None:
        """REJECT carrying the latest committed snapshot verbatim (§3.4:
        stale requests get 'reject + latest snapshot'). Stored under the
        dedup key so a resend replays the identical response."""
        resp = {
            "v": protocol.PROTOCOL_VERSION, "type": "REJECT",
            "requestId": req["requestId"], "request": req["type"],
            "reason": reason, "sessionId": session.id,
            "snapshot": rstate.snapshot(session),
        }
        self._dedup_store(session, self._dedup_key(req, role), resp)
        link.send_obj(resp)

    def _sequenced_op(self, link: PeerLink, req: dict, session: PairSession,
                      member: Member) -> None:
        """One sequenced request. Order of checks (each reject leaves the
        committed snapshot untouched): origin validity -> dedup -> epoch ->
        manifestVersion -> baseRevision -> per-type handler. OPEN_WORK
        skips the epoch/manifestVersion/baseRevision checks — see the
        state.py docstring for why a pick is exempt."""
        t = req["type"]
        payload = req["payload"]

        # Effective role / member: on the leader-sequenced plane the leader
        # forwards follower-ordered requests verbatim with payload.origin —
        # only a leader may carry the field, and only a session role name.
        role = member.role
        eff = member
        origin = payload.get("origin")
        if origin is not None:
            if origin not in protocol.ROLES or member.role != "leader":
                self._reject_snapshot(link, req, session, member.role,
                                      rstate.REASON_BAD_ORIGIN)
                return
            role = origin
            if role != member.role:
                eff = next((m for m in session.members.values()
                            if m.role == role), member)

        dkey = self._dedup_key(req, role)
        cached = session.dedup.get(dkey)
        if cached is not None:
            link.send_obj(cached)      # identical replay, no state change
            return
        if dkey in session.dedup_seen:
            self._reject_snapshot(link, req, session, role,
                                  rstate.REASON_STALE_REQUEST)
            return

        try:
            # OPEN_WORK is exempt from the optimistic checks: the pick is
            # self-contained and latest-pick-wins — a stale base only made
            # a live tile tap die silently.
            if t != "OPEN_WORK":
                rstate.check_epoch(session, req)
                rstate.check_manifest_version(session, payload)
                if t in rstate.ABSOLUTE_TYPES:
                    rstate.check_base(session, req)
            self._apply_op(link, req, session, member, eff, role)
        except rstate.Reject as e:
            self._reject_snapshot(link, req, session, role, e.reason)

    def _apply_op(self, link: PeerLink, req: dict, session: PairSession,
                  member: Member, eff: Member, role: str) -> None:
        """Dispatch one validated sequenced op. Raises rstate.Reject on any
        validation failure — callers get a snapshot-carrying REJECT and the
        session state is untouched."""
        t = req["type"]
        payload = req["payload"]
        if t == "OPEN_WORK":
            if role != "leader":
                raise rstate.Reject(rstate.REASON_NOT_LEADER)
            page = payload.get("page")
            if page is None:
                raise rstate.Reject(rstate.REASON_NO_INITIAL_PAGE)
            offset = payload.get("offset", session.offset)
            if offset not in (0, 1):
                raise rstate.Reject(rstate.REASON_RANGE)
            self._begin_load(link, req, session, role,
                             folder=payload["folder"], page=page,
                             offset=offset)
            return
        if t in ("TURN", "GOTO"):
            gate = rstate.gate_page_turn(session)
            if gate is not None:
                raise rstate.Reject(gate)
            if t == "TURN":
                rstate.apply_turn(session, payload)
            else:
                rstate.apply_goto(session, payload)
            self._commit_op(link, req, session, role)
            return
        if t == "SET_OFFSET":
            if session.manifest is None:
                raise rstate.Reject(rstate.REASON_NO_WORK)
            offset = payload["offset"]
            if offset == session.offset:
                self._commit_op(link, req, session, role)   # accepted no-op
                return
            idx = rstate.current_index(session)
            if idx < 0:
                raise rstate.Reject(rstate.REASON_NO_WORK)
            anchor = session.plan[idx].right   # right is always a PageRef
            self._begin_load(link, req, session, role,
                             folder=session.manifest["folder"],
                             page={"folder": anchor.folder,
                                   "name": anchor.name},
                             offset=offset)
            return
        if t == "SWAP_SIDES":
            rstate.apply_swap_sides(session, payload)
            self._commit_op(link, req, session, role)
            return
        if t == "READY":
            if session.manifest is None:
                raise rstate.Reject(rstate.REASON_NO_WORK)
            session.ready.add(eff.id)
            self._commit_op(link, req, session, role)
            return
        if t == "APPLIED":
            # client-side apply report: acknowledged, never a broadcast and
            # never a state transition — applying state must not echo new
            # client-visible requests.
            eff.last_applied = {"requestId": payload.get("requestId"),
                                "revision": req["baseRevision"]}
            resp = {"v": protocol.PROTOCOL_VERSION, "type": "ACK",
                    "requestId": req["requestId"], "request": "APPLIED",
                    "sessionId": session.id, "data": {}}
            self._dedup_store(session, self._dedup_key(req, role), resp)
            link.send_obj(resp)
            return
        raise rstate.Reject(rstate.REASON_MALFORMED)

    def _commit_op(self, link: PeerLink, req: dict, session: PairSession,
                   role: str) -> None:
        """Accepted sequenced op: one revision bump, apply-order log, ACK to
        the requester, then APPLIED + STATE commit broadcast to both
        members — applied order == sequencer receive order."""
        session.apply_seq += 1
        frame = self._state_frame(session)   # bumps revision, derives phase
        session.apply_log.append({
            "seq": session.apply_seq, "type": req["type"], "role": role,
            "requestId": req["requestId"], "revision": frame["revision"],
            "spreadId": frame["spreadId"]})
        logger.info("reader-sync apply seq=%d rev=%d type=%s role=%s link=%s",
                    session.apply_seq, frame["revision"], req["type"], role,
                    link.id)
        resp = {"v": protocol.PROTOCOL_VERSION, "type": "ACK",
                "requestId": req["requestId"], "request": req["type"],
                "sessionId": session.id, "data": {}}
        self._dedup_store(session, self._dedup_key(req, role), resp)
        link.send_obj(resp)
        self._broadcast(session, {
            "v": protocol.PROTOCOL_VERSION, "type": "APPLIED",
            "sessionId": session.id, "requestId": req["requestId"],
            "revision": frame["revision"], "by": role})
        self._broadcast(session, frame)

    # ── async work loading (OPEN_WORK / SET_OFFSET) ─────────────────

    def _begin_load(self, link: PeerLink, req: dict, session: PairSession,
                    role: str, *, folder: str, page: dict, offset: int) -> None:
        """Accept a work change: new generation + loading STATE now, manifest
        commit only once the resolver finishes (see _finish_load)."""
        if self.work_resolver is None:
            raise rstate.Reject(rstate.REASON_UNAVAILABLE)
        session.load_seq += 1
        job = {"seq": session.load_seq, "kind": req["type"].lower(),
               "folder": folder, "page": page, "offset": offset,
               "requestId": req["requestId"], "role": role}
        session.pending_load = job
        self._commit_op(link, req, session, role)
        try:
            result = self.work_resolver(link, job)
        except Exception as e:
            self._finish_load(session, job, error=e)
            return
        if inspect.isawaitable(result):
            self._schedule_load(session, job, result)
        else:
            self._finish_load(session, job, manifest=result)

    def _schedule_load(self, session: PairSession, job: dict, awaitable) -> None:
        """Drive an awaitable resolver on the running loop (the WS receive
        path always has one); in loop-less contexts (sync test rigs) run it
        to completion inline rather than silently dropping it."""
        async def runner():
            try:
                manifest = await awaitable
            except Exception as e:
                self._finish_load_by_id(session.id, job, error=e)
            else:
                self._finish_load_by_id(session.id, job, manifest=manifest)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            task = loop.create_task(runner(), name="reader-sync-load")
            self.track_task(task)
        else:
            asyncio.run(runner())

    def _finish_load_by_id(self, session_id: str, job: dict, manifest=None,
                           error=None) -> None:
        session = self.sessions.get(session_id)
        if session is not None:
            self._finish_load(session, job, manifest=manifest, error=error)

    def _finish_load(self, session: PairSession, job: dict, manifest=None,
                     error=None) -> None:
        """Resolve completion. Generation guard first: only the newest
        accepted load may commit — a stale resolve finishing late is
        discarded and can never roll back a newer epoch (#75 §3.4)."""
        if session.pending_load is not job or job["seq"] != session.load_seq:
            logger.info("reader-sync stale load discarded seq=%d", job["seq"])
            return
        session.pending_load = None
        if error is not None:
            code = getattr(error, "code", None) or "ERR_MALFORMED"
            reason = rstate.MANIFEST_ERR_REASON.get(code, "malformed")
            logger.info("reader-sync load failed code=%s", code)
            self._broadcast(session, {
                "v": protocol.PROTOCOL_VERSION, "type": "ERROR",
                "sessionId": session.id, "code": reason})
            self._broadcast_state(session)
            return
        # commit: new epoch + manifest for everyone, READY gate re-arms
        session.work_epoch += 1
        session.manifest_version += 1
        session.manifest = manifest
        session.plan = rstate.plan_from_manifest(manifest)
        session.offset = manifest.get("offset", session.offset)
        session.spread_id = (manifest.get("initial") or {}).get("spreadId")
        session.ready.clear()
        session.dedup.clear()
        session.dedup_seen.clear()
        session.selecting = False
        self._send_manifest(None, session)
        session.apply_seq += 1
        frame = self._state_frame(session)
        session.apply_log.append({
            "seq": session.apply_seq, "type": "MANIFEST_COMMIT",
            "role": "server", "requestId": job["requestId"],
            "revision": frame["revision"], "spreadId": frame["spreadId"]})
        logger.info("reader-sync apply seq=%d rev=%d type=MANIFEST_COMMIT",
                    session.apply_seq, frame["revision"])
        self._broadcast(session, frame)

    def _send_manifest(self, link: PeerLink | None, session: PairSession) -> None:
        """MANIFEST chunk frames for the committed manifest — broadcast when
        link is None, unicast otherwise (the resume path, §3.3)."""
        from app.reader_sync.catalog import split_manifest   # lazy import
        for chunk in split_manifest(session.manifest):
            frame = dict(chunk)
            frame.update({"type": "MANIFEST", "sessionId": session.id,
                          "workEpoch": session.work_epoch,
                          "manifestVersion": session.manifest_version})
            if link is None:
                self._broadcast(session, frame)
            else:
                link.send_obj(frame)

    # ── broadcast / teardown ─────────────────────────────────────────

    def _state_frame(self, session: PairSession) -> dict:
        session.revision += 1
        # phase is derived state, never an independent source (#80/S05)
        session.phase = rstate.derive_phase(session)
        return {
            "v": protocol.PROTOCOL_VERSION, "type": "STATE",
            "sessionId": session.id, "revision": session.revision,
            "workEpoch": session.work_epoch,
            "manifestVersion": session.manifest_version,
            "phase": session.phase, "spreadId": session.spread_id,
            "offset": session.offset, "leaderSide": session.leader_side,
            "presence": {
                m.id: {"role": m.role, "status": m.status, "name": m.name,
                       "ready": m.id in session.ready}
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
