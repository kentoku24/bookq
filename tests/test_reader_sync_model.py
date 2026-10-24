"""S01 — dual-reader spread model & wire contract tests (#76).

Deterministic and transport-free: no WS, no image fetch, no DOM rendering.
JS-side checks evaluate the real production pairModel.js in a browser via
add_script_tag — no mirrored re-implementation.
"""
import copy
import json
import os
from pathlib import Path

import pytest

from app.reader_sync import model, protocol

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = json.loads(
    (REPO_ROOT / "tests" / "fixtures" / "reader_sync_cases.json").read_text(encoding="utf-8")
)
PAIR_MODEL_JS = REPO_ROOT / "app" / "static" / "viewer" / "pairModel.js"


def _chapters(case):
    return [{"folder": ch["folder"], "pages": list(ch["pages"])} for ch in case["chapters"]]


def _ser_compact(spreads):
    """name-string form matching the fixture: name when the ref lives in the
    chapter folder, else {folder, name}."""
    def ref(r):
        if r is None:
            return None
        return r.name if r.folder == s.chapter_folder else {"folder": r.folder, "name": r.name}
    out = []
    for s in spreads:
        out.append({"id": s.id, "chapterFolder": s.chapter_folder,
                    "right": ref(s.right), "left": ref(s.left)})
    return out


def _ser_full(spreads):
    return [model.serialize_spread(s) for s in spreads]


def test_spread_cases():
    for case in FIXTURE["spreadCases"]:
        plan = model.build_spread_plan(_chapters(case), case["offset"])
        assert _ser_compact(plan) == case["expectSpreads"], case["name"]


def _run_op_py(plan, op):
    if op["op"] == "turn":
        return model.turn_index(plan, op["from"], op["delta"], op["crossChapter"])
    if op["op"] == "goto":
        return model.goto_index(plan, op["page"])
    if op["op"] == "project":
        s = plan[op["spreadIndex"]]
        ref = model.assigned_ref(s, op["role"], op["leaderSide"])
        side = op["leaderSide"] if op["role"] == "leader" else ("left" if op["leaderSide"] == "right" else "right")
        return {"side": side, "name": None if ref is None else ref.name}
    raise AssertionError(f"unknown op: {op}")


def _check_op_result(op, got):
    if op["op"] == "project":
        assert got["side"] == op["expectSide"] and got["name"] == op["expectName"], op
    else:
        assert got == op["expect"], op


def test_op_cases_python():
    for case in FIXTURE["opCases"]:
        plan = model.build_spread_plan(_chapters(case), case["offset"])
        for op in case["ops"]:
            if "expectError" in op:
                with pytest.raises(model.ModelError) as e:
                    _run_op_py(plan, op)
                assert e.value.code == op["expectError"], (case["name"], op)
            else:
                _check_op_result(op, _run_op_py(plan, op))


def test_invalid_model_inputs():
    with pytest.raises(model.ModelError) as e:
        model.build_spread_plan([{"folder": "A", "pages": ["1"]}], 2)
    assert e.value.code == "ERR_RANGE"
    with pytest.raises(model.ModelError) as e:
        model.build_spread_plan([{"folder": "A"}], 0)
    assert e.value.code == "ERR_MALFORMED"
    with pytest.raises(model.ModelError) as e:
        model.build_spread_plan([{"folder": "A", "pages": [{"name": "x"}]}], 0)
    assert e.value.code == "ERR_MALFORMED"
    with pytest.raises(model.ModelError) as e:
        model.assigned_ref(
            model.Spread("c0:s0", "A", model.PageRef("A", "1"), None), "leader", "up")
    assert e.value.code == "ERR_RANGE"


def test_inputs_not_mutated():
    case = next(c for c in FIXTURE["spreadCases"] if c["name"] == "複数章は見開きを跨がない")
    chapters = _chapters(case)
    before = copy.deepcopy(chapters)
    plan = model.build_spread_plan(chapters, case["offset"])
    assert chapters == before
    snap = copy.deepcopy(plan)
    model.turn_index(plan, 0, 1, True)
    model.goto_index(plan, {"folder": "B", "name": "x"})
    model.assigned_ref(plan[0], "leader", "right")
    assert plan == snap


def test_protocol_reject_cases():
    for case in FIXTURE["rejectCases"]:
        if "state" in case:
            fn, raw = protocol.validate_state, case["state"]
        else:
            fn, raw = protocol.parse_request, case["frame"]
        if raw == "__RAW_OVER_CAP__":
            raw = "x" * (protocol.MAX_FRAME_BYTES + 1)
        with pytest.raises(protocol.ProtocolError) as e:
            fn(raw)
        assert e.value.code == case["expectError"], case["name"]


def test_protocol_accept_cases():
    for case in FIXTURE["acceptFrames"]:
        if "state" in case:
            assert protocol.validate_state(case["state"]) == case["state"], case["name"]
        else:
            got = protocol.parse_request(case["frame"])
            assert got == case["frame"], case["name"]
            # roundtrip through the encoder
            assert protocol.parse_request(protocol.encode_frame(case["frame"])) == case["frame"]


def test_protocol_rejects_raw_strings():
    with pytest.raises(protocol.ProtocolError) as e:
        protocol.parse_request("not json")
    assert e.value.code == protocol.ERR_MALFORMED
    with pytest.raises(protocol.ProtocolError) as e:
        protocol.parse_request(b"\xff\xfe{")
    assert e.value.code == protocol.ERR_MALFORMED
    ok = protocol.encode_frame({"v": 1, "requestId": "r", "sessionId": "s",
                                "workEpoch": 0, "baseRevision": 0, "type": "PING", "payload": {}})
    assert protocol.parse_request(ok)["type"] == "PING"


@pytest.fixture
def any_browser(request):
    if os.environ.get("BROWSER") == "webkit":
        return request.getfixturevalue("webkit_browser")
    return request.getfixturevalue("browser")


_JS_RUN_OPS = """([spreads, ops]) => ops.map(op => {
  try {
    if (op.op === 'turn') return {ok: PairModel.turnIndex(spreads, op.from, op.delta, op.crossChapter)};
    if (op.op === 'goto') {
      const i = PairModel.spreadIndexForPage(spreads, op.page);
      if (i < 0) { const e = new Error('range'); e.code = 'ERR_RANGE'; throw e; }
      return {ok: i};
    }
    if (op.op === 'project') {
      const r = PairModel.projectSpread(spreads[op.spreadIndex], op.role, op.leaderSide);
      return {ok: {side: r.side, name: r.ref ? r.ref.name : null}};
    }
    return {ok: null};
  } catch (e) { return {err: (e && e.code) || 'ERR_UNKNOWN'}; }
})"""


def test_pair_model_js_in_browser(any_browser):
    """Evaluate the real production pairModel.js — never a copied re-impl."""
    assert PAIR_MODEL_JS.exists()
    assert PAIR_MODEL_JS.parent.name == "viewer" and PAIR_MODEL_JS.parent.parent.name == "static"
    context = any_browser.new_context()
    pg = context.new_page()
    try:
        pg.goto("about:blank")
        pg.add_script_tag(path=str(PAIR_MODEL_JS))
        assert pg.evaluate("() => window.PairModel && window.PairModel.PROTOCOL_VERSION") == protocol.PROTOCOL_VERSION
        for case in FIXTURE["opCases"]:
            plan = model.build_spread_plan(_chapters(case), case["offset"])
            results = pg.evaluate(_JS_RUN_OPS, [_ser_full(plan), case["ops"]])
            assert len(results) == len(case["ops"])
            for op, res in zip(case["ops"], results):
                if "expectError" in op:
                    assert res.get("err") == op["expectError"], (case["name"], op, res)
                else:
                    assert "ok" in res, (case["name"], op, res)
                    _check_op_result(op, res["ok"])
        # JS-side rejects: invalid inputs must throw typed errors, not fall back
        rejects = pg.evaluate(
            """() => [
              (() => { try { PairModel.turnIndex(null, 0, 1, true); return 'no-throw'; } catch (e) { return e.code; } })(),
              (() => { try { PairModel.turnIndex([{id:'c0:s0',chapterFolder:'A'}], 5, 1, true); return 'no-throw'; } catch (e) { return e.code; } })(),
              (() => { try { PairModel.projectSpread(null, 'leader', 'right'); return 'no-throw'; } catch (e) { return e.code; } })(),
              (() => { try { PairModel.projectSpread({right:null,left:null}, 'x', 'right'); return 'no-throw'; } catch (e) { return e.code; } })(),
              (() => { try { PairModel.projectSpread({right:null,left:null}, 'leader', 'up'); return 'no-throw'; } catch (e) { return e.code; } })(),
              (() => { try { PairModel.spreadIndexForPage([{id:'c0:s0'}], {name:1}); return 'no-throw'; } catch (e) { return e.code; } })()
            ]""")
        assert rejects == ["ERR_MALFORMED", "ERR_RANGE", "ERR_MALFORMED",
                           "ERR_MALFORMED", "ERR_RANGE", "ERR_MALFORMED"]
    finally:
        context.close()


def test_pair_model_js_is_single_source():
    """No second copy of the projection logic elsewhere in the frontend."""
    for rel in ("app/static/app.js", "app/static/viewer/spread.js",
                "app/static/viewer/overview.js", "app/static/viewer/zoom.js"):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "projectSpread" not in text, f"{rel} must not re-implement projectSpread"
    text = PAIR_MODEL_JS.read_text(encoding="utf-8")
    assert "globalThis.PairModel" in text
