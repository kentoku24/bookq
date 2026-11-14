"""#210 — golden-frame conformance: drive the in-process session core through
api/reader-sync-cases.json and assert the exact wire frames each link emits.

The same case file is executed by the viewer-rs adapter
(rust/viewer-rs/src/reader_sync/golden.rs), so a protocol change must move
both implementations in lockstep — drift fails one side's harness.

Dynamic server values (session/member/link ids, codes, resume tokens,
digests) are bound at runtime: "@L1.member" resolves to the memberId issued
to link L1's CREATE/JOIN ACK, "@session" to the latest session id, and so on.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.reader_sync import catalog, model, protocol, sessions  # noqa: E402
from app.reader_sync import state as rstate  # noqa: E402

CASES_PATH = REPO_ROOT / "api" / "reader-sync-cases.json"


# ── resolver manifest construction ───────────────────────────────────


def build_manifest(spec: dict) -> dict:
    """The manifest dict a real catalog would resolve — built through the
    same model/digest code the production resolver uses."""
    chapters = [{
        "name": c["name"], "folder": c["folder"],
        "pages": [{"folder": c["folder"], "name": n} for n in c["pages"]],
    } for c in spec["chapters"]]
    page_refs = [p for c in chapters for p in c["pages"]]
    plan = model.build_spread_plan(
        [{"folder": c["folder"], "pages": c["pages"]} for c in chapters],
        spec["offset"])
    core = {
        "v": protocol.PROTOCOL_VERSION,
        "folder": spec["folder"],
        "offset": spec["offset"],
        "chapters": chapters,
        "pageRefs": page_refs,
        "spreadTable": [model.serialize_spread(s) for s in plan],
    }
    core["digest"] = catalog.manifest_digest(core)
    return core


def canonical_text(manifest: dict) -> str:
    return json.dumps(manifest, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"))


# ── bindings (@-template resolution + harvesting) ────────────────────


def _resolve(obj, bindings):
    """Substitute '@name' references in values and dict keys."""
    if isinstance(obj, str) and obj.startswith("@"):
        if obj[1:] not in bindings:
            raise KeyError(f"unbound reference {obj}")
        return bindings[obj[1:]]
    if isinstance(obj, list):
        return [_resolve(x, bindings) for x in obj]
    if isinstance(obj, dict):
        return {bindings[k[1:]] if isinstance(k, str) and k.startswith("@")
                and k[1:] in bindings else k:
                _resolve(v, bindings) for k, v in obj.items()}
    return obj


class LinkDriver:
    """Owns a PeerLink: its outbound queue, bulk expansion and the
    transport-close hook (recorded, never a socket)."""

    def __init__(self, reg: sessions.SessionRegistry, user: dict | None):
        self.link = sessions.PeerLink(reg)
        self.link.user = user
        self.closed: list[tuple[int, str]] = []
        self.link.request_transport_close = \
            lambda code, reason: self.closed.append((code, reason))
        reg.register_link(self.link)

    def drain(self) -> list[dict]:
        """Every pending outbound frame, bulk blocks expanded in place."""
        out = []
        while True:
            try:
                item = self.link.outq.get_nowait()
            except asyncio.QueueEmpty:
                return out
            if item is sessions._BULK:
                while (raw := self.link._pop_bulk()) is not None:
                    out.append(json.loads(raw))
            else:
                out.append(json.loads(item))


def _harvest(bindings: dict, linkname: str, frame: dict,
             request_senders: dict) -> None:
    """Bind dynamic values an implementation issues into wire frames."""
    t = frame.get("type")
    if t == "HELLO" and isinstance(frame.get("link"), str):
        bindings[f"{linkname}.link"] = frame["link"]
    sid = frame.get("sessionId")
    if isinstance(sid, str):
        bindings["session"] = sid
    if t == "ACK":
        sender = request_senders.get(frame.get("requestId"))
        data = frame.get("data") or {}
        if sender is not None:
            if isinstance(data.get("memberId"), str):
                bindings[f"{sender}.member"] = data["memberId"]
            if isinstance(data.get("resumeToken"), str):
                bindings[f"{sender}.token"] = data["resumeToken"]
        if isinstance(data.get("code"), str):
            bindings["code"] = data["code"]
    if t == "STATE":
        for mid, m in (frame.get("presence") or {}).items():
            if isinstance(m, dict) and isinstance(m.get("role"), str):
                bindings[f"{m['role']}.member"] = mid
    if t == "MANIFEST" and isinstance(frame.get("digest"), str):
        bindings["digest"] = frame["digest"]


# ── case runner ──────────────────────────────────────────────────────


def run_case(doc: dict, case: dict) -> None:
    cid = case["id"]
    clock = [0.0]
    resolver_map = doc.get("resolver") or {}
    rkey = case.get("resolver")

    def fake_resolver(link, job):
        spec = resolver_map.get(job["folder"])
        if spec is None:
            raise catalog.ManifestError(catalog.ERR_NOT_FOUND,
                                        f"no resolver entry: {job['folder']}")
        manifest = build_manifest(spec)
        # Mirror catalog.resolve_work: the committed initial spread comes
        # from the requested page ref, resolved through the built plan.
        plan = rstate.plan_from_manifest(manifest)
        idx = model.spread_index_for_page(plan, job["page"])
        manifest["initial"] = {"page": job["page"],
                               "spreadId": plan[idx].id}
        # MANIFEST chunk data is the canonical bytes of the *delivered*
        # manifest (including the resolver-added "initial") — rebind so
        # @manifest.canonical always mirrors the latest resolve.
        bindings["manifest.canonical"] = canonical_text(manifest)
        return manifest

    reg = sessions.SessionRegistry(clock=lambda: clock[0],
                                   work_resolver=fake_resolver if rkey else None)
    if rkey is not None:
        spec = resolver_map[rkey]
        bindings_manifest = build_manifest(spec)
    else:
        bindings_manifest = None

    bindings: dict = {}
    if bindings_manifest is not None:
        bindings["manifest.canonical"] = canonical_text(bindings_manifest)
    links: dict[str, LinkDriver] = {}
    request_senders: dict = {}   # requestId -> link name

    def _send(name, frame):
        f = _resolve(frame, bindings)
        request_senders[f.get("requestId")] = name
        reg.handle_raw(links[name].link,
                       json.dumps(f, separators=(",", ":")))

    def _drain(name):
        for fr in links[name].drain():
            _harvest(bindings, name, fr, request_senders)

    # Canonical shared setup — the same handshake every case needs, with
    # its wire output drained+harvested but not asserted (cases assert
    # their own steps only).
    setup = case.get("setup")
    if setup:
        users = case.get("users") or {}
        links["L1"] = LinkDriver(reg, users.get("L1",
            {"email": "alice@example.com", "name": "Alice", "picture": ""}))
        _drain("L1")
        _send("L1", {"v": 1, "requestId": "c1", "sessionId": None,
                     "workEpoch": 0, "baseRevision": 0, "type": "CREATE",
                     "payload": {}})
        _drain("L1")
        links["L2"] = LinkDriver(reg, users.get("L2",
            {"email": "bob@example.com", "name": "Bob", "picture": ""}))
        _drain("L2")
        _send("L2", {"v": 1, "requestId": "j1", "sessionId": None,
                     "workEpoch": 0, "baseRevision": 0, "type": "JOIN",
                     "payload": {"code": "@code"}})
        _drain("L1"); _drain("L2")
        _send("L1", {"v": 1, "requestId": "a1", "sessionId": "@session",
                     "workEpoch": 0, "baseRevision": 1, "type": "APPROVE",
                     "payload": {"memberId": "@L2.member"}})
        _drain("L1"); _drain("L2")
        if setup in ("paired_open", "reading"):
            _send("L1", {"v": 1, "requestId": "o1", "sessionId": "@session",
                         "workEpoch": 0, "baseRevision": 2,
                         "type": "OPEN_WORK",
                         "payload": {"folder": rkey,
                                     "page": {"folder": rkey,
                                              "name": resolver_map[rkey]["chapters"][0]["pages"][0]},
                                     "offset": resolver_map[rkey]["offset"]}})
            _drain("L1"); _drain("L2")
        if setup == "reading":
            _send("L1", {"v": 1, "requestId": "r1", "sessionId": "@session",
                         "workEpoch": 1, "baseRevision": 4, "type": "READY",
                         "payload": {"manifestVersion": 1}})
            _drain("L1"); _drain("L2")
            _send("L2", {"v": 1, "requestId": "r2", "sessionId": "@session",
                         "workEpoch": 1, "baseRevision": 5, "type": "READY",
                         "payload": {"manifestVersion": 1}})
            _drain("L1"); _drain("L2")

    def need_link(name):
        if name not in links:
            raise AssertionError(f"{cid}: unknown link {name}")
        return links[name]

    for i, step in enumerate(case["steps"]):
        op = step.get("op")
        at = f"{cid} step[{i}] {op}"
        if op == "link":
            links[step["as"]] = LinkDriver(reg, step.get("user"))
        elif op == "detach":
            reg.detach_link(need_link(step["link"]).link)
        elif op == "send":
            _send(step["link"], step["frame"])
        elif op in ("expect", "drain"):
            got = need_link(step["link"]).drain()
            for f in got:
                _harvest(bindings, step["link"], f, request_senders)
            if op == "drain":
                continue
            want = [_resolve(f, bindings) for f in step["frames"]]
            assert got == want, (
                f"{at}: frame mismatch\n"
                f"--- want ---\n{json.dumps(want, indent=1)}\n"
                f"--- got ----\n{json.dumps(got, indent=1)}")
        elif op == "tick":
            clock[0] += step.get("by", 0)
            reg.tick()
        else:
            raise AssertionError(f"{at}: unknown op")


# ── pytest entry ─────────────────────────────────────────────────────

_DOC = json.loads(CASES_PATH.read_text())
_CASES = {c["id"]: c for c in _DOC["cases"]}


@pytest.mark.parametrize("case_id", sorted(_CASES))
def test_golden_case(case_id):
    run_case(_DOC, _CASES[case_id])


def test_cases_file_is_wellformed():
    assert _DOC["format"] == 1
    assert _CASES, "no cases defined"
    for cid, c in _CASES.items():
        assert c["steps"], f"{cid}: empty"
