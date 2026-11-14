"""S05 — sequencer/state over real reader-sync sockets (#80, #485 Phase 2c port).

Two live sockets against the session server fixture: the wire-visible
sequencer contract — fixed op sequences, duplicate replay, staleness
rejections with snapshots, OPEN_WORK epoch flow, SET_OFFSET rebuilds and
SWAP_SIDES projection.

The in-process sequencer unit matrix (fake resolver, generation races,
load timeouts, role tie-break internals) stays in
test_reader_state_internals.py, which retires with the Python backend.
Wire constants are inlined from the asyncapi contract instead of the
Python package so this file runs in both lanes.
"""
import hashlib
import json
import time

from websockets.sync.client import connect as ws_connect

# Wire constants inlined from the asyncapi contract (RequestFrame.v const 1).
PROTOCOL_VERSION = 1

_WS_PATH = "/ws/reader-sync"

_CONTENT_KEYS = ("v", "folder", "offset", "chapters", "pageRefs", "spreadTable")


def _canonical(obj) -> str:
    """Deterministic JSON: sorted keys, compact separators, ASCII-only."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _join_manifest(chunks) -> dict:
    """Reassemble split_manifest chunk dicts without the Python package."""
    if not isinstance(chunks, (list, tuple)) or not chunks:
        raise ValueError("no manifest chunks")
    count = None
    digest = None
    parts: dict[int, str] = {}
    for ch in chunks:
        if (not isinstance(ch, dict)
                or ch.get("v") != PROTOCOL_VERSION
                or ch.get("kind") != "manifest-chunk"
                or not isinstance(ch.get("index"), int)
                or isinstance(ch.get("index"), bool)
                or not isinstance(ch.get("count"), int)
                or isinstance(ch.get("count"), bool)
                or not isinstance(ch.get("digest"), str)
                or not isinstance(ch.get("data"), str)):
            raise ValueError("malformed manifest chunk")
        if count is None:
            count, digest = ch["count"], ch["digest"]
        elif (ch["count"], ch["digest"]) != (count, digest):
            raise ValueError("inconsistent manifest chunk set")
        if ch["index"] in parts:
            raise ValueError("duplicate manifest chunk index")
        parts[ch["index"]] = ch["data"]
    if count != len(chunks) or sorted(parts) != list(range(count)):
        raise ValueError("manifest chunk set incomplete")
    manifest = json.loads("".join(parts[i] for i in range(count)))
    if not isinstance(manifest, dict) or manifest.get("digest") != digest:
        raise ValueError("manifest digest field mismatch")
    content = {k: manifest[k] for k in _CONTENT_KEYS}
    recomputed = "sha256:" + hashlib.sha256(
        _canonical(content).encode("ascii")).hexdigest()
    if recomputed != digest:
        raise ValueError("manifest digest verification failed")
    return manifest


# conftest tree: 1/2/3/10/11.png — offset0 spreads c0:s0=(1,2) c0:s1=(3,10)
WORK = "simple_manga"
FIRST_PAGE = {"folder": WORK, "name": "1.png"}


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


def _turn(delta=1, cross=True, chapter=None, **kw):
    return _req("TURN", {"delta": delta, "crossChapter": cross,
                         "fromChapter": chapter}, **kw)


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


def _pair2(ws_a, ws_b):
    """CREATE/JOIN/APPROVE; returns (session_id, post-approve revision)."""
    ws_a.send(_req("CREATE", rid="c"))
    data = _expect(ws_a, "ACK")["data"]
    ws_b.send(_req("JOIN", {"code": data["code"]}, rid="j"))
    joined = _expect(ws_b, "ACK")["data"]
    _expect(ws_a, "JOIN_REQUEST")
    ws_a.send(_req("APPROVE", {"memberId": joined["memberId"]},
                   sid=data["sessionId"], rid="a"))
    st = _expect(ws_a, "STATE")
    _expect(ws_b, "STATE")
    return data["sessionId"], st["revision"]


def _open_and_ready(ws_a, ws_b, sid, rev):
    """OPEN_WORK + both READY -> reading; returns current revision."""
    ws_a.send(_req("OPEN_WORK", {"folder": WORK, "page": FIRST_PAGE},
                   sid=sid, rid="ow", epoch=0, base=rev))
    st, seen_a = _drain_until(
        ws_a, lambda f: f.get("type") == "STATE" and f.get("workEpoch") == 1)
    chunks = [f for f in seen_a if f["type"] == "MANIFEST"]
    assert chunks, "manifest must be chunked-delivered before/at commit"
    manifest = _join_manifest(chunks)
    assert manifest["folder"] == WORK
    _, seen_b = _drain_until(
        ws_b, lambda f: f.get("type") == "STATE" and f.get("workEpoch") == 1)
    assert [f["data"] for f in seen_b if f["type"] == "MANIFEST"] == \
        [f["data"] for f in chunks]
    ws_a.send(_turn(1, sid=sid, rid="early", epoch=1))
    assert _expect(ws_a, "REJECT")["reason"] == "not_ready"
    for ws, rid in ((ws_a, "ra"), (ws_b, "rb")):
        ws.send(_req("READY", {"manifestVersion": 1}, sid=sid, rid=rid, epoch=1))
    sa, _ = _drain_until(ws_a, lambda f: f.get("type") == "STATE"
                         and f.get("phase") == "reading")
    sb, _ = _drain_until(ws_b, lambda f: f.get("type") == "STATE"
                         and f.get("phase") == "reading")
    assert sa["spreadId"] == sb["spreadId"] == "c0:s0"
    return sa["revision"]


# ── fixed op sequences ─────────────────────────────────────────────


def test_simultaneous_nexts_advance_two_spreads(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, rev = _pair2(ws_a, ws_b)
        rev = _open_and_ready(ws_a, ws_b, sid, rev)
        ws_a.send(_turn(1, sid=sid, rid="n1", epoch=1))
        ws_b.send(_turn(1, sid=sid, rid="n2", epoch=1))
        end_a, seen_a = _drain_until(
            ws_a, lambda f: f.get("type") == "STATE" and f.get("spreadId") == "c0:s2")
        end_b, seen_b = _drain_until(
            ws_b, lambda f: f.get("type") == "STATE" and f.get("spreadId") == "c0:s2")
        revs_a = [f["revision"] for f in seen_a if f["type"] == "STATE"]
        revs_b = [f["revision"] for f in seen_b if f["type"] == "STATE"]
        assert revs_a == revs_b
        assert revs_a == sorted(revs_a) and len(set(revs_a)) == len(revs_a)
        assert end_a["revision"] == end_b["revision"] > rev
        # revision never regresses on either member
        assert all(b > a for a, b in zip(revs_a, revs_a[1:]))


def test_next_then_prev_returns_to_spread(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, rev = _pair2(ws_a, ws_b)
        _open_and_ready(ws_a, ws_b, sid, rev)
        ws_b.send(_turn(1, sid=sid, rid="n", epoch=1))
        _drain_until(ws_b, lambda f: f.get("type") == "STATE"
                     and f.get("spreadId") == "c0:s1")
        ws_b.send(_turn(-1, sid=sid, rid="p", epoch=1))
        st, _ = _drain_until(ws_b, lambda f: f.get("type") == "STATE"
                             and f.get("spreadId") == "c0:s0")
        assert st["phase"] == "reading"


def test_boundary_turns_clamp_but_still_commit(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, rev = _pair2(ws_a, ws_b)
        rev = _open_and_ready(ws_a, ws_b, sid, rev)
        ws_a.send(_turn(-1, sid=sid, rid="p0", epoch=1))
        st, _ = _drain_until(ws_a, lambda f: f.get("type") == "STATE"
                             and f.get("revision") == rev + 1)
        assert st["spreadId"] == "c0:s0"


# ── dedup / staleness — snapshot exactly unchanged on reject ───────


def test_duplicate_request_replays_ack_without_state(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, rev = _pair2(ws_a, ws_b)
        _open_and_ready(ws_a, ws_b, sid, rev)
        frame = _turn(1, sid=sid, rid="dup", epoch=1)
        ws_b.send(frame)
        ack1 = _expect(ws_b, "ACK")
        st, _ = _drain_until(ws_b, lambda f: f.get("type") == "STATE"
                             and f.get("spreadId") == "c0:s1")
        assert st["revision"] > rev
        _drain_until(ws_a, lambda f: f.get("type") == "STATE"
                     and f.get("spreadId") == "c0:s1")
        ws_b.send(frame)
        ws_b.send(_req("PING", {"seq": 42}, rid="pk"))
        seen = []
        deadline = time.time() + 10
        while True:
            f = _recv(ws_b, timeout=max(0.1, deadline - time.time()))
            seen.append(f)
            if f["type"] == "PONG" and f.get("seq") == 42:
                break
            assert time.time() < deadline
        assert [f["type"] for f in seen] == ["ACK", "PONG"]
        assert seen[0]["requestId"] == ack1["requestId"] == "dup"


def test_stale_epoch_and_base_rejected_with_snapshot(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, rev = _pair2(ws_a, ws_b)
        rev = _open_and_ready(ws_a, ws_b, sid, rev)
        ws_b.send(_turn(1, sid=sid, rid="se", epoch=0))
        rej = _expect(ws_b, "REJECT")
        assert rej["reason"] == "stale_epoch"
        assert rej["snapshot"]["workEpoch"] == 1
        ws_a.send(_req("GOTO", {"page": {"folder": WORK, "name": "11.png"}},
                       sid=sid, rid="sb", epoch=1, base=1))
        rej = _expect(ws_a, "REJECT")
        assert rej["reason"] == "stale_revision"
        assert rej["snapshot"]["revision"] == rev
        ws_a.send(_turn(1, sid=sid, rid="ok", epoch=1))
        st = _expect(ws_a, "STATE")
        assert st["revision"] == rev + 1 and st["spreadId"] == "c0:s1"


def test_foreign_session_request_rejected(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, rev = _pair2(ws_a, ws_b)
        rev = _open_and_ready(ws_a, ws_b, sid, rev)
        ws_a.send(_turn(1, sid="deadbeef", rid="fx", epoch=1))
        assert _expect(ws_a, "REJECT")["reason"] == "wrong_session"
        with _ws(server) as ws_x:
            _recv(ws_x)
            ws_x.send(_turn(1, sid=sid, rid="x2", epoch=1))
            assert _expect(ws_x, "REJECT")["reason"] == "no_session"


def test_goto_unknown_page_rejected_snapshot_untouched(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, rev = _pair2(ws_a, ws_b)
        rev = _open_and_ready(ws_a, ws_b, sid, rev)
        ws_a.send(_req("GOTO", {"page": {"folder": WORK, "name": "zzz.png"}},
                       sid=sid, rid="gr", epoch=1, base=rev))
        rej = _expect(ws_a, "REJECT")
        assert rej["reason"] == "range"
        assert rej["snapshot"]["spreadId"] == "c0:s0"


# ── OPEN_WORK / READY / phases ─────────────────────────────────────


def test_follower_open_work_rejected_leader_flow(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, rev = _pair2(ws_a, ws_b)
        rev = _open_and_ready(ws_a, ws_b, sid, rev)
        ws_b.send(_req("OPEN_WORK", {"folder": WORK, "page": FIRST_PAGE},
                       sid=sid, rid="fo", epoch=1, base=rev))
        rej = _expect(ws_b, "REJECT")
        assert rej["reason"] == "not_leader"


def test_open_work_new_epoch_manifest_and_ready_gate(server):
    other = "series/vol1"
    other_page = {"folder": other, "name": "p1.png"}
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, rev = _pair2(ws_a, ws_b)
        rev = _open_and_ready(ws_a, ws_b, sid, rev)
        ws_a.send(_req("OPEN_WORK", {"folder": other, "page": other_page},
                       sid=sid, rid="owb", epoch=1, base=rev))
        st, seen = _drain_until(
            ws_a, lambda f: f.get("type") == "STATE" and f.get("workEpoch") == 2)
        mans = [f for f in seen if f["type"] == "MANIFEST"]
        assert mans and all(m["workEpoch"] == 2 for m in mans)
        assert st["phase"] == "loading"
        ws_b.send(_turn(1, sid=sid, rid="old-ep", epoch=1))
        rej = _expect(ws_b, "REJECT")
        assert rej["reason"] in ("stale_epoch", "not_ready")
        for ws, rid in ((ws_a, "rl2"), (ws_b, "rf2")):
            ws.send(_req("READY", {"manifestVersion": 2},
                         sid=sid, rid=rid, epoch=2))
        last, _ = _drain_until(ws_b, lambda f: f.get("type") == "STATE"
                               and f.get("phase") == "reading")
        assert last["workEpoch"] == 2
        ws_b.send(_turn(1, sid=sid, rid="n", epoch=2))
        moved, _ = _drain_until(ws_b, lambda f: f.get("type") == "STATE"
                                and f["revision"] > last["revision"])
        assert moved["spreadId"] != last["spreadId"] or \
            moved["revision"] == last["revision"] + 1


def test_open_work_accepted_despite_stale_base(server):
    """A leader pick framed on a stale base is still honored (pick wins)."""
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, rev = _pair2(ws_a, ws_b)
        _open_and_ready(ws_a, ws_b, sid, rev)
        ws_a.send(_req("OPEN_WORK", {"folder": WORK, "page": FIRST_PAGE},
                       sid=sid, rid="ow-stale", epoch=0, base=rev - 1))
        ack = _expect(ws_a, "ACK")
        assert ack["request"] == "OPEN_WORK"
        st, seen = _drain_until(
            ws_a, lambda f: f.get("type") == "STATE" and f.get("workEpoch") == 2)
        assert _of(seen, "MANIFEST")


def test_set_offset_rebuilds_manifest_and_keeps_position(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, rev = _pair2(ws_a, ws_b)
        rev = _open_and_ready(ws_a, ws_b, sid, rev)
        ws_a.send(_req("GOTO", {"page": {"folder": WORK, "name": "3.png"}},
                       sid=sid, rid="g", epoch=1, base=rev))
        st, _ = _drain_until(ws_a, lambda f: f.get("type") == "STATE"
                             and f.get("spreadId") == "c0:s1")
        rev = st["revision"]
        ws_a.send(_req("SET_OFFSET", {"offset": 1},
                       sid=sid, rid="so", epoch=1, base=rev))
        st2, seen = _drain_until(
            ws_a, lambda f: f.get("type") == "STATE" and f.get("workEpoch") == 2)
        assert _of(seen, "MANIFEST")
        assert st2["offset"] == 1
        assert st2["spreadId"] == "c0:s1"
        ws_a.send(_turn(1, sid=sid, rid="early", epoch=2))
        assert _expect(ws_a, "REJECT")["reason"] == "not_ready"


def test_swap_sides_flips_projection(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, rev = _pair2(ws_a, ws_b)
        rev = _open_and_ready(ws_a, ws_b, sid, rev)
        ws_b.send(_req("SWAP_SIDES", {"leaderSide": "right"},
                       sid=sid, rid="sw", epoch=1, base=rev))
        st = _expect(ws_b, "STATE")
        assert st["leaderSide"] == "right"
