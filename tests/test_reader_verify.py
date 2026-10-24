"""S10 (#85) — unit tests for the dual-reader verification runner.

Covers the runner's own contract without any device: argument parsing,
scenario declaration validation, run-plan resolution, result JSON shape,
exit-code mapping, redaction, the fixture control channel, evidence
self-check, and the marker palette used by the pixel assertions.
"""
from __future__ import annotations

import json
import os
import threading
import time

os.environ["BOOKQ_TEST_MODE"] = "true"

import pytest  # noqa: E402

from tests.reader_sync_helpers import (  # noqa: E402
    RUNNER_DIR,
    fx_control_client,
    mini_result,
    runner_module,
)

core = runner_module("verify_core")
fserver = runner_module("verify_fixture_server")


# ── exit codes ────────────────────────────────────────────────────────

def test_exit_codes():
    assert core.exit_code_for(["pass", "pass"]) == 0
    assert core.exit_code_for(["pass", "fail"]) == 1
    assert core.exit_code_for(["pass", "env_unavailable"]) == 2
    assert core.exit_code_for(["pass", "unsupported"]) == 3
    # precedence: assert-fail beats unsupported beats env beats pass
    assert core.exit_code_for(["env_unavailable", "unsupported"]) == 3
    assert core.exit_code_for(["env_unavailable", "fail"]) == 1
    assert core.exit_code_for([]) == 3          # empty plan = bad request


# ── URL classification ───────────────────────────────────────────────

def test_classify_url():
    assert core.classify_url("https://dev-03.x.ts.net") == "tailnet"
    assert core.classify_url("https://bookq.x.ts.net") == "tailnet"
    assert core.classify_url("http://127.0.0.1:8080") == "loopback"
    assert core.classify_url("http://localhost:8080") == "loopback"
    assert core.classify_url("http://192.168.1.5:8080") == "lan"
    assert core.ws_url_for("https://d.ts.net") == \
        "wss://d.ts.net/ws/reader-sync"
    assert core.ws_url_for("http://127.0.0.1:8080/") == \
        "ws://127.0.0.1:8080/ws/reader-sync"
    with pytest.raises(core.ScenarioError):
        core.ws_url_for("ftp://x")


# ── scenario declarations ────────────────────────────────────────────

def test_bundled_scenarios_validate():
    data = core.load_scenarios()
    names = [s["name"] for s in data["scenarios"]]
    assert len(names) == len(set(names))
    for sc in data["scenarios"]:
        assert sc["ops"], sc["name"]


def test_load_scenarios_rejects_bad(tmp_path):
    def w(obj):
        p = tmp_path / "sc.json"
        p.write_text(json.dumps(obj))
        return p
    with pytest.raises(core.ScenarioError):
        core.load_scenarios(w({"scenarios": [{"name": "a", "ops": []}]}))
    with pytest.raises(core.ScenarioError):
        core.load_scenarios(w({"scenarios": [
            {"name": "a", "ops": [{"op": "explode"}]}]}))
    with pytest.raises(core.ScenarioError):
        core.load_scenarios(w({"scenarios": [
            {"name": "a", "ops": [{"op": "pair"}]},
            {"name": "a", "ops": [{"op": "pair"}]}]}))
    with pytest.raises(core.ScenarioError):
        core.load_scenarios(w({"scenarios": [
            {"name": "a", "ops": [{"op": "turn", "delta": 5}]}]}))
    with pytest.raises(core.ScenarioError):
        core.load_scenarios(w({"scenarios": [
            {"name": "a", "ops": [{"op": "fault", "type": "nuke"}]}]}))
    with pytest.raises(core.ScenarioError):
        core.load_scenarios(w({"scenarios": [
            {"name": "a", "ops": [{"op": "pair"}],
             "work": {"offset": 7, "chapters": []}}]}))
    with pytest.raises(core.ScenarioError):
        core.load_scenarios(w({"scenarios": [
            {"name": "a", "ops": [{"op": "pair"}],
             "work": {"chapters": [{"folder": "Real Folder", "pages": ["x"]}]}
        }]}))
    with pytest.raises(core.ScenarioError):
        core.load_scenarios(w({"scenarios": [
            {"name": "a", "ops": [{"op": "pair"}], "requires": ["warp"]}]}))
    with pytest.raises(core.ScenarioError):
        core.load_scenarios(tmp_path / "missing.json")


# ── run-plan resolution ──────────────────────────────────────────────

def _plan_fixture(**kw):
    data = {"scenarios": [
        {"name": "both", "ops": [{"op": "pair"}],
         "transports": ["server-ws", "webrtc-dc"]},
        {"name": "simonly", "ops": [{"op": "pair"}],
         "drivers": ["simulator"]},
        {"name": "needrtc", "ops": [{"op": "dc_open"}],
         "requires": ["webrtc"]},
        {"name": "lanonly", "ops": [{"op": "pair"}], "route": "lan"},
    ]}
    return core.resolve_plan(data, **kw)


def test_resolve_plan_chrome():
    caps = {"webrtc", "ws_bystander"}
    plan, skipped = _plan_fixture(driver="chrome", route="tailnet", caps=caps)
    names = [(e["name"], e["transport"]) for e in plan]
    assert ("both", "server-ws") in names
    assert ("both", "webrtc-dc") in names
    assert ("needrtc", None) not in names
    assert all(n != "simonly" for n, _ in names)
    assert all(n != "lanonly" for n, _ in names)


def test_resolve_plan_simulator_and_missing_cap():
    caps = {"device_inputs", "ws_bystander"}       # no webrtc
    plan, skipped = _plan_fixture(driver="simulator", route="tailnet",
                                  caps=caps)
    names = [(e["name"], e["transport"]) for e in plan]
    assert ("both", "server-ws") in names
    assert ("both", "webrtc-dc") not in names       # cap-gated out
    assert ("simonly", "server-ws") in names
    sk = {s["name"]: s for s in skipped}
    assert sk["needrtc"]["status"] == "env_unavailable"


def test_resolve_plan_explicit_unsupported():
    plan, skipped = _plan_fixture(driver="chrome", route="tailnet",
                                  caps=set(), only=["simonly", "ghost"])
    sk = {s["name"]: s["status"] for s in skipped}
    assert sk["simonly"] == "unsupported"
    assert sk["ghost"] == "unsupported"


# ── result JSON / evidence ───────────────────────────────────────────

def test_result_required_ok():
    ok, missing = core.result_required_ok(mini_result())
    assert ok, missing
    ok, missing = core.result_required_ok(mini_result(scenarios=[{"name": "x"}]))
    assert not ok and any("transport" in m for m in missing)
    ok, missing = core.result_required_ok({"run_id": "x"})
    assert not ok and "scenarios" in missing


def test_evidence_self_check(tmp_path):
    ev = core.EvidenceWriter("t-run", tmp_path)
    ev.write_json(ev.dir / "result.json", mini_result())
    ev.write_json(ev.dir / "scenarios.json", {"declared": {}})
    sc = ev.scenario_dir("turn_convergence", "server-ws")
    (sc / "opened-leader.png").write_bytes(b"png")
    res = mini_result(scenarios=[{
        "name": "turn_convergence", "transport": "server-ws",
        "status": "pass",
        "assertions": [{"name": "a", "ok": True}],
        "artifacts": {"screenshots": [
            "turn_convergence/server-ws/opened-leader.png"]}}])
    chk = core.self_check(ev, res, [
        "result.json", "scenarios.json",
        "turn_convergence/server-ws/opened-leader.png"])
    assert chk["passed"] and not chk["missing_files"]
    chk2 = core.self_check(ev, res, ["result.json", "gone.png"])
    assert not chk2["passed"] and "gone.png" in chk2["missing_files"]


# ── redaction ────────────────────────────────────────────────────────

def test_redact_secrets():
    ev = {"code": "123456", "resumeToken": "tok", "nested": {"token": "x"},
          "name": "fx:workA/p001.png", "rev": 3}
    r = core.redact(ev)
    assert r["code"] == "<redacted>" and r["resumeToken"] == "<redacted>"
    assert r["nested"]["token"] == "<redacted>"
    assert r["name"] == "fx:workA/p001.png" and r["rev"] == 3


def test_safe_name():
    assert core.safe_name("fx:workA/p001.png") == "fx:workA/p001.png"
    real = core.safe_name("私の漫画フォルダ")
    assert real.startswith("sha256:") and "私の漫画" not in real


# ── spread table + markers ───────────────────────────────────────────

def test_spread_table_and_markers():
    from tests.fixtures.reader_sync_tree import WORKS
    table = core.build_spread_table(WORKS["fx6"])
    assert len(table) == 3
    assert table[0]["right"]["name"].endswith("p001.png")
    assert table[0]["left"]["name"].endswith("p002.png")
    m = core.expected_marker(table, 0, "leader", "right")
    assert m["side"] == "right" and m["ref"]["name"].endswith("p001.png")
    m = core.expected_marker(table, 0, "follower", "right")
    assert m["side"] == "left" and m["ref"]["name"].endswith("p002.png")
    # fx5: last spread has a blank left
    t5 = core.build_spread_table(WORKS["fx5"])
    assert len(t5) == 3 and t5[-1]["left"] is None
    m = core.expected_marker(t5, 2, "follower", "right")
    assert m["side"] == "left" and m["ref"] is None
    # offset=1: first spread right-only
    t1 = core.build_spread_table(WORKS["fx5_offset1"])
    assert t1[0]["right"]["name"].endswith("p001.png") and t1[0]["left"] is None


def test_marker_rgb_and_png(tmp_path):
    try:
        from PIL import Image
    except ImportError:
        pytest.skip("pillow not installed")
    rgb = core.marker_rgb("fx:workA/p001.png")
    im = Image.new("RGB", (100, 100), rgb)
    p = tmp_path / "m.png"
    im.save(p)
    got = core.png_region_rgb(p, box=(0.0, 0.0, 1.0, 1.0))
    assert core.rgb_close(got, rgb)
    # distinct pages -> distinct colors (determinism)
    assert core.marker_rgb("fx:workA/p001.png") != \
        core.marker_rgb("fx:workA/p002.png")
    assert core.marker_rgb("fx:workA/p001.png") == \
        core.marker_rgb("fx:workA/p001.png")


def test_rtt_stats():
    s = core.rtt_stats([10, 20, 30, 40, 50])
    assert s["n"] == 5 and s["min"] == 10 and s["max"] == 50
    assert s["p50"] == 30
    assert core.rtt_stats([])["n"] == 0


# ── fixture control server (no browser) ──────────────────────────────

def test_fixture_server_roundtrip(tmp_path):
    srv = fserver.FixtureServer(port=0)
    srv.start()
    try:
        ctl = fx_control_client(srv, "leader")
        hello = srv.wait_hello("leader", timeout=5)
        assert hello["ua"] == "unit-test"

        def handler(cmd, params):
            if cmd == "state":
                return {"phase": "paired"}
            if cmd == "boom":
                raise RuntimeError("nope")
            raise ValueError("unknown")

        def run_cmd(name):
            box = {}

            def go():
                try:
                    box["r"] = srv.cmd("leader", name)
                except Exception as e:    # noqa: BLE001
                    box["e"] = e

            t = threading.Thread(target=go, daemon=True)
            t.start()
            for _ in range(50):
                if ctl.pump_once(handler) == name:
                    break
                time.sleep(0.02)
            t.join(timeout=5)
            if "e" in box:
                raise box["e"]
            return box.get("r", None)

        assert run_cmd("state") == {"phase": "paired"}
        with pytest.raises(fserver.CmdError) as ei:
            run_cmd("boom")
        assert ei.value.kind == "error"
    finally:
        srv.stop()


def test_fixture_page_substitution():
    srv = fserver.FixtureServer(port=0)
    srv.start()
    try:
        html = srv.fixture_page("leader", "leader",
                                "wss://d.ts.net/ws/reader-sync",
                                "https://d.ts.net", "h:1").decode()
        assert "__FX_CONFIG_JSON__" not in html
        cfg = json.loads(html.split("var FXC = ", 1)[1].split(";", 1)[0])
        assert cfg["wsUrl"] == "wss://d.ts.net/ws/reader-sync"
        assert cfg["ctrlBase"] == "http://h:1/ctrl"
        assert cfg["mods"][0].endswith("/static/viewer/pairTransport.js")
    finally:
        srv.stop()


# ── CLI parsing (no run) ─────────────────────────────────────────────

def test_cli_parse():
    verify = runner_module("verify")
    a = verify._parse(["--driver", "chrome", "--url", "https://d.ts.net"])
    assert a.driver == "chrome" and a.url == "https://d.ts.net"
    assert a.surface == "fixture" and not a.headed
    a = verify._parse(["--driver", "simulator", "--leader-udid", "A",
                       "--follower-udid", "B", "--url", "http://x",
                       "--scenario", "s1", "--scenario", "s2"])
    assert a.leader_udid == "A" and a.scenario == ["s1", "s2"]
    with pytest.raises(SystemExit):
        verify._parse(["--url", "https://d.ts.net"])      # --driver required
    with pytest.raises(SystemExit):
        verify._parse(["--driver", "firefox"])            # bad choice


def test_cli_unsupported_surfaces():
    verify = runner_module("verify")
    # simulator without both UDIDs -> exit 3 (declaration problem)
    rc = verify.main(["--driver", "simulator", "--leader-udid", "A",
                      "--url", "http://127.0.0.1:1"])
    assert rc == core.EXIT_UNSUPPORTED
    # same UDID twice -> exit 3
    rc = verify.main(["--driver", "simulator", "--leader-udid", "A",
                      "--follower-udid", "A", "--url", "http://127.0.0.1:1"])
    assert rc == core.EXIT_UNSUPPORTED
    # ui surface on simulator -> exit 3 today
    rc = verify.main(["--driver", "simulator", "--leader-udid", "A",
                      "--follower-udid", "B", "--surface", "ui",
                      "--url", "http://127.0.0.1:1"])
    assert rc == core.EXIT_UNSUPPORTED
    # no URL at all -> exit 3
    rc = verify.main(["--driver", "chrome"])
    assert rc == core.EXIT_UNSUPPORTED


def test_cli_env_unavailable(tmp_path):
    """Unreachable target URL -> exit 2, and result JSON still lands."""
    verify = runner_module("verify")
    # port 1 — nothing listening; quick refusal
    rc = verify.main(["--driver", "chrome",
                      "--url", "http://127.0.0.1:1",
                      "--evidence-root", str(tmp_path),
                      "--run-id", "t-env"])
    assert rc == core.EXIT_ENV
    res = json.loads((tmp_path / "t-env" / "result.json").read_text())
    assert res["run_id"] == "t-env" and res["driver"] == "chrome"
    assert res["preflight"]["url"]["ok"] is False
    assert res["summary"]["pass"] == 0


def test_cli_bad_scenarios_file(tmp_path):
    verify = runner_module("verify")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"scenarios": [{"name": "x", "ops": []}]}))
    rc = verify.main(["--driver", "chrome", "--scenarios", str(bad),
                      "--url", "http://127.0.0.1:1"])
    assert rc == core.EXIT_UNSUPPORTED
