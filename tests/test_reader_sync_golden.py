"""#210 — golden-frame conformance over the live server (#485 Phase 2c port).

The canonical vectors live in the reader-sync cases file and are consumed
in-process by both harnesses (the Python runner and the viewer-rs golden
adapter), so a protocol change must move both implementations in lockstep.
This file keeps the shared vectors honest at the HTTP/WS boundary: the
cases file stays wellformed, and the key vectors — pairing handshake,
OPEN_WORK manifest delivery, LEAVE teardown and error paths — are replayed
against the live session server fixture, where dynamic values (codes,
member ids, digests) bind at runtime exactly as the in-process harnesses
assume.

The in-process vector runner itself stays in
test_reader_sync_golden_internals.py, which retires with the Python
backend. No Python package imports here so this file runs in both lanes
(uvicorn + BOOKQ_TEST_SERVER=rs).
"""
import hashlib
import json
import time
from pathlib import Path

from websockets.sync.client import connect as ws_connect

# Wire constant inlined from the asyncapi contract (RequestFrame.v const 1).
PROTOCOL_VERSION = 1

_WS_PATH = "/ws/reader-sync"

CASES_PATH = Path(__file__).resolve().parent.parent / "api" / "reader-sync-cases.json"

_KNOWN_OPS = {"link", "detach", "send", "expect", "drain", "tick"}

# conftest tree work used for the live OPEN_WORK replay below.
WORK = "simple_manga"
FIRST_PAGE = {"folder": WORK, "name": "1.png"}


def _doc():
    return json.loads(CASES_PATH.read_text())


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


def test_cases_file_is_wellformed():
    doc = _doc()
    assert doc["format"] == 1
    cases = {c["id"]: c for c in doc["cases"]}
    assert len(cases) >= 1, "no cases defined"
    for cid, case in cases.items():
        assert case["steps"], f"{cid}: empty"
        for i, step in enumerate(case["steps"]):
            assert step.get("op") in _KNOWN_OPS, f"{cid} step[{i}]: unknown op"
            if step["op"] == "send":
                frame = step["frame"]
                assert frame["v"] == PROTOCOL_VERSION
                # error_paths deliberately sends a typeless frame to pin
                # the malformed REJECT — type is required only when present.
                assert isinstance(frame.get("requestId"), str)
                if "type" in frame:
                    assert isinstance(frame["type"], str) and frame["type"]
    resolver = doc.get("resolver") or {}
    for name, spec in resolver.items():
        assert isinstance(spec["folder"], str) and spec["folder"]
        assert isinstance(spec["chapters"], list) and spec["chapters"]
        for ch in spec["chapters"]:
            for n in ch["pages"]:
                assert isinstance(n, str) and n


def test_golden_pairing_vector_over_live_ws(server):
    """pairing_create_join_approve: the live handshake emits the same frame
    type sequence the golden vector pins (HELLO/ACK/STATE/JOIN_REQUEST)."""
    with _ws(server) as ws_a, _ws(server) as ws_b:
        assert _recv(ws_a)["type"] == "HELLO"
        assert _recv(ws_b)["type"] == "HELLO"
        ws_a.send(_req("CREATE", rid="c1"))
        ack = _expect(ws_a, "ACK")
        assert ack["request"] == "CREATE"
        data = ack["data"]
        assert len(data["code"]) == 6 and data["code"].isdigit()
        assert data["role"] == "leader"
        assert data["resumeToken"]
        _expect(ws_a, "STATE")
        ws_b.send(_req("JOIN", {"code": data["code"]}, rid="j1"))
        joined = _expect(ws_b, "ACK")
        assert joined["request"] == "JOIN"
        jr = _expect(ws_a, "JOIN_REQUEST")
        assert jr["memberId"] == joined["data"]["memberId"]
        ws_a.send(_req("APPROVE", {"memberId": joined["data"]["memberId"]},
                       sid=data["sessionId"], rid="a1"))
        _expect(ws_a, "ACK")
        st_a = _expect(ws_a, "STATE")
        st_b = _expect(ws_b, "STATE")
        assert len(st_a["presence"]) == 2 == len(st_b["presence"])


def _join_manifest(chunks) -> dict:
    count = None
    digest = None
    parts: dict[int, str] = {}
    for ch in chunks:
        if count is None:
            count, digest = ch["count"], ch["digest"]
        parts[ch["index"]] = ch["data"]
    assert count == len(chunks) and sorted(parts) == list(range(count))
    manifest = json.loads("".join(parts[i] for i in range(count)))
    assert manifest.get("digest") == digest
    content = {k: manifest[k] for k in
               ("v", "folder", "offset", "chapters", "pageRefs", "spreadTable")}
    canonical = json.dumps(content, ensure_ascii=True, sort_keys=True,
                           separators=(",", ":"))
    assert "sha256:" + hashlib.sha256(canonical.encode("ascii")).hexdigest() == digest
    return manifest


def test_golden_open_work_vector_over_live_ws(server):
    """open_work_manifest_ready_reading: OPEN_WORK delivers chunked MANIFEST
    frames whose digest verifies, then READY x2 reaches reading."""
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        ws_a.send(_req("CREATE", rid="c1"))
        data = _expect(ws_a, "ACK")["data"]
        ws_b.send(_req("JOIN", {"code": data["code"]}, rid="j1"))
        joined = _expect(ws_b, "ACK")["data"]
        _expect(ws_a, "JOIN_REQUEST")
        ws_a.send(_req("APPROVE", {"memberId": joined["memberId"]},
                       sid=data["sessionId"], rid="a1"))
        rev = _expect(ws_a, "STATE")["revision"]
        _expect(ws_b, "STATE")
        sid = data["sessionId"]
        ws_a.send(_req("OPEN_WORK", {"folder": WORK, "page": FIRST_PAGE},
                       sid=sid, rid="o1", epoch=0, base=rev))
        st, seen = _drain_until(
            ws_a, lambda f: f.get("type") == "STATE" and f.get("workEpoch") == 1)
        manifest = _join_manifest([f for f in seen if f["type"] == "MANIFEST"])
        assert manifest["folder"] == WORK
        assert manifest["digest"].startswith("sha256:")
        ws_a.send(_req("READY", {"manifestVersion": 1}, sid=sid, rid="r1", epoch=1))
        ws_b.send(_req("READY", {"manifestVersion": 1}, sid=sid, rid="r2", epoch=1))
        sa, _ = _drain_until(ws_a, lambda f: f.get("type") == "STATE"
                             and f.get("phase") == "reading")
        assert sa["spreadId"] == "c0:s0"


def test_golden_leave_vector_over_live_ws(server):
    """leave_ends_session_for_all: LEAVE broadcasts END to every member."""
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        ws_a.send(_req("CREATE", rid="c1"))
        data = _expect(ws_a, "ACK")["data"]
        _expect(ws_a, "STATE")
        ws_b.send(_req("JOIN", {"code": data["code"]}, rid="j1"))
        joined = _expect(ws_b, "ACK")["data"]
        _expect(ws_a, "JOIN_REQUEST")
        ws_a.send(_req("APPROVE", {"memberId": joined["memberId"]},
                       sid=data["sessionId"], rid="a1"))
        _expect(ws_a, "ACK")
        _expect(ws_a, "STATE"); _expect(ws_b, "STATE")
        ws_b.send(_req("LEAVE", sid=data["sessionId"], rid="l1"))
        assert _expect(ws_a, "END")["reason"] == "left"
        assert _expect(ws_b, "END")["reason"] == "left"


def test_golden_error_vector_over_live_ws(server):
    """error_paths: bad code and unpaired SIGNAL are typed REJECTs."""
    with _ws(server) as ws:
        _recv(ws)
        ws.send(_req("JOIN", {"code": "000000"}, rid="j1"))
        assert _expect(ws, "REJECT")["reason"] == "bad_code"
        ws.send(_req("CREATE", rid="c1"))
        created = _expect(ws, "ACK")["data"]
        _expect(ws, "STATE")
        ws.send(_req("SIGNAL", {"kind": "offer", "data": {}},
                     sid=created["sessionId"], rid="s1"))
        assert _expect(ws, "REJECT")["reason"] == "not_paired"
