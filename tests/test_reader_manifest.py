"""S04 — shared work manifest over the live server (#79, #485 Phase 2c port).

The leader OPEN_WORKs a work over a real reader-sync socket; both backends
(uvicorn + viewer-rs) resolve the manifest from the same enumeration the
solo viewer serves, chunk it into MANIFEST frames and commit a STATE. The
tests below assert the wire-visible contract only: manifest shape, digest
verification, chapter runs and typed load errors.

The in-process catalog unit matrix (flatten rules, kanji order, archives,
split/join internals) stays in test_reader_manifest_internals.py, which
retires with the Python backend. Wire constants are inlined from the
asyncapi contract instead of the Python package so this file runs in both
lanes.
"""
import hashlib
import json
import time
import urllib.parse
import urllib.request

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


def _get(server, path):
    req = urllib.request.Request(server + path)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _images(server, folder):
    status, body = _get(
        server, "/api/v1/images?folder=" + urllib.parse.quote(folder, safe=""))
    assert status == 200
    assert body["folder"] == folder
    return body["images"]


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


def _pair(ws_a, ws_b):
    """CREATE/JOIN/APPROVE over the live socket; returns (session_id, rev)."""
    ws_a.send(_req("CREATE", rid="c"))
    data = _expect(ws_a, "ACK")["data"]
    assert len(data["code"]) == 6 and data["code"].isdigit()
    ws_b.send(_req("JOIN", {"code": data["code"]}, rid="j"))
    joined = _expect(ws_b, "ACK")["data"]
    _expect(ws_a, "JOIN_REQUEST")
    ws_a.send(_req("APPROVE", {"memberId": joined["memberId"]},
                   sid=data["sessionId"], rid="a"))
    st = _expect(ws_a, "STATE")
    _expect(ws_b, "STATE")
    return data["sessionId"], st["revision"]


def _open_work(ws_a, ws_b, sid, rev, folder, page, rid="ow"):
    """Leader OPEN_WORK; returns (manifest, leader commit STATE)."""
    ws_a.send(_req("OPEN_WORK", {"folder": folder, "page": page},
                   sid=sid, rid=rid, epoch=0, base=rev))
    st, seen_a = _drain_until(
        ws_a, lambda f: f.get("type") == "STATE" and f.get("workEpoch") == 1)
    chunks = [f for f in seen_a if f["type"] == "MANIFEST"]
    assert chunks, "manifest must be chunked-delivered before/at commit"
    manifest = _join_manifest(chunks)
    # follower receives the same chunk payloads
    _, seen_b = _drain_until(
        ws_b, lambda f: f.get("type") == "STATE" and f.get("workEpoch") == 1)
    assert [f["data"] for f in seen_b if f["type"] == "MANIFEST"] == \
        [f["data"] for f in chunks]
    return manifest, st


# conftest tree: 1/2/3/10/11.png — offset0 spreads c0:s0=(1,2) c0:s1=(3,10)
WORK = "simple_manga"
FIRST_PAGE = {"folder": WORK, "name": "1.png"}


def test_manifest_shape_digest_and_initial(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, rev = _pair(ws_a, ws_b)
        manifest, st = _open_work(ws_a, ws_b, sid, rev, WORK, FIRST_PAGE)
        for key in ("chapters", "pageRefs", "spreadTable", "digest"):
            assert key in manifest
        assert manifest["chapters"] == [{
            "name": WORK, "folder": WORK,
            "pages": [{"folder": WORK, "name": n} for n in
                      ("1.png", "2.png", "3.png", "10.png", "11.png")],
        }]
        assert manifest["pageRefs"] == manifest["chapters"][0]["pages"]
        assert manifest["spreadTable"][0]["id"] == "c0:s0"
        assert manifest["spreadTable"][0]["right"] == FIRST_PAGE
        assert isinstance(manifest["digest"], str)
        assert manifest["digest"].startswith("sha256:")
        assert st["manifestVersion"] == 1
        assert manifest["initial"]["page"] == FIRST_PAGE


def test_manifest_pages_match_images_envelope(server):
    """Manifest pages are the same enumeration the solo viewer serves."""
    expected = _images(server, WORK)
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, rev = _pair(ws_a, ws_b)
        manifest, _ = _open_work(ws_a, ws_b, sid, rev, WORK, FIRST_PAGE)
        assert [p["name"] for p in manifest["pageRefs"]] == expected
        assert all(set(p.keys()) == {"folder", "name"} for p in manifest["pageRefs"])


def test_manifest_digest_stable_across_initial_pages(server):
    """Same work anchored on different pages → same digest (content-only)."""
    other = {"folder": WORK, "name": "3.png"}
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, rev = _pair(ws_a, ws_b)
        first, _ = _open_work(ws_a, ws_b, sid, rev, WORK, FIRST_PAGE)
    with _ws(server) as ws_c, _ws(server) as ws_d:
        _recv(ws_c); _recv(ws_d)
        sid2, rev2 = _pair(ws_c, ws_d)
        second, _ = _open_work(ws_c, ws_d, sid2, rev2, WORK, other, rid="ow2")
    assert first["digest"] == second["digest"]
    assert first["pageRefs"] == second["pageRefs"]
    assert first["initial"]["page"] != second["initial"]["page"]


def test_depth2_sibling_run_concatenates(server):
    """group/deep_series/vol1 sits at raw depth 2 — the leaf run concatenates."""
    folder = "group/deep_series/vol1"
    page = {"folder": folder, "name": "p1.png"}
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, rev = _pair(ws_a, ws_b)
        manifest, _ = _open_work(ws_a, ws_b, sid, rev, folder, page)
        assert [c["folder"] for c in manifest["chapters"]] == [
            "group/deep_series/vol1", "group/deep_series/vol2"]
        # chapters never share a spread — each starts a fresh pairing run
        first_ids = [s["id"] for s in manifest["spreadTable"]]
        assert first_ids[0] == "c0:s0"
        assert any(s.startswith("c1:") for s in first_ids[1:])


def test_depth1_leaf_stays_single_chapter(server):
    folder = "series/vol1"
    page = {"folder": folder, "name": "p1.png"}
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, rev = _pair(ws_a, ws_b)
        manifest, _ = _open_work(ws_a, ws_b, sid, rev, folder, page)
        assert [c["folder"] for c in manifest["chapters"]] == [folder]
        assert len(manifest["pageRefs"]) == 2


def test_unknown_folder_load_reports_error(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, rev = _pair(ws_a, ws_b)
        ws_a.send(_req("OPEN_WORK",
                       {"folder": "nosuchdir",
                        "page": {"folder": "nosuchdir", "name": "x.png"}},
                       sid=sid, rid="bad", epoch=0, base=rev))
        err = _expect(ws_a, "ERROR")
        assert err["code"] == "not_found"


def test_empty_work_load_reports_error(server):
    with _ws(server) as ws_a, _ws(server) as ws_b:
        _recv(ws_a); _recv(ws_b)
        sid, rev = _pair(ws_a, ws_b)
        ws_a.send(_req("OPEN_WORK",
                       {"folder": "empty_folder",
                        "page": {"folder": "empty_folder", "name": "x.png"}},
                       sid=sid, rid="empty", epoch=0, base=rev))
        err = _expect(ws_a, "ERROR")
        assert err["code"] == "empty_work"
