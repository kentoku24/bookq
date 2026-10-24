#!/usr/bin/env python3
"""reader-sync sequencer probe (#80/S05): two real WS clients against a
live server — pairing, OPEN_WORK manifest delivery, READY gating, ordered
page ops, duplicate/stale rejection, and a ws->dc plane flip.

Proves the authoritative-state path end-to-end through a given base URL
(tailscale serve https URL, LAN http URL, or local test server). Prints a
machine-readable transcript of event names/revisions/reasons ONLY — never
pairing codes, resume tokens, folder names, or frame payloads.

Usage:
    uv run --with-requirements requirements-dev.txt --with websockets \
        python scripts/reader-sync/seq_probe.py <base-url> [work-folder]

    <base-url>   http(s):// or ws(s)://, no path
    work-folder  optional; when omitted the probe picks the first folder
                 with a non-empty page list from /api/images enumeration
                 (test/dev trees only).

Exit 0 when every asserted behaviour holds; 1 with the failure printed.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request

from websockets.sync.client import connect

PATH = "/ws/reader-sync"
TIMEOUT = 15.0


def _recv(ws, timeout: float = TIMEOUT):
    raw = ws.recv(timeout=timeout)
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    return json.loads(raw)


def _req(rtype, payload=None, *, sid=None, rid="p", epoch=0, base=0):
    return json.dumps({
        "v": 1, "requestId": rid, "sessionId": sid, "workEpoch": epoch,
        "baseRevision": base, "type": rtype, "payload": payload or {},
    })


def _pong(ws, f: dict) -> None:
    ws.send(json.dumps({"v": 1, "type": "PONG", "seq": f.get("seq")}))


def _until(ws, pred, label, timeout: float = TIMEOUT):
    """Read frames until pred matches; return (frame, seen). Server
    heartbeat PINGs are answered inline so long resolves never trip the
    no-response timeout."""
    deadline = time.time() + timeout
    seen = []
    while time.time() < deadline:
        f = _recv(ws, timeout=max(0.1, deadline - time.time()))
        if f.get("type") == "PING":
            _pong(ws, f)
            continue
        seen.append(f)
        if pred(f):
            return f, seen
    raise AssertionError(f"{label}: no match; saw "
                         f"{[f.get('type') for f in seen]}")


def _expect(ws, rtype, label):
    return _until(ws, lambda f: f.get("type") == rtype, label)[0]


def _pick_work(base: str):
    """First (folder, page_name) with a direct image child in the tree.

    DFS over one /api/folders fetch (the first hit on a cold library scan
    can take tens of seconds — the timeout is generous). A node's server
    path is ``parent_path + "/" + name``; flattened names already carry
    intermediate segments. Archive/pdf folders list their inner pages as
    image children, so this works uniformly. Folder names are never printed
    (live-library leak prevention).
    """
    with urllib.request.urlopen(base + "/api/folders", timeout=120) as r:
        tree = json.load(r)
    nodes = tree if isinstance(tree, list) else tree.get("folders", [])
    stack = [("", n) for n in nodes]
    while stack:
        prefix, node = stack.pop(0)
        if node.get("type") != "folder":
            continue
        path = f"{prefix}/{node['name']}".lstrip("/")
        kids = node.get("children") or []
        for c in kids:
            if c.get("type") == "image":
                return path, c["name"]
        stack.extend((path, c) for c in kids)
    return None


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print("usage: seq_probe.py <base-url> [work-folder]", file=sys.stderr)
        return 2
    base = sys.argv[1].rstrip("/")
    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
    url = ws_base + PATH
    t0 = time.time()

    def log(event: str, **kv):
        safe = {k: v for k, v in kv.items()
                if k not in ("code", "token", "folder", "data")}
        print(f"{time.time() - t0:7.3f}s  {event:<14} "
              f"{json.dumps(safe, ensure_ascii=False)}")

    try:
        if len(sys.argv) == 3:
            work, first_page = sys.argv[2], None
            log("work-arg", present=True)
        else:
            picked = _pick_work(base)
            log("work-pick", ok=picked is not None)
            if picked is None:
                print("no readable folder with images on this server",
                      file=sys.stderr)
                return 1
            work, first_page = picked
        if first_page is None:
            with urllib.request.urlopen(
                    base + "/api/images?folder=" + urllib.parse.quote(work),
                    timeout=30) as r:
                first_page = json.load(r)[0]

        log("connect", url=url)
        with connect(url) as a, connect(url) as b:
            assert _recv(a)["type"] == "HELLO" and _recv(b)["type"] == "HELLO"
            log("hello", ok=True)

            # ── pair ────────────────────────────────────────────────
            a.send(_req("CREATE", rid="c"))
            created = _expect(a, "ACK", "create-ack")["data"]
            _expect(a, "STATE", "create-state")
            b.send(_req("JOIN", {"code": created["code"]}, rid="j"))
            joined = _expect(b, "ACK", "join-ack")["data"]
            _expect(a, "JOIN_REQUEST", "join-request")
            a.send(_req("APPROVE", {"memberId": joined["memberId"]},
                        sid=created["sessionId"], rid="a1"))
            st = _expect(a, "STATE", "approve-state")
            _expect(b, "STATE", "approve-state-b")
            sid = created["sessionId"]
            rev = st["revision"]
            log("paired", rev=rev, phase=st["phase"])

            # ── OPEN_WORK -> manifest -> READY gate ─────────────────
            a.send(_req("OPEN_WORK",
                        {"folder": work,
                         "page": {"folder": work, "name": first_page}},
                        sid=sid, rid="ow", epoch=0, base=rev))
            st, seen = _until(
                a, lambda f: f.get("type") == "STATE"
                and f.get("workEpoch") == 1, "commit-state", timeout=60)
            chunks = [f for f in seen if f.get("type") == "MANIFEST"]
            assert chunks, "manifest chunks must precede/with the commit STATE"
            assert st["spreadId"] is not None, "manifest commit has no spread"
            log("open-work", rev=st["revision"], phase=st["phase"],
                chunks=len(chunks), epoch=st["workEpoch"])

            a.send(_req("TURN", {"delta": 1, "crossChapter": True,
                                 "fromChapter": None},
                        sid=sid, rid="early", epoch=1))
            rej = _expect(a, "REJECT", "early-turn")
            assert rej["reason"] == "not_ready", rej
            assert rej["snapshot"]["revision"] == st["revision"]
            log("gate", reason=rej["reason"])

            for w, rid in ((a, "ra"), (b, "rb")):
                w.send(_req("READY", {"manifestVersion": 1}, sid=sid,
                            rid=rid, epoch=1))
            sa, _ = _until(a, lambda f: f.get("type") == "STATE"
                           and f.get("phase") == "reading", "ready-a")
            _until(b, lambda f: f.get("type") == "STATE"
                   and f.get("phase") == "reading", "ready-b")
            rev = sa["revision"]
            log("reading", rev=rev, spread=sa["spreadId"])

            # ── ordered ops: NEXT x2 (leader + follower) -> +2 ──────
            # cross-socket arrival order is genuinely receive-order, so wait
            # for BOTH commit STATEs rather than a specific requestId order.
            a.send(_req("TURN", {"delta": 1, "crossChapter": True,
                                 "fromChapter": None},
                        sid=sid, rid="n1", epoch=1))
            b.send(_req("TURN", {"delta": 1, "crossChapter": True,
                                 "fromChapter": None},
                        sid=sid, rid="n2", epoch=1))

            def _two_states(f, acc):
                if f.get("type") == "STATE":
                    acc.append(f["revision"])
                return len(acc) >= 2

            acc_a: list = []
            _, seen_a = _until(a, lambda f: _two_states(f, acc_a), "ops-a")
            acc_b: list = []
            _, seen_b = _until(b, lambda f: _two_states(f, acc_b), "ops-b")
            applied_a = {f["requestId"] for f in seen_a if f["type"] == "APPLIED"}
            applied_b = {f["requestId"] for f in seen_b if f["type"] == "APPLIED"}
            assert applied_a >= {"n1", "n2"} and applied_b >= {"n1", "n2"}
            revs_a, revs_b = acc_a, acc_b
            assert revs_a == revs_b, (revs_a, revs_b)
            assert revs_a == sorted(revs_a) and len(set(revs_a)) == len(revs_a)
            rev = revs_a[-1]
            log("ordered-ops", revs=revs_a)

            # ── duplicate requestId -> replay, no new state ─────────
            dup = _req("TURN", {"delta": -1, "crossChapter": True,
                                "fromChapter": None},
                       sid=sid, rid="dup", epoch=1)
            a.send(dup)
            st, _ = _until(a, lambda f: f.get("type") == "STATE",
                           "dup-commit")
            rev = st["revision"]
            a.send(dup)
            a.send(_req("PING", {"seq": 7}, rid="pk"))
            seq = []
            deadline = time.time() + TIMEOUT
            while time.time() < deadline:
                f = _recv(a)
                if f.get("type") == "PING":          # heartbeat -> PONG inline
                    _pong(a, f)
                    continue
                seq.append(f["type"])
                if f["type"] == "PONG" and f.get("seq") == 7:
                    break
            assert seq == ["ACK", "PONG"], seq
            log("dedup", replayed=True)

            # ── stale epoch -> reject + latest snapshot ─────────────
            a.send(_req("TURN", {"delta": 1, "crossChapter": True,
                                 "fromChapter": None},
                        sid=sid, rid="se", epoch=0))
            rej = _expect(a, "REJECT", "stale-epoch")
            assert rej["reason"] == "stale_epoch", rej
            assert rej["snapshot"]["revision"] == rev
            log("stale", reason=rej["reason"], snap_rev=rej["snapshot"]["revision"])

            # ── follower OPEN_WORK -> not_leader ────────────────────
            b.send(_req("OPEN_WORK",
                        {"folder": work,
                         "page": {"folder": work, "name": first_page}},
                        sid=sid, rid="fo", epoch=1, base=rev))
            rej = _expect(b, "REJECT", "follower-open")
            assert rej["reason"] == "not_leader", rej
            log("leader-only", reason=rej["reason"])

            # ── transport flip at a revision boundary ───────────────
            a.send(_req("TRANSPORT_STATE", {"state": "up"}, sid=sid,
                        rid="u1", epoch=1))
            b.send(_req("TRANSPORT_STATE", {"state": "up"}, sid=sid,
                        rid="u2", epoch=1))
            ts, seen = _until(
                a, lambda f: f.get("type") == "TRANSPORT_STATE"
                and f.get("dataPlane") == "webrtc-dc", "plane-flip")
            boundary = [f for f in seen if f["type"] == "STATE"][-1]
            assert ts["revision"] == boundary["revision"] > rev
            log("plane-flip", rev=ts["revision"], plane=ts["dataPlane"])

            # sequencing still converges on the dc plane
            b.send(_req("TURN", {"delta": 1, "crossChapter": True,
                                 "fromChapter": None},
                        sid=sid, rid="dc1", epoch=1))
            sa, _ = _until(b, lambda f: f.get("type") == "STATE"
                           and f.get("revision", 0) > ts["revision"], "dc-op")
            log("dc-seq", rev=sa["revision"], spread=sa["spreadId"],
                plane=sa["dataPlane"])

            log("done", ok=True)
        return 0
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
