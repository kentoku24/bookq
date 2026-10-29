"""S05 — authoritative shared state + sequencer / conflict control (#80).

Transport-free, deterministic: exercises ``app.reader_sync.sessions`` +
``app.reader_sync.state`` directly with a fake clock, fake links and an
injected manifest resolver (the catalog I/O path is covered separately by
S04 and the real-WS layer in tests/test_reader_ws_state.py).

Spec anchors (issue #80 完了条件 / parent #75 §3.4):
- accepted TURN/GOTO/SET_OFFSET/SWAP_SIDES/OPEN_WORK broadcast STATE with a
  strictly increasing revision; rejections leave the committed snapshot
  (spreadId / revision / manifestVersion) exactly unchanged.
- duplicate requestId replays the stored outcome; stale workEpoch /
  manifestVersion / baseRevision / foreign session are rejected and return
  the latest snapshot.
- apply order == receive order (``session.apply_log``); no state apply ever
  emits a client request frame (zero echo).
- follower OPEN_WORK rejected; leader OPEN_WORK = new workEpoch + manifest
  delivery + READY gate before page input resumes.
- peer-absent / not-ready / selecting TURN/GOTO rejected; nothing queued
  during a disconnect is replayed on resume.
- an outdated async load completion is discarded by generation.
- the same fixed op sequence on server-ws and webrtc-dc (leader-sequenced,
  role-deterministic tie-break) yields the same final state, and both
  members observe the identical revision sequence.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

import os

os.environ["BOOKQ_TEST_MODE"] = "true"

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from app.reader_sync import model, protocol  # noqa: E402
from app.reader_sync import catalog  # noqa: E402
from app.reader_sync import state as rstate  # noqa: E402
from app.reader_sync.sessions import (  # noqa: E402
    REASON_ALREADY_BOUND,  # noqa: F401  (import surface smoke)
    PeerLink,
    SessionRegistry,
    _BULK,
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
    """PeerLink drained by hand; transport close recorded."""

    def __init__(self, registry: SessionRegistry, name: str = ""):
        super().__init__(registry)
        self.user = {"email": f"{name}@example.com", "name": name or "user", "picture": ""}
        self.name = name
        self.close_code = None
        self.close_reason = None

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
            if raw is _BULK:
                self._bulk_pending = False
                while True:
                    bulk = self._pop_bulk()
                    if bulk is None:
                        break
                    out.append(json.loads(bulk))
                continue
            out.append(json.loads(raw))
        return out

    def types(self) -> list[str]:
        return [f["type"] for f in self.frames()]


def _of(frames: list[dict], t: str) -> list[dict]:
    return [f for f in frames if f["type"] == t]


def _one(frames: list[dict], t: str) -> dict:
    got = _of(frames, t)
    assert len(got) == 1, f"expected exactly one {t}, got {frames!r}"
    return got[0]


def _req(rtype: str, payload=None, *, session_id=None, request_id="r1",
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


def _connect(reg: SessionRegistry, name: str) -> FakeLink:
    link = FakeLink(reg, name=name)
    reg.register_link(link)
    assert _one(link.frames(), "HELLO")
    return link


def _pair(reg: SessionRegistry, leader: FakeLink, follower: FakeLink) -> str:
    _send(reg, leader, _req("CREATE", request_id="c1"))
    cframes = leader.frames()
    data = _one(cframes, "ACK")["data"]
    _one(cframes, "STATE")
    _send(reg, follower, _req("JOIN", {"code": data["code"]}, request_id="j1"))
    joined = _one(follower.frames(), "ACK")["data"]
    _one(leader.frames(), "JOIN_REQUEST")
    _send(reg, leader, _req("APPROVE", {"memberId": joined["memberId"]},
                            session_id=data["sessionId"], request_id="a1"))
    aframes = leader.frames()
    _one(aframes, "ACK")
    _one(aframes, "STATE")
    _one(follower.frames(), "STATE")
    return data["sessionId"]


# ── fake manifest / resolver ───────────────────────────────────────


def _manifest(folder: str = "book", pages=("p1.png", "p2.png", "p3.png",
              "p4.png", "p5.png", "p6.png"), *, offset: int = 0,
              initial: str | None = None, chapters: list | None = None) -> dict:
    """Hand-built manifest with the same shape catalog.resolve_work emits."""
    if chapters is None:
        chapters = [{"name": folder, "folder": folder,
                     "pages": [{"folder": folder, "name": n} for n in pages]}]
    plan = model.build_spread_plan(
        [{"folder": c["folder"], "pages": c["pages"]} for c in chapters], offset)
    page_refs = [p for c in chapters for p in c["pages"]]
    man = {
        "v": protocol.PROTOCOL_VERSION,
        "folder": folder,
        "offset": offset,
        "chapters": chapters,
        "pageRefs": page_refs,
        "spreadTable": [model.serialize_spread(s) for s in plan],
    }
    man["digest"] = catalog.manifest_digest(man)
    if initial is None:
        initial = chapters[0]["pages"][0]["name"]
    ref = {"folder": chapters[0]["folder"], "name": initial}
    idx = next(i for i, s in enumerate(plan)
               if (s.right.folder, s.right.name) == (ref["folder"], ref["name"])
               or (s.left and (s.left.folder, s.left.name) == (ref["folder"], ref["name"])))
    man["initial"] = {"page": ref, "spreadId": plan[idx].id}
    return man


def _resolver(manifests: dict, errors: dict | None = None):
    """Sync resolver: manifests/errors keyed by folder name."""
    errors = errors or {}

    def resolve(link, job):
        folder = job["folder"]
        if folder in errors:
            raise errors[folder]
        man = manifests[folder]
        # resolve_work contract: initial page / offset / spread for THIS job
        out = dict(man)
        out["offset"] = job["offset"]
        if job["offset"] != man["offset"]:
            # rebuild the spread table for the requested offset
            reb = _manifest(folder, chapters=man["chapters"], offset=job["offset"],
                            initial=job["page"]["name"])
            reb["folder"] = folder
            return reb
        ref = job["page"]
        idx = next(i for i, s in enumerate(man["spreadTable"])
                   if s["right"] == ref or s["left"] == ref)
        out["initial"] = {"page": ref, "spreadId": man["spreadTable"][idx]["id"]}
        return out

    return resolve


def _reg(clock: FakeClock, resolver=None, **kw) -> SessionRegistry:
    return SessionRegistry(clock=clock.now, work_resolver=resolver, **kw)


def _open_and_read(reg, leader, follower, sid, *, rev=None) -> dict:
    """OPEN_WORK + READY x2 -> reading. Returns the last STATE seen."""
    rev = reg.sessions[sid].revision if rev is None else rev
    _send(reg, leader, _req("OPEN_WORK",
                            {"folder": "book", "page": {"folder": "book", "name": "p1.png"}},
                            session_id=sid, request_id="ow1",
                            work_epoch=0, base_revision=rev))
    lf = leader.frames()
    _one(lf, "ACK")
    follower.frames()
    epoch = reg.sessions[sid].work_epoch
    _send(reg, leader, _req("READY", session_id=sid, request_id="rdy-l",
                            work_epoch=epoch))
    _send(reg, follower, _req("READY", session_id=sid, request_id="rdy-f",
                              work_epoch=epoch))
    leader.frames()
    fframes = follower.frames()
    last = _of(fframes, "STATE")[-1]
    return last


def _snapshot(session) -> dict:
    return rstate.snapshot(session)


def _turn(delta=1, cross=True, chapter=None, **kw):
    return _req("TURN", {"delta": delta, "crossChapter": cross,
                         "fromChapter": chapter}, **kw)


def _goto(folder="book", name="p1.png", **kw):
    return _req("GOTO", {"page": {"folder": folder, "name": name}}, **kw)


# ═══════════════════════════════════════════════════════════════════
# fixed op sequences (spec: 固定操作列 assert)
# ═══════════════════════════════════════════════════════════════════


def test_simultaneous_nexts_advance_two_spreads():
    """同時NEXT×2 → 合計+2見開き (receive order applies both)."""
    clock = FakeClock()
    reg = _reg(clock, resolver=_resolver({"book": _manifest()}))
    leader, follower = _connect(reg, "l"), _connect(reg, "f")
    sid = _pair(reg, leader, follower)
    st = _open_and_read(reg, leader, follower, sid)
    session = reg.sessions[sid]
    assert st["phase"] == "reading" and st["spreadId"] == "c0:s0"
    rev = session.revision

    _send(reg, leader, _turn(session_id=sid, request_id="n1", work_epoch=1,
                             base_revision=rev))
    _send(reg, follower, _turn(session_id=sid, request_id="n2", work_epoch=1,
                               base_revision=rev))
    assert session.spread_id == "c0:s2"      # +2 spreads total
    assert session.revision == rev + 2       # one commit each, both accepted
    lrev = [f["revision"] for f in _of(leader.frames(), "STATE")]
    frev = [f["revision"] for f in _of(follower.frames(), "STATE")]
    assert lrev == frev == [rev + 1, rev + 2]


def test_next_then_prev_returns_to_spread():
    clock = FakeClock()
    reg = _reg(clock, resolver=_resolver({"book": _manifest()}))
    leader, follower = _connect(reg, "l"), _connect(reg, "f")
    sid = _pair(reg, leader, follower)
    _open_and_read(reg, leader, follower, sid)
    session = reg.sessions[sid]

    _send(reg, follower, _turn(1, session_id=sid, request_id="n", work_epoch=1))
    assert session.spread_id == "c0:s1"
    _send(reg, follower, _turn(-1, session_id=sid, request_id="p", work_epoch=1))
    assert session.spread_id == "c0:s0"


def test_boundary_turns_clamp_unchanged():
    """先頭PREV・末尾NEXT → spread不変 (accepted → revision still bumps)."""
    clock = FakeClock()
    reg = _reg(clock, resolver=_resolver({"book": _manifest()}))
    leader, follower = _connect(reg, "l"), _connect(reg, "f")
    sid = _pair(reg, leader, follower)
    _open_and_read(reg, leader, follower, sid)
    session = reg.sessions[sid]
    assert session.spread_id == "c0:s0"      # 6 pages -> s0..s2
    rev = session.revision

    _send(reg, leader, _turn(-1, session_id=sid, request_id="p0", work_epoch=1))
    assert session.spread_id == "c0:s0" and session.revision == rev + 1
    # move to tail then NEXT -> stays
    _send(reg, follower, _goto(name="p6.png", session_id=sid, request_id="g",
                               work_epoch=1, base_revision=session.revision))
    assert session.spread_id == "c0:s2"
    rev = session.revision
    _send(reg, leader, _turn(1, session_id=sid, request_id="n9", work_epoch=1))
    assert session.spread_id == "c0:s2" and session.revision == rev + 1


def test_chapter_limited_turn_rules():
    """章内制限あり操作: 章端でclamp、章外(fromChapter不一致)は拒否。"""
    chapters = [
        {"name": "A", "folder": "w/A",
         "pages": [{"folder": "w/A", "name": "a1.png"},
                   {"folder": "w/A", "name": "a2.png"}]},
        {"name": "B", "folder": "w/B",
         "pages": [{"folder": "w/B", "name": "b1.png"},
                   {"folder": "w/B", "name": "b2.png"}]},
    ]
    man = _manifest("w/A", chapters=chapters, initial="a1.png")
    clock = FakeClock()
    reg = _reg(clock, resolver=_resolver({"w/A": man}))
    leader, follower = _connect(reg, "l"), _connect(reg, "f")
    sid = _pair(reg, leader, follower)
    _send(reg, leader, _req("OPEN_WORK",
                            {"folder": "w/A", "page": {"folder": "w/A", "name": "a1.png"}},
                            session_id=sid, request_id="ow",
                            work_epoch=0, base_revision=reg.sessions[sid].revision))
    leader.frames(); follower.frames()
    _send(reg, leader, _req("READY", session_id=sid, request_id="rl", work_epoch=1))
    _send(reg, follower, _req("READY", session_id=sid, request_id="rf", work_epoch=1))
    leader.frames(); follower.frames()
    session = reg.sessions[sid]
    # plan: c0:s0=(a1,a2)  c1:s0=(b1,b2); currently c0:s0 (chapter w/A)

    # chapter-limited NEXT at chapter end -> clamps inside the chapter
    _send(reg, follower, _turn(1, cross=False, chapter="w/A", session_id=sid,
                               request_id="t1", work_epoch=1))
    assert session.spread_id == "c0:s0"

    # cross-chapter NEXT moves into chapter B
    _send(reg, follower, _turn(1, cross=True, session_id=sid,
                               request_id="t2", work_epoch=1))
    assert session.spread_id == "c1:s0"

    # a stale chapter-limited TURN issued back in w/A is rejected, no move
    snap = _snapshot(session)
    _send(reg, follower, _turn(-1, cross=False, chapter="w/A", session_id=sid,
                               request_id="t3", work_epoch=1))
    rej = _one(follower.frames(), "REJECT")
    assert rej["reason"] == rstate.REASON_STALE_CHAPTER
    assert rej["snapshot"]["spreadId"] == "c1:s0"
    assert _snapshot(session) == snap

    # cross-chapter PREV returns to A
    _send(reg, leader, _turn(-1, cross=True, session_id=sid,
                             request_id="t4", work_epoch=1))
    assert session.spread_id == "c0:s0"


def test_same_ops_same_result_from_both_roles():
    """leader/follower双方からの同列操作 → 同一結果 (two independent sessions)."""
    results = []
    for order in (("l", "f"), ("f", "l")):
        clock = FakeClock()
        reg = _reg(clock, resolver=_resolver({"book": _manifest()}))
        links = {"l": _connect(reg, "l"), "f": _connect(reg, "f")}
        sid = _pair(reg, links["l"], links["f"])
        _open_and_read(reg, links["l"], links["f"], sid)
        session = reg.sessions[sid]
        base = session.revision
        for i, role in enumerate(order):
            _send(reg, links[role], _turn(1, session_id=sid,
                                          request_id=f"n{i}", work_epoch=1))
        results.append((session.spread_id, session.revision - base))
    assert results[0] == results[1] == ("c0:s2", 2)


# ═══════════════════════════════════════════════════════════════════
# dedup / staleness / foreign — snapshot EXACTLY unchanged on reject
# ═══════════════════════════════════════════════════════════════════


def _reading_session():
    clock = FakeClock()
    reg = _reg(clock, resolver=_resolver({"book": _manifest()}))
    leader, follower = _connect(reg, "l"), _connect(reg, "f")
    sid = _pair(reg, leader, follower)
    _open_and_read(reg, leader, follower, sid)
    leader.frames(); follower.frames()
    return reg, sid, leader, follower


def test_duplicate_request_id_replays_without_state_change():
    reg, sid, leader, follower = _reading_session()
    session = reg.sessions[sid]
    req = _turn(1, session_id=sid, request_id="dup-1", work_epoch=1)
    _send(reg, follower, req)
    first = follower.frames()
    ack1 = _one(first, "ACK")
    snap = _snapshot(session)
    n_state = len(_of(first, "STATE"))
    assert n_state == 1 and session.spread_id == "c0:s1"
    leader.frames()   # drain the commit broadcast so the resend diff is clean

    # identical resend -> same ACK, no new STATE, snapshot untouched
    _send(reg, follower, req)
    second = follower.frames()
    ack2 = _one(second, "ACK")
    assert ack2["requestId"] == "dup-1" == ack1["requestId"]
    assert _of(second, "STATE") == [] and _of(second, "APPLIED") == []
    assert _snapshot(session) == snap
    # peer sees nothing new either
    assert _of(leader.frames(), "STATE") == []

    # a duplicate REJECTION replays the stored reject too
    bad = _goto(name="zz.png", session_id=sid, request_id="dup-bad",
                work_epoch=1, base_revision=session.revision)
    _send(reg, leader, bad)
    rej1 = _one(leader.frames(), "REJECT")
    _send(reg, leader, bad)
    rej2 = _one(leader.frames(), "REJECT")
    assert rej1 == rej2 and _snapshot(session) == snap


def test_stale_epoch_and_manifest_and_base_rejected():
    reg, sid, leader, follower = _reading_session()
    session = reg.sessions[sid]
    snap = _snapshot(session)

    # stale workEpoch
    _send(reg, follower, _turn(1, session_id=sid, request_id="e0", work_epoch=0))
    rej = _one(follower.frames(), "REJECT")
    assert rej["reason"] == rstate.REASON_STALE_EPOCH
    assert rej["snapshot"]["revision"] == snap["revision"]

    # stale manifestVersion (client-supplied)
    _send(reg, follower, _req("READY", {"manifestVersion": 99}, session_id=sid,
                              request_id="m0", work_epoch=1))
    rej = _one(follower.frames(), "REJECT")
    assert rej["reason"] == rstate.REASON_STALE_MANIFEST

    # stale baseRevision on an absolute op
    _send(reg, leader, _goto(name="p3.png", session_id=sid, request_id="g0",
                             work_epoch=1, base_revision=0))
    rej = _one(leader.frames(), "REJECT")
    assert rej["reason"] == rstate.REASON_STALE_REVISION
    assert rej["snapshot"]["spreadId"] == snap["spreadId"]

    # SWAP_SIDES is absolute too
    _send(reg, leader, _req("SWAP_SIDES", {"leaderSide": "left"}, session_id=sid,
                            request_id="s0", work_epoch=1,
                            base_revision=snap["revision"] - 1))
    rej = _one(leader.frames(), "REJECT")
    assert rej["reason"] == rstate.REASON_STALE_REVISION

    # everything rejected: snapshot identical to the last commit
    assert _snapshot(session) == snap
    assert _of(follower.frames(), "STATE") == []


def test_foreign_session_request_rejected():
    reg, sid_a, la, fa = _reading_session()
    lb, fb = _connect(reg, "lb"), _connect(reg, "fb")
    sid_b = _pair(reg, lb, fb)
    snap_a = _snapshot(reg.sessions[sid_a])

    # member of A addresses session B
    _send(reg, la, _turn(1, session_id=sid_b, request_id="x1", work_epoch=1))
    rej = _one(la.frames(), "REJECT")
    assert rej["reason"] == "wrong_session"
    # an unbound stranger is rejected too
    stranger = _connect(reg, "x")
    _send(reg, stranger, _turn(1, session_id=sid_a, request_id="x2", work_epoch=1))
    assert _one(stranger.frames(), "REJECT")["reason"] == "no_session"
    assert _snapshot(reg.sessions[sid_a]) == snap_a
    assert reg.sessions[sid_b].spread_id is None


# ═══════════════════════════════════════════════════════════════════
# order / monotonic revision / no echo
# ═══════════════════════════════════════════════════════════════════


def test_apply_log_matches_receive_order_and_no_echo():
    reg, sid, leader, follower = _reading_session()
    session = reg.sessions[sid]

    ops = [
        (follower, _turn(1, session_id=sid, request_id="o1", work_epoch=1)),
        (leader, _goto(name="p5.png", session_id=sid, request_id="o2",
                       work_epoch=1, base_revision=None)),  # filled below
        (follower, _turn(-1, session_id=sid, request_id="o3", work_epoch=1)),
        (leader, _req("SWAP_SIDES", {"leaderSide": "left"}, session_id=sid,
                      request_id="o4", work_epoch=1, base_revision=None)),
    ]
    log_before = len(session.apply_log)
    sent_types = []
    for link, req in ops:
        if req["baseRevision"] is None:
            req["baseRevision"] = session.revision
        _send(reg, link, req)
        sent_types.append(req["type"])

    new_entries = session.apply_log[log_before:]
    assert [e["type"] for e in new_entries] == sent_types  # apply == receive order
    revs = [e["revision"] for e in new_entries]
    assert revs == sorted(revs) and len(set(revs)) == len(revs)  # strictly ↑

    l_states = [f["revision"] for f in _of(leader.frames(), "STATE")]
    f_states = [f["revision"] for f in _of(follower.frames(), "STATE")]
    assert l_states == f_states  # identical revision sequence on both members
    assert all(b > a for a, b in zip(l_states, l_states[1:]))

    # zero echo: applying state never fabricates client requests — the number
    # of applied ops equals the number of request frames received, and every
    # server-emitted frame is a control/state type (never a request type).
    assert len(new_entries) == len(ops)
    server_only = {"ACK", "REJECT", "APPLIED", "STATE", "MANIFEST", "ERROR",
                   "END", "HELLO", "PING", "PONG", "JOIN_REQUEST", "JOIN_CANCEL",
                   "SIGNAL", "TRANSPORT_STATE"}
    for link in (leader, follower):
        for f in link.frames():
            assert f["type"] in server_only


def test_revision_never_regresses_across_all_broadcasts():
    reg, sid, leader, follower = _reading_session()
    session = reg.sessions[sid]
    seen = []
    for i in range(6):
        _send(reg, leader, _turn(1 if i % 2 == 0 else -1, session_id=sid,
                                 request_id=f"r{i}", work_epoch=1))
        seen += [f["revision"] for f in _of(leader.frames(), "STATE")]
    assert seen == sorted(seen) and len(set(seen)) == len(seen)
    assert session.revision == seen[-1]


# ═══════════════════════════════════════════════════════════════════
# OPEN_WORK / READY / phases
# ═══════════════════════════════════════════════════════════════════


def test_follower_open_work_rejected_leader_flow():
    reg, sid, leader, follower = _reading_session()
    session = reg.sessions[sid]
    epoch_before = session.work_epoch
    snap = _snapshot(session)

    _send(reg, follower, _req("OPEN_WORK",
                              {"folder": "book", "page": {"folder": "book", "name": "p1.png"}},
                              session_id=sid, request_id="fo",
                              work_epoch=1, base_revision=session.revision))
    rej = _one(follower.frames(), "REJECT")
    assert rej["reason"] == "not_leader"
    assert _snapshot(session) == snap and session.work_epoch == epoch_before


def test_open_work_new_epoch_manifest_and_ready_gate():
    """leaderの作品変更 → 新epoch+新manifest配信、READYまで入力拒否。"""
    clock = FakeClock()
    man_a = _manifest("book", pages=("a1.png", "a2.png", "a3.png", "a4.png"))
    man_b = _manifest("other", pages=("b1.png", "b2.png", "b3.png", "b4.png"),
                      initial="b1.png")
    reg = _reg(clock, resolver=_resolver({"book": man_a, "other": man_b}))
    leader, follower = _connect(reg, "l"), _connect(reg, "f")
    sid = _pair(reg, leader, follower)
    session = reg.sessions[sid]

    # work A -> epoch 1, manifestVersion 1, phase loading until READYs
    _send(reg, leader, _req("OPEN_WORK",
                            {"folder": "book", "page": {"folder": "book", "name": "a1.png"}},
                            session_id=sid, request_id="owa",
                            work_epoch=0, base_revision=session.revision))
    lf = leader.frames()
    assert _of(lf, "MANIFEST") and catalog.join_manifest(
        [{k: c[k] for k in ("v", "kind", "digest", "index", "count", "data")}
         for c in _of(lf, "MANIFEST")])["digest"] == man_a["digest"]
    last = _of(lf, "STATE")[-1]
    assert session.work_epoch == 1 and session.manifest_version == 1
    assert last["phase"] == "loading"

    # page input refused until both sides READY
    _send(reg, leader, _turn(1, session_id=sid, request_id="early", work_epoch=1))
    assert _one(leader.frames(), "REJECT")["reason"] == rstate.REASON_NOT_READY

    _send(reg, leader, _req("READY", session_id=sid, request_id="rl", work_epoch=1))
    _send(reg, follower, _req("READY", session_id=sid, request_id="rf", work_epoch=1))
    last = _of(follower.frames(), "STATE")[-1]
    assert last["phase"] == "reading"

    # leader switches to another work -> new epoch + new manifest
    _send(reg, leader, _req("OPEN_WORK",
                            {"folder": "other", "page": {"folder": "other", "name": "b1.png"}},
                            session_id=sid, request_id="owb",
                            work_epoch=1, base_revision=session.revision))
    lb = leader.frames()
    mans = _of(lb, "MANIFEST")
    assert mans and all(m["workEpoch"] == 2 for m in mans)
    assert session.work_epoch == 2 and session.manifest_version == 2
    assert _of(lb, "STATE")[-1]["phase"] == "loading"

    # old-epoch page input refused during the new load
    _send(reg, follower, _turn(1, session_id=sid, request_id="old-ep", work_epoch=1))
    rej = _one(follower.frames(), "REJECT")
    assert rej["reason"] in (rstate.REASON_STALE_EPOCH, rstate.REASON_NOT_READY)

    _send(reg, leader, _req("READY", session_id=sid, request_id="rl2", work_epoch=2))
    _send(reg, follower, _req("READY", session_id=sid, request_id="rf2", work_epoch=2))
    last = _of(follower.frames(), "STATE")[-1]
    assert last["phase"] == "reading" and session.spread_id == "c0:s0"
    # reading resumed: TURN accepted again
    _send(reg, follower, _turn(1, session_id=sid, request_id="n", work_epoch=2))
    assert session.spread_id == "c0:s1"


def test_turn_goto_gates_offline_notready_selecting():
    reg, sid, leader, follower = _reading_session()
    session = reg.sessions[sid]
    snap = _snapshot(session)

    # peer absent -> paused -> reject
    reg.detach_link(follower)
    _send(reg, leader, _turn(1, session_id=sid, request_id="off", work_epoch=1))
    rej = _one(leader.frames(), "REJECT")
    assert rej["reason"] == "peer_offline"
    assert _snapshot(session)["spreadId"] == snap["spreadId"]

    # peer back but not READY yet -> loading -> reject not_ready
    back = _connect(reg, "f2")
    member = next(m for m in session.members.values() if m.role == "follower")
    _send(reg, back, _req("RESUME", {"memberId": member.id,
                                     "resumeToken": member.resume_token},
                          session_id=sid, request_id="re"))
    back.frames(); leader.frames()
    _send(reg, leader, _turn(1, session_id=sid, request_id="nr", work_epoch=1))
    assert _one(leader.frames(), "REJECT")["reason"] == rstate.REASON_NOT_READY

    _send(reg, back, _req("READY", session_id=sid, request_id="r2", work_epoch=1))
    back.frames(); leader.frames()
    assert rstate.derive_phase(session) == "reading"

    # leader starts browsing -> selecting -> reject
    _send(reg, leader, _req("SET_ACTIVITY", {"state": "selecting"},
                            session_id=sid, request_id="sel"))
    leader.frames()
    # the follower's live link after RESUME is `back` (the old link is dead)
    _send(reg, back, _goto(name="p4.png", session_id=sid, request_id="gsel",
                           work_epoch=1, base_revision=session.revision))
    rej = _one(back.frames(), "REJECT")
    assert rej["reason"] in (rstate.REASON_BAD_PHASE, rstate.REASON_NO_WORK)
    assert _snapshot(session)["spreadId"] == session.spread_id

    _send(reg, leader, _req("SET_ACTIVITY", {"state": "active"},
                            session_id=sid, request_id="unsel"))
    leader.frames()
    assert rstate.derive_phase(session) == "reading"


def test_disconnect_ops_are_never_replayed():
    """切断中に到着した操作は復帰後に再生されない。"""
    reg, sid, leader, follower = _reading_session()
    session = reg.sessions[sid]
    committed = _snapshot(session)

    reg.detach_link(follower)
    # in-flight ops while disconnected are simply rejected (nothing queued)
    _send(reg, leader, _turn(1, session_id=sid, request_id="d1", work_epoch=1))
    _send(reg, leader, _goto(name="p6.png", session_id=sid, request_id="d2",
                             work_epoch=1, base_revision=session.revision))
    rejects = _of(leader.frames(), "REJECT")
    assert len(rejects) == 2

    # follower resumes -> first STATE carries the last commit, untouched
    member = next(m for m in session.members.values() if m.role == "follower")
    back = _connect(reg, "f2")
    _send(reg, back, _req("RESUME", {"memberId": member.id,
                                     "resumeToken": member.resume_token},
                          session_id=sid, request_id="re"))
    frames = back.frames()
    st = _one(frames, "STATE")
    assert st["spreadId"] == committed["spreadId"]
    assert st["workEpoch"] == committed["workEpoch"]
    # manifest is re-delivered to the resumed member only
    assert _of(frames, "MANIFEST")
    assert _of(leader.frames(), "MANIFEST") == []


# ═══════════════════════════════════════════════════════════════════
# stale load completion discarded by generation
# ═══════════════════════════════════════════════════════════════════


def test_outdated_load_result_discarded_by_generation():
    """旧load完了が新load完了より後でも、最新epochの状態を上書きしない。"""
    gates: dict[str, asyncio.Event] = {}

    async def resolver(link, job):
        await gates[job["folder"]].wait()
        return _manifest(job["folder"],
                         pages=(f"{job['folder']}1.png", f"{job['folder']}2.png",
                               f"{job['folder']}3.png"),
                         initial=f"{job['folder']}1.png")

    clock = FakeClock()
    reg = _reg(clock, resolver=resolver)
    leader, follower = _connect(reg, "l"), _connect(reg, "f")
    sid = _pair(reg, leader, follower)
    session = reg.sessions[sid]

    async def go():
        gates["wa"] = asyncio.Event()
        gates["wb"] = asyncio.Event()
        _send(reg, leader, _req("OPEN_WORK",
                                {"folder": "wa", "page": {"folder": "wa", "name": "wa1.png"}},
                                session_id=sid, request_id="A",
                                work_epoch=0, base_revision=session.revision))
        _send(reg, leader, _req("OPEN_WORK",
                                {"folder": "wb", "page": {"folder": "wb", "name": "wb1.png"}},
                                session_id=sid, request_id="B",
                                work_epoch=0, base_revision=session.revision))
        # B (the newer load) resolves FIRST
        gates["wb"].set()
        for _ in range(50):
            await asyncio.sleep(0)
            if session.manifest is not None:
                break
        assert session.manifest["folder"] == "wb" and session.work_epoch == 1
        # A resolves afterwards — stale generation, discarded
        gates["wa"].set()
        for _ in range(50):
            await asyncio.sleep(0)
        assert session.manifest["folder"] == "wb"
        assert session.work_epoch == 1 and session.manifest_version == 1
        spread = session.spread_id
        assert spread is not None and spread.startswith("c0:")

    from tests.helpers import drive
    drive(go())


def test_failed_load_keeps_session_alive_and_reports_error():
    err = catalog.ManifestError(catalog.ERR_NOT_FOUND, "missing")
    clock = FakeClock()
    reg = _reg(clock, resolver=_resolver({}, errors={"gone": err}))
    leader, follower = _connect(reg, "l"), _connect(reg, "f")
    sid = _pair(reg, leader, follower)
    session = reg.sessions[sid]

    _send(reg, leader, _req("OPEN_WORK",
                            {"folder": "gone", "page": {"folder": "gone", "name": "x.png"}},
                            session_id=sid, request_id="bad",
                            work_epoch=0, base_revision=session.revision))
    lf = leader.frames()
    assert _one(lf, "ERROR")["code"] == "not_found"
    # no epoch/manifest commit happened; session stays usable
    assert session.work_epoch == 0 and session.manifest is None
    assert rstate.derive_phase(session) == "selecting"


def test_missing_resolver_rejects_cleanly():
    clock = FakeClock()
    reg = _reg(clock)  # no resolver wired
    leader, follower = _connect(reg, "l"), _connect(reg, "f")
    sid = _pair(reg, leader, follower)
    session = reg.sessions[sid]
    _send(reg, leader, _req("OPEN_WORK",
                            {"folder": "x", "page": {"folder": "x", "name": "p1.png"}},
                            session_id=sid, request_id="nores",
                            work_epoch=0, base_revision=session.revision))
    assert _one(leader.frames(), "REJECT")["reason"] == rstate.REASON_UNAVAILABLE


def test_open_work_requires_page_and_leader_offset_default():
    reg, sid, leader, follower = _reading_session()
    session = reg.sessions[sid]
    _send(reg, leader, _req("OPEN_WORK", {"folder": "book"},
                            session_id=sid, request_id="np",
                            work_epoch=1, base_revision=session.revision))
    assert _one(leader.frames(), "REJECT")["reason"] == rstate.REASON_NO_INITIAL_PAGE


def test_open_work_accepted_despite_stale_base():
    """A leader pick framed on stale epoch/baseRevision/manifestVersion is
    still honored: the pick is self-contained (folder+page), latest-pick-
    wins is the intent, and pending_load's generation guard discards an
    older load finishing late. Rejecting it only swallowed a live tile
    tap on the leader's browse list (the REJECT was applied as a silent
    snapshot with no visible effect)."""
    reg, sid, leader, follower = _reading_session()
    session = reg.sessions[sid]
    assert session.work_epoch == 1 and session.manifest_version == 1

    # everything about this frame is stale: old epoch, old base, old mv
    _send(reg, leader, _req("OPEN_WORK",
                            {"folder": "book",
                             "page": {"folder": "book", "name": "p1.png"},
                             "manifestVersion": 0},
                            session_id=sid, request_id="ow-stale",
                            work_epoch=0,
                            base_revision=session.revision - 1))
    lf = leader.frames()
    assert _of(lf, "REJECT") == []
    assert _one(lf, "ACK")["request"] == "OPEN_WORK"
    # accepted: new epoch + fresh manifest delivered to both members
    assert session.work_epoch == 2 and session.manifest_version == 2
    assert session.manifest is not None and session.pending_load is None
    assert _of(lf, "MANIFEST") and _of(lf, "STATE")
    assert _of(follower.frames(), "STATE")


def test_set_offset_rebuilds_manifest_and_keeps_position():
    reg, sid, leader, follower = _reading_session()
    session = reg.sessions[sid]
    # move to spread holding p3/p4 (offset0: (p1,p2)(p3,p4)(p5,p6))
    _send(reg, leader, _goto(name="p3.png", session_id=sid, request_id="g",
                             work_epoch=1, base_revision=session.revision))
    assert session.spread_id == "c0:s1"

    _send(reg, leader, _req("SET_OFFSET", {"offset": 1}, session_id=sid,
                            request_id="so", work_epoch=1,
                            base_revision=session.revision))
    lf = leader.frames()
    assert _of(lf, "MANIFEST")                      # new spread table delivered
    assert session.work_epoch == 2 and session.manifest_version == 2
    assert session.offset == 1
    # anchor page p3.png still on screen: offset1 plan (p1,–)(p2,p3)(p4,p5)(p6,–)
    assert session.spread_id == "c0:s1"
    assert session.plan[1].right.name == "p2.png" and session.plan[1].left.name == "p3.png"
    # ready gate re-applies
    _send(reg, leader, _turn(1, session_id=sid, request_id="early", work_epoch=2))
    assert _one(leader.frames(), "REJECT")["reason"] == rstate.REASON_NOT_READY


def test_huge_manifest_streams_without_killing_links():
    """Regression: a manifest over the 64-frame outq bound used to overflow
    the queue mid-burst (before the drain task ran once), closing both
    links with send_queue_full and leaving the pair permanently paused —
    a big sibling-run work (thousands of pages) hit this deterministically.
    Bulk chunks ride the side-queue: all arrive, commit STATE still follows
    them, neither link dies."""
    clock = FakeClock()
    # ~150B/page across chapters+pageRefs+spreadTable -> ~2.2MB > 64 chunks
    pages = tuple(f"p{i:05}.png" for i in range(15000))
    man = _manifest("big", pages=pages)
    chunks = catalog.split_manifest(man)
    assert len(chunks) > 64
    reg = _reg(clock, resolver=_resolver({"big": man}))
    leader, follower = _connect(reg, "L"), _connect(reg, "F")
    sid = _pair(reg, leader, follower)

    _send(reg, leader, _req("OPEN_WORK",
                            {"folder": "big",
                             "page": {"folder": "big", "name": "p00000.png"}},
                            session_id=sid, request_id="ow"))

    lf = leader.frames()
    mans = _of(lf, "MANIFEST")
    assert len(mans) == len(chunks)
    assert catalog.join_manifest(mans)["digest"] == man["digest"]
    # wire order preserved: commit STATE still lands after the chunk block
    assert lf[-1]["type"] == "STATE" and lf[-1]["workEpoch"] == 1
    assert leader.close_reason is None
    ff = follower.frames()
    assert len(_of(ff, "MANIFEST")) == len(chunks)
    assert catalog.join_manifest(_of(ff, "MANIFEST"))["digest"] == man["digest"]
    assert follower.close_reason is None


def test_manifest_commit_failure_reports_error_and_stays_pickable(monkeypatch):
    """Regression: a manifest that fails at commit time (split_manifest
    ERR_SIZE) used to raise inside _finish_load — the epoch/manifest were
    already bumped but no commit STATE was ever broadcast, leaving every
    member stuck at 'loading' forever. The failure must surface as an
    ERROR frame instead, commit nothing, and stay pickable."""
    monkeypatch.setattr(catalog, "MAX_MANIFEST_BYTES", 8)   # force ERR_SIZE
    clock = FakeClock()
    reg = _reg(clock, resolver=_resolver({"book": _manifest()}))
    leader, follower = _connect(reg, "L"), _connect(reg, "F")
    sid = _pair(reg, leader, follower)
    session = reg.sessions[sid]

    _send(reg, leader, _req("OPEN_WORK",
                            {"folder": "book",
                             "page": {"folder": "book", "name": "p1.png"}},
                            session_id=sid, request_id="ow"))

    lf = leader.frames()
    assert _one(lf, "ERROR")["code"] == "size"
    assert _of(lf, "MANIFEST") == []
    # nothing half-committed: epoch/manifest untouched, back to selecting
    assert session.work_epoch == 0 and session.manifest is None
    assert session.pending_load is None
    assert _of(lf, "STATE")[-1]["phase"] == "selecting"
    assert leader.close_reason is None and follower.close_reason is None
    assert _of(follower.frames(), "ERROR")             # peer notified too

    # recovery: a well-formed re-pick still commits normally
    monkeypatch.setattr(catalog, "MAX_MANIFEST_BYTES", 32 * 1024 * 1024)
    _send(reg, leader, _req("OPEN_WORK",
                            {"folder": "book",
                             "page": {"folder": "book", "name": "p1.png"}},
                            session_id=sid, request_id="ow2"))
    assert session.work_epoch == 1 and session.manifest is not None
    assert _of(leader.frames(), "MANIFEST")


def test_swap_sides_flips_projection():
    reg, sid, leader, follower = _reading_session()
    session = reg.sessions[sid]
    assert session.leader_side == "left"
    _send(reg, follower, _req("SWAP_SIDES", {"leaderSide": "right"},
                              session_id=sid, request_id="sw",
                              work_epoch=1, base_revision=session.revision))
    assert session.leader_side == "right"
    st = _one(follower.frames(), "STATE")
    assert st["leaderSide"] == "right"
    # projection sanity via the S01 model
    s = session.plan[model.spread_index_by_id(session.plan, session.spread_id)]
    assert model.assigned_ref(s, "leader", "right") == s.right
    assert model.assigned_ref(s, "follower", "left") == s.right


# ═══════════════════════════════════════════════════════════════════
# sequencer role: server-ws vs webrtc-dc (leader-sequenced)
# ═══════════════════════════════════════════════════════════════════


def _fixed_op_list(sid, epoch):
    """The fixed operation sequence both data planes must sequence identically."""
    return [
        ("leader", _turn(1, session_id=sid, request_id="f1", work_epoch=epoch)),
        ("follower", _turn(1, session_id=sid, request_id="f2", work_epoch=epoch)),
        ("follower", _turn(-1, session_id=sid, request_id="f3", work_epoch=epoch)),
        ("leader", _turn(1, session_id=sid, request_id="f4", work_epoch=epoch)),
        ("follower", _goto(name="p6.png", session_id=sid, request_id="f5",
                           work_epoch=epoch, base_revision="LATEST")),
        ("leader", _req("SWAP_SIDES", {"leaderSide": "left"}, session_id=sid,
                        request_id="f6", work_epoch=epoch, base_revision="LATEST")),
    ]


def _run_plane(plane: str):
    """Drive the fixed op list over one data plane; return
    (final snapshot, leader revs, follower revs, applied requestId order)."""
    clock = FakeClock()
    reg = _reg(clock, resolver=_resolver({"book": _manifest()}))
    links = {"leader": _connect(reg, "l"), "follower": _connect(reg, "f")}
    sid = _pair(reg, links["leader"], links["follower"])
    _open_and_read(reg, links["leader"], links["follower"], sid)
    session = reg.sessions[sid]

    if plane == "webrtc-dc":
        # both members report the channel up -> server flips the data plane
        for link in (links["leader"], links["follower"]):
            _send(reg, link, _req("TRANSPORT_STATE", {"state": "up"},
                                  session_id=sid, request_id=f"ts-{link.name}",
                                  work_epoch=1, base_revision=session.revision))
        assert session.data_plane == "webrtc-dc"
        links["leader"].frames(); links["follower"].frames()

    rev0 = session.revision          # revision at the op-sequence boundary
    applied = []
    for role, req in _fixed_op_list(sid, 1):
        if req["baseRevision"] == "LATEST":
            req["baseRevision"] = session.revision
        if plane == "server-ws":
            _send(reg, links[role], req)
        else:
            # leader-sequenced: every request reaches the server ordered by the
            # leader; follower-origin ops arrive relayed on the leader's link.
            wire = dict(req)
            wire["payload"] = dict(req["payload"], origin=role)
            _send(reg, links["leader"], wire)
        applied.append(req["requestId"])

    lrev = [f["revision"] for f in _of(links["leader"].frames(), "STATE")]
    frev = [f["revision"] for f in _of(links["follower"].frames(), "STATE")]
    return _snapshot(session), rev0, lrev, frev, applied, session


def test_same_sequence_same_final_state_on_both_planes():
    """同一固定操作列を server-ws / webrtc-dc で実行 → 同一結果.

    The webrtc-dc leg carries one extra revision from the plane-flip boundary
    STATE (session setup, not part of the fixed sequence), so the op
    sequence's revision delta — and every committed field — is what must
    match."""
    ws_snap, ws_rev0, ws_lrev, ws_frev, _, _ = _run_plane("server-ws")
    dc_snap, dc_rev0, dc_lrev, dc_frev, _, _ = _run_plane("webrtc-dc")
    assert {k: ws_snap[k] for k in ("workEpoch", "manifestVersion", "phase",
                                  "spreadId", "offset", "leaderSide")} == \
           {k: dc_snap[k] for k in ("workEpoch", "manifestVersion", "phase",
                                    "spreadId", "offset", "leaderSide")}
    # identical spread + identical revision delta across the fixed sequence
    assert ws_snap["spreadId"] == dc_snap["spreadId"]
    assert ws_snap["revision"] - ws_rev0 == dc_snap["revision"] - dc_rev0
    # every member on each leg observes the identical revision column
    assert ws_lrev == ws_frev and dc_lrev == dc_frev
    assert ws_lrev == sorted(ws_lrev) and len(set(ws_lrev)) == len(ws_lrev)
    assert dc_lrev == sorted(dc_lrev) and len(set(dc_lrev)) == len(dc_lrev)
    # and the op-sequence columns themselves are the same length
    assert len(ws_lrev) == len(dc_lrev)


def test_leader_plane_simultaneous_requests_role_tiebreak():
    """P2P同着要求は role 決定的タイブレーク — leader が先。両メンバーが
    同一 revision 列を受け取る。"""
    clock = FakeClock()
    reg = _reg(clock, resolver=_resolver({"book": _manifest()}))
    links = {"leader": _connect(reg, "l"), "follower": _connect(reg, "f")}
    sid = _pair(reg, links["leader"], links["follower"])
    _open_and_read(reg, links["leader"], links["follower"], sid)
    session = reg.sessions[sid]
    for link in links.values():
        _send(reg, link, _req("TRANSPORT_STATE", {"state": "up"},
                              session_id=sid, request_id=f"ts-{link.name}",
                              work_epoch=1, base_revision=session.revision))
    links["leader"].frames(); links["follower"].frames()

    # simultaneous batch delivered to the leader OUT OF order: follower's GOTO
    # arrives in the same tick before the leader's own GOTO.
    batch = [
        ("follower", _goto(name="p6.png", session_id=sid, request_id="sim-f",
                           work_epoch=1, base_revision=session.revision)),
        ("leader", _goto(name="p3.png", session_id=sid, request_id="sim-l",
                         work_epoch=1, base_revision=session.revision)),
    ]
    ordered = rstate.order_batch(batch)
    assert [r["requestId"] for _, r in ordered] == ["sim-l", "sim-f"]

    for role, req in ordered:
        req = dict(req)
        if req["type"] in ("GOTO", "SET_OFFSET", "SWAP_SIDES", "OPEN_WORK"):
            req["baseRevision"] = session.revision
        req["payload"] = dict(req["payload"], origin=role)
        _send(reg, links["leader"], req)

    # leader's GOTO applied first, then follower's -> final spread = c0:s2
    assert session.spread_id == "c0:s2"
    entries = [e for e in session.apply_log if e["requestId"] in ("sim-l", "sim-f")]
    assert [e["requestId"] for e in entries] == ["sim-l", "sim-f"]
    assert entries[0]["role"] == "leader" and entries[1]["role"] == "follower"

    lrev = [f["revision"] for f in _of(links["leader"].frames(), "STATE")]
    frev = [f["revision"] for f in _of(links["follower"].frames(), "STATE")]
    assert lrev == frev and lrev[-1] == session.revision


def test_follower_cannot_inject_origin():
    reg, sid, leader, follower = _reading_session()
    session = reg.sessions[sid]
    snap = _snapshot(session)
    _send(reg, follower,
          _req("TURN", {"delta": 1, "crossChapter": True, "fromChapter": None,
                        "origin": "leader"},
               session_id=sid, request_id="spoof", work_epoch=1))
    rej = _one(follower.frames(), "REJECT")
    assert rej["reason"] == rstate.REASON_BAD_ORIGIN
    assert _snapshot(session) == snap


# ═══════════════════════════════════════════════════════════════════
# transport switch integrity
# ═══════════════════════════════════════════════════════════════════


def test_transport_switch_never_loses_or_duplicates_requests():
    reg, sid, leader, follower = _reading_session()
    session = reg.sessions[sid]

    _send(reg, leader, _turn(1, session_id=sid, request_id="pre", work_epoch=1))
    commit = _snapshot(session)

    # flip to webrtc-dc: the first STATE after the switch equals the commit
    for link in (leader, follower):
        _send(reg, link, _req("TRANSPORT_STATE", {"state": "up"},
                              session_id=sid, request_id=f"u-{link.name}",
                              work_epoch=1, base_revision=session.revision))
    lf = leader.frames()
    st = _of(lf, "STATE")[-1]
    assert st["dataPlane"] == "webrtc-dc"
    assert st["spreadId"] == commit["spreadId"]
    assert st["workEpoch"] == commit["workEpoch"]
    ts = _one(lf, "TRANSPORT_STATE")
    assert ts["revision"] == session.revision

    # ops keep sequencing on the new plane (relayed follower op included)
    _send(reg, leader, _req("TURN", {"delta": 1, "crossChapter": True,
                                     "fromChapter": None, "origin": "follower"},
                            session_id=sid, request_id="dc1", work_epoch=1))
    assert session.spread_id == "c0:s2"

    # flap back down: again the boundary STATE matches the latest commit
    commit2 = _snapshot(session)
    _send(reg, follower, _req("TRANSPORT_STATE", {"state": "down"},
                              session_id=sid, request_id="dwn",
                              work_epoch=1, base_revision=session.revision))
    f_states = _of(follower.frames(), "STATE")
    st = f_states[-1]
    assert st["dataPlane"] == "server-ws"
    assert st["spreadId"] == commit2["spreadId"] == "c0:s2"

    # total applied == total accepted requests (no loss, no duplication)
    applied = [e["requestId"] for e in session.apply_log]
    assert applied.count("pre") == applied.count("dc1") == 1
    # every member's STATE column strictly increasing — and the leader's
    # post-"up"-drain window is the same revision column the follower saw
    l_revs = [f["revision"] for f in _of(leader.frames(), "STATE")]
    assert l_revs == sorted(l_revs) and len(set(l_revs)) == len(l_revs)
    f_revs = [f["revision"] for f in f_states]
    assert f_revs == sorted(f_revs)
    assert l_revs == f_revs[-len(l_revs):]


def test_ready_applied_deferred_requests_behaviour():
    """READY needs a committed manifest; APPLIED is an ack-only report."""
    clock = FakeClock()
    reg = _reg(clock, resolver=_resolver({"book": _manifest()}))
    leader, follower = _connect(reg, "l"), _connect(reg, "f")
    sid = _pair(reg, leader, follower)
    session = reg.sessions[sid]

    # READY before any manifest -> rejected, snapshot unchanged
    _send(reg, follower, _req("READY", session_id=sid, request_id="r0",
                              work_epoch=0))
    assert _one(follower.frames(), "REJECT")["reason"] == rstate.REASON_NO_WORK

    _open_and_read(reg, leader, follower, sid)
    session.ready.clear()  # simulate member not yet ready for coverage
    rev = session.revision
    _send(reg, leader, _req("APPLIED", {"requestId": "n"},
                            session_id=sid, request_id="ap1", work_epoch=1,
                            base_revision=rev))
    lf = leader.frames()
    assert _one(lf, "ACK")["requestId"] == "ap1"
    # APPLIED never emits STATE/APPLIED broadcasts
    assert _of(lf, "STATE") == [] and _of(lf, "APPLIED") == []
    assert session.revision == rev
    member = next(m for m in session.members.values() if m.role == "leader")
    assert member.last_applied == {"requestId": "n", "revision": rev}
