"""S05 — sequencer/state over real WebSocket clients (#80).

Two independent websockets.sync clients against the live session `server`
fixture (BOOKQ_TEST_MODE mock auth): the fixed operation sequences from the
issue's 完了条件 exercised on the real transport — pairing, OPEN_WORK
manifest delivery, READY gating, page ops from both members,
duplicate/stale/foreign rejections carrying the latest snapshot,
disconnect-resume without replay, and data-plane flips.

Runs in both lanes (default uvicorn + BOOKQ_TEST_SERVER=rs): wire constants
are inlined from the asyncapi contract (v=1) and the manifest reassembly
below mirrors catalog.join_manifest without importing app.* (#486 Phase 2c).
"""
import hashlib
import json
import time

from websockets.sync.client import connect as ws_connect

# Wire constants inlined from api/asyncapi.yaml (RequestFrame.v const 1) —
# importing app.reader_sync here would quarantine this file under
# BOOKQ_TEST_SERVER=rs, where the whole suite oracles the viewer-rs backend.
PROTOCOL_VERSION = 1

_WS_PATH = "/ws/reader-sync"

_CONTENT_KEYS = ("v", "folder", "offset", "chapters", "pageRefs", "spreadTable")


def _canonical(obj) -> str:
    """Deterministic JSON: sorted keys, compact separators, ASCII-only."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _join_manifest(chunks) -> dict:
    """Reassemble split_manifest chunk dicts; mirrors
    app/reader_sync/catalog.join_manifest (count/index/digest checks plus
    digest re-verification) without importing app.* so both lanes use it."""
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

# conftest downloads_tree fixture: 1/2/3/10/11.png — offset0 spreads
# c0:s0=(1,2) c0:s1=(3,10) c0:s2=(11,–)
WORK = "simple_manga"
FIRST_PAGE = {"folder": WORK, "name": "1.png"}


def _ws(base_url: str):
    return ws_connect(base_url.replace("http", "ws") + _WS_PATH)


def _recv(ws, timeout: float = 5.0) -> dict:
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


def _drain_until(ws, pred, timeout: float = 5.0):
    """Read frames until one satisfies pred; returns (match, all frames)."""
    deadline = time.time() + timeout
    seen = []
    while time.time() < deadline:
        f = _recv(ws, timeout=max(0.1, deadline - time.time()))
        seen.append(f)
        if pred(f):
            return f, seen
    raise AssertionError(f"no matching frame within {timeout}s; saw "
                         f"{[f.get('type') for f in seen]}")


def _expect(ws, rtype: str, timeout: float = 5.0):
    return _drain_until(ws, lambda f: f.get("type") == rtype, timeout)[0]


def _last_state(ws, timeout: float = 5.0):
    f, _ = _drain_until(ws, lambda f: f.get("type") == "STATE", timeout)
    return f


def _pair(ws_a, ws_b):
    """Full pairing; returns (session_id, leader_member, follower_member,
    revision) where revision is the post-APPROVE STATE revision both members
    committed to."""
    ws_a.send(_req("CREATE", rid="c"))
    data = _expect(ws_a, "ACK")["data"]
    ws_b.send(_req("JOIN", {"code": data["code"]}, rid="j"))
    joined = _expect(ws_b, "ACK")["data"]
    _expect(ws_a, "JOIN_REQUEST")
    ws_a.send(_req("APPROVE", {"memberId": joined["memberId"]},
                   sid=data["sessionId"], rid="a"))
    st = _expect(ws_a, "STATE")
    _expect(ws_b, "STATE")
    return data["sessionId"], data["memberId"], joined["memberId"], st["revision"]


def _open_and_ready(ws_a, ws_b, sid, rev):
    """Leader OPEN_WORK -> both READY -> reading. Returns current revision."""
    ws_a.send(_req("OPEN_WORK", {"folder": WORK, "page": FIRST_PAGE},
                   sid=sid, rid="ow", epoch=0, base=rev))
    # commit STATE: workEpoch flips to 1 once the manifest lands
    st, seen_a = _drain_until(
        ws_a, lambda f: f.get("type") == "STATE" and f.get("workEpoch") == 1)
    manifest_chunks = [f for f in seen_a if f["type"] == "MANIFEST"]
    assert manifest_chunks, "manifest must be chunked-delivered before/at commit"
    manifest = _join_manifest(manifest_chunks)
    assert manifest["folder"] == WORK
    assert manifest["digest"].startswith("sha256:")
    # follower receives the same manifest + STATE
    fst, seen_b = _drain_until(
        ws_b, lambda f: f.get("type") == "STATE" and f.get("workEpoch") == 1)
    assert st["manifestVersion"] == fst["manifestVersion"] == 1
    assert [f["data"] for f in seen_b if f["type"] == "MANIFEST"] == \
        [f["data"] for f in manifest_chunks]
    # page input gated until both READY
    ws_a.send(_req("TURN", {"delta": 1, "crossChapter": True, "fromChapter": None},
                   sid=sid, rid="early", epoch=1))
    rej = _expect(ws_a, "REJECT")
    assert rej["reason"] == "not_ready"
    for ws, rid in ((ws_a, "ra"), (ws_b, "rb")):
        ws.send(_req("READY", {"manifestVersion": 1}, sid=sid, rid=rid, epoch=1))
    sa, _ = _drain_until(ws_a, lambda f: f.get("type") == "STATE"
                         and f.get("phase") == "reading")
    sb, _ = _drain_until(ws_b, lambda f: f.get("type") == "STATE"
                         and f.get("phase") == "reading")
    assert sa["spreadId"] == sb["spreadId"] == "c0:s0"
    return sa["revision"]


# ── fixed op sequences over the wire ───────────────────────────────


def test_reading_flow_and_fixed_ops(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)  # HELLO
        sid, _, _, rev = _pair(ws_a, ws_b)
        rev = _open_and_ready(ws_a, ws_b, sid, rev)

        # simultaneous NEXT x2 -> +2 spreads; identical revision stream on both
        ws_a.send(_req("TURN", {"delta": 1, "crossChapter": True,
                                "fromChapter": None},
                       sid=sid, rid="n1", epoch=1))
        ws_b.send(_req("TURN", {"delta": 1, "crossChapter": True,
                                "fromChapter": None},
                       sid=sid, rid="n2", epoch=1))
        end_a, seen_a = _drain_until(
            ws_a, lambda f: f.get("type") == "STATE" and f.get("spreadId") == "c0:s2")
        end_b, seen_b = _drain_until(
            ws_b, lambda f: f.get("type") == "STATE" and f.get("spreadId") == "c0:s2")
        revs_a = [f["revision"] for f in seen_a if f["type"] == "STATE"]
        revs_b = [f["revision"] for f in seen_b if f["type"] == "STATE"]
        assert revs_a == revs_b
        assert revs_a == sorted(revs_a) and len(set(revs_a)) == len(revs_a)
        assert {f["requestId"] for f in seen_a if f["type"] == "APPLIED"} == {"n1", "n2"}
        assert end_a["revision"] == end_b["revision"] > rev

        # NEXT then PREV returns (boundary-free position)
        ws_a.send(_req("TURN", {"delta": -1, "crossChapter": True,
                                "fromChapter": None},
                       sid=sid, rid="p1", epoch=1))
        _drain_until(ws_a, lambda f: f.get("type") == "STATE"
                     and f.get("spreadId") == "c0:s1")

        # tail NEXT clamps — spread unchanged, revision still commits
        ws_b.send(_req("TURN", {"delta": -1, "crossChapter": True,
                                "fromChapter": None},
                       sid=sid, rid="p2", epoch=1))
        st, _ = _drain_until(ws_b, lambda f: f.get("type") == "STATE"
                         and f.get("spreadId") == "c0:s0")
        ws_b.send(_req("TURN", {"delta": -1, "crossChapter": True,
                                "fromChapter": None},
                       sid=sid, rid="p3", epoch=1))
        st2, _ = _drain_until(ws_b, lambda f: f.get("type") == "STATE"
                          and f.get("spreadId") == "c0:s0"
                          and f.get("revision") > st["revision"])
        assert st2["spreadId"] == "c0:s0"


def test_rejections_carry_snapshot_over_ws(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, leader_mid, _, rev0 = _pair(ws_a, ws_b)
        rev = _open_and_ready(ws_a, ws_b, sid, rev0)

        # follower OPEN_WORK -> not_leader, snapshot unchanged
        ws_b.send(_req("OPEN_WORK", {"folder": WORK, "page": FIRST_PAGE},
                       sid=sid, rid="fo", epoch=1, base=rev))
        rej = _expect(ws_b, "REJECT")
        assert rej["reason"] == "not_leader"
        assert rej["snapshot"]["revision"] == rev
        assert rej["snapshot"]["spreadId"] == "c0:s0"

        # stale workEpoch -> stale_epoch + snapshot
        ws_a.send(_req("TURN", {"delta": 1, "crossChapter": True,
                                "fromChapter": None},
                       sid=sid, rid="se", epoch=0))
        rej = _expect(ws_a, "REJECT")
        assert rej["reason"] == "stale_epoch"
        assert rej["snapshot"]["workEpoch"] == 1

        # stale baseRevision on an absolute op -> stale_revision
        ws_a.send(_req("GOTO", {"page": {"folder": WORK, "name": "11.png"}},
                       sid=sid, rid="sb", epoch=1, base=1))
        rej = _expect(ws_a, "REJECT")
        assert rej["reason"] == "stale_revision"
        assert rej["snapshot"]["revision"] == rev

        # out-of-range GOTO -> typed reject, snapshot untouched
        ws_a.send(_req("GOTO", {"page": {"folder": WORK, "name": "zzz.png"}},
                       sid=sid, rid="gr", epoch=1, base=rev))
        rej = _expect(ws_a, "REJECT")
        assert rej["reason"] == "range"
        assert rej["snapshot"]["spreadId"] == "c0:s0"

        # foreign session id -> wrong_session
        ws_a.send(_req("TURN", {"delta": 1, "crossChapter": True,
                                "fromChapter": None},
                       sid="deadbeef", rid="fx", epoch=1))
        assert _expect(ws_a, "REJECT")["reason"] == "wrong_session"

        # nothing applied: next legit op still lands on the same base
        ws_a.send(_req("TURN", {"delta": 1, "crossChapter": True,
                                "fromChapter": None},
                       sid=sid, rid="ok", epoch=1))
        st = _last_state(ws_a)
        assert st["revision"] == rev + 1 and st["spreadId"] == "c0:s1"


def test_duplicate_request_replays_ack_over_ws(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, _, _, rev = _pair(ws_a, ws_b)
        _open_and_ready(ws_a, ws_b, sid, rev)

        frame = _req("TURN", {"delta": 1, "crossChapter": True,
                              "fromChapter": None},
                     sid=sid, rid="dup", epoch=1)
        ws_b.send(frame)
        ack1 = _expect(ws_b, "ACK")
        st, _ = _drain_until(ws_b, lambda f: f.get("type") == "STATE"
                         and f.get("spreadId") == "c0:s1")
        rev = st["revision"]
        # drain the same commit broadcast on the leader so the resend diff is clean
        _drain_until(ws_a, lambda f: f.get("type") == "STATE"
                     and f.get("spreadId") == "c0:s1")

        # resend the identical frame: same ACK, then a PING boundary — no new
        # STATE/APPLIED may appear between them.
        ws_b.send(frame)
        ws_b.send(_req("PING", {"seq": 42}, rid="pk"))
        seen = []
        deadline = time.time() + 5
        while True:
            f = _recv(ws_b, timeout=max(0.1, deadline - time.time()))
            seen.append(f)
            if f["type"] == "PONG" and f.get("seq") == 42:
                break
            assert time.time() < deadline
        types = [f["type"] for f in seen]
        assert types == ["ACK", "PONG"]
        assert seen[0]["requestId"] == ack1["requestId"] == "dup"
        # nothing else arrived on the peer socket — the dedup replay is local
        try:
            extra = _recv(ws_a, timeout=0.5)
        except Exception:
            extra = None
        assert extra is None or extra.get("type") in ("PING", "PONG")


def test_disconnect_resume_full_over_ws(server):
    """Full detach->RESUME cycle with the token captured at JOIN time."""
    with _ws(server) as ws_a:
        _recv(ws_a)
        ws_a.send(_req("CREATE", rid="c"))
        data = _expect(ws_a, "ACK")["data"]
        sid = data["sessionId"]
        with _ws(server) as ws_b:
            _recv(ws_b)
            ws_b.send(_req("JOIN", {"code": data["code"]}, rid="j"))
            joined = _expect(ws_b, "ACK")["data"]
            _expect(ws_a, "JOIN_REQUEST")
            ws_a.send(_req("APPROVE", {"memberId": joined["memberId"]},
                           sid=sid, rid="a"))
            st = _expect(ws_a, "STATE"); _expect(ws_b, "STATE")
            _open_and_ready(ws_a, ws_b, sid, st["revision"])
            ws_a.send(_req("TURN", {"delta": 1, "crossChapter": True,
                                    "fromChapter": None},
                           sid=sid, rid="pre", epoch=1))
            st, _ = _drain_until(ws_a, lambda f: f.get("type") == "STATE"
                             and f.get("spreadId") == "c0:s1")
            committed_rev, committed_spread = st["revision"], st["spreadId"]

        # follower gone -> paused; leader op rejected, nothing queued
        st, _ = _drain_until(ws_a, lambda f: f.get("type") == "STATE"
                             and f.get("phase") == "paused")
        ws_a.send(_req("TURN", {"delta": 1, "crossChapter": True,
                                "fromChapter": None},
                       sid=sid, rid="gone", epoch=1))
        assert _expect(ws_a, "REJECT")["reason"] == "peer_offline"

        with _ws(server) as ws_c:
            _recv(ws_c)
            ws_c.send(_req("RESUME", {"memberId": joined["memberId"],
                                      "resumeToken": joined["resumeToken"]},
                           sid=sid, rid="re"))
            frames = []
            ack, frames = _drain_until(
                ws_c, lambda f: f.get("type") == "ACK" and f.get("requestId") == "re")
            # first STATE after resume: the disconnect window contributed nothing
            st = _expect(ws_c, "STATE")
            assert st["spreadId"] == committed_spread
            assert st["workEpoch"] == 1
            # manifest re-delivered so the resumed client can READY again
            man, more = _drain_until(
                ws_c, lambda f: f.get("type") == "MANIFEST")
            # collect the rest of the chunk set by count
            chunks = [man]
            while len(chunks) < man["count"]:
                f = _recv(ws_c)
                if f["type"] == "MANIFEST":
                    chunks.append(f)
            manifest = _join_manifest(chunks)
            assert manifest["folder"] == WORK
            # READY -> reading resumes
            ws_c.send(_req("READY", {"manifestVersion": 1}, sid=sid,
                           rid="rr", epoch=1))
            st, _ = _drain_until(ws_c, lambda f: f.get("type") == "STATE"
                                 and f.get("phase") == "reading")
            assert st["revision"] > committed_rev
            ws_c.send(_req("TURN", {"delta": 1, "crossChapter": True,
                                    "fromChapter": None},
                           sid=sid, rid="post", epoch=1))
            st, _ = _drain_until(ws_a, lambda f: f.get("type") == "STATE"
                                 and f.get("spreadId") == "c0:s2")
            assert st["phase"] == "reading"


def test_transport_switch_keeps_commit_consistency_over_ws(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, _, _, rev = _pair(ws_a, ws_b)
        _open_and_ready(ws_a, ws_b, sid, rev)
        ws_a.send(_req("TURN", {"delta": 1, "crossChapter": True,
                                "fromChapter": None},
                       sid=sid, rid="pre", epoch=1))
        st, _ = _drain_until(ws_a, lambda f: f.get("type") == "STATE"
                         and f.get("spreadId") == "c0:s1")
        committed = (st["revision"], st["spreadId"], st["workEpoch"])

        # both report the dc channel -> plane flips at a revision boundary
        ws_a.send(_req("TRANSPORT_STATE", {"state": "up"}, sid=sid, rid="u1", epoch=1))
        ws_b.send(_req("TRANSPORT_STATE", {"state": "up"}, sid=sid, rid="u2", epoch=1))
        ts, seen = _drain_until(
            ws_a, lambda f: f.get("type") == "TRANSPORT_STATE"
            and f.get("dataPlane") == "webrtc-dc")
        # the boundary STATE carries the latest commit — no dup, no loss
        st = [f for f in seen if f["type"] == "STATE"][-1]
        assert (st["spreadId"], st["workEpoch"]) == (committed[1], committed[2])
        assert ts["revision"] == st["revision"] > committed[0]

        # requests still sequence while on the dc plane
        ws_b.send(_req("TURN", {"delta": 1, "crossChapter": True,
                                "fromChapter": None},
                       sid=sid, rid="dc1", epoch=1))
        st, _ = _drain_until(ws_b, lambda f: f.get("type") == "STATE"
                         and f.get("spreadId") == "c0:s2")
        # flap back down
        ws_b.send(_req("TRANSPORT_STATE", {"state": "down"}, sid=sid, rid="d1", epoch=1))
        ts, seen = _drain_until(
            ws_b, lambda f: f.get("type") == "TRANSPORT_STATE"
            and f.get("dataPlane") == "server-ws")
        st = [f for f in seen if f["type"] == "STATE"][-1]
        assert st["spreadId"] == "c0:s2"
