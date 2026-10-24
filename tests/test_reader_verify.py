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


# ── S11 (#86) additions ──────────────────────────────────────────────

ops_mod = runner_module("verify_ops")


def _summary_table():
    """The fixture-reported manifest summary shape {id,r,l} — what every
    expectation normalizes over (page names only, never folders)."""
    return [
        {"id": "c0:s0", "r": "p001.png", "l": "p002.png"},
        {"id": "c0:s1", "r": "p003.png", "l": "p004.png"},
        {"id": "c1:s0", "r": "q001.png", "l": "q002.png"},
        {"id": "c1:s1", "r": "q003.png", "l": None},
    ]


def test_expected_page_summary_shape():
    t = _summary_table()
    e = core.expected_page(t, "c0:s0", "leader", "right")
    assert e["side"] == "right" and e["page"] == "p001.png" and e["index"] == 0
    e = core.expected_page(t, "c0:s0", "follower", "right")
    assert e["side"] == "left" and e["page"] == "p002.png"
    # swapped leader side flips the projection
    e = core.expected_page(t, "c0:s1", "leader", "left")
    assert e["side"] == "left" and e["page"] == "p004.png"
    # blank side: page None -> BLANK marker
    e = core.expected_page(t, "c1:s1", "follower", "right")
    assert e["side"] == "left" and e["page"] is None
    assert core.spread_table_index(t, "nope") == -1


def test_turn_target_chapter_clamp():
    t = _summary_table()
    # cross-chapter clamp = global
    assert ops_mod._turn_target(t, 0, 1, True) == 1
    assert ops_mod._turn_target(t, 3, 1, True) == 3      # at end
    assert ops_mod._turn_target(t, 1, -1, True) == 0
    # chapter-limited stays inside the c{K} of the spread id
    assert ops_mod._turn_target(t, 1, 1, False) == 1     # c0 hi bound
    assert ops_mod._turn_target(t, 2, 1, False) == 3     # c1 can move
    assert ops_mod._turn_target(t, 2, -1, False) == 2    # c0->c1 denied
    assert ops_mod._turn_target(t, 0, -1, False) == 0
    assert ops_mod._chapter_of("c1:s3") == 1


def test_discover_work_validation(tmp_path):
    def w(obj):
        p = tmp_path / "sc.json"
        p.write_text(json.dumps(obj))
        return p
    # discover works are legal declarations
    data = core.load_scenarios(w({"scenarios": [
        {"name": "a", "ops": [{"op": "open_work"}],
         "work": {"discover": True, "minPages": 4}}]}))
    assert data["scenarios"][0]["work"]["discover"] is True
    with pytest.raises(core.ScenarioError):
        core.load_scenarios(w({"scenarios": [
            {"name": "a", "ops": [{"op": "open_work"}],
             "work": {"discover": True, "minPages": 1}}]}))


def test_repeat_and_clients_validation(tmp_path):
    def w(obj):
        p = tmp_path / "sc.json"
        p.write_text(json.dumps(obj))
        return p
    data = core.load_scenarios(w({"scenarios": [
        {"name": "a", "repeat": 3, "ops": [{"op": "pair"}],
         "clients": {"leader": "safari", "follower": "pwa"}}]}))
    plan, _ = core.resolve_plan(data, driver="simulator", route="lan",
                                caps={"pwa"})
    assert [e["run"] for e in plan] == [1, 2, 3]
    plan2, _ = core.resolve_plan(data, driver="simulator", route="lan",
                                 caps={"pwa"}, repeat=1)
    assert len(plan2) == 1
    with pytest.raises(core.ScenarioError):
        core.load_scenarios(w({"scenarios": [
            {"name": "a", "ops": [{"op": "pair"}],
             "clients": {"leader": "electron"}}]}))
    with pytest.raises(core.ScenarioError):
        core.load_scenarios(w({"scenarios": [
            {"name": "a", "ops": [{"op": "pair"}], "repeat": 0}]}))


def test_concurrent_and_ui_input_validation(tmp_path):
    def w(obj):
        p = tmp_path / "sc.json"
        p.write_text(json.dumps(obj))
        return p
    with pytest.raises(core.ScenarioError):
        core.load_scenarios(w({"scenarios": [
            {"name": "a", "ops": [{"op": "concurrent", "ops": []}]}]}))
    with pytest.raises(core.ScenarioError):
        core.load_scenarios(w({"scenarios": [
            {"name": "a", "ops": [{"op": "concurrent", "ops": [
                {"op": "dc_open"}]}]}]}))
    with pytest.raises(core.ScenarioError):
        core.load_scenarios(w({"scenarios": [
            {"name": "a", "ops": [{"op": "ui_input", "kind": "warp"}]}]}))
    with pytest.raises(core.ScenarioError):
        core.load_scenarios(w({"scenarios": [
            {"name": "a", "ops": [{"op": "pair", "via": "smoke"}]}]}))


def test_redact_folder_keys():
    wire = {"type": "OPEN_WORK",
            "payload": {"folder": "実フォルダ/第1巻",
                        "page": {"folder": "実フォルダ/第1巻",
                                 "name": "p001.png"},
                        "code": "123456"}}
    r = core.redact(wire)
    assert r["payload"]["folder"].startswith("sha256:")
    assert "実フォルダ" not in json.dumps(r, ensure_ascii=False)
    assert r["payload"]["page"]["name"] == "p001.png"   # page names stay
    assert r["payload"]["code"] == "<redacted>"


def test_leak_scan_in_self_check(tmp_path):
    ev = core.EvidenceWriter("t-leak", tmp_path)
    ev.write_json(ev.dir / "result.json", mini_result())
    (ev.dir / "trace-leader.jsonl").write_text(
        json.dumps({"type": "GOTO",
                    "payload": {"page": {"folder": "SecretShelf",
                                         "name": "p1.png"}}}) + "\n")
    chk = core.self_check(ev, mini_result(), ["result.json"])
    assert not chk["passed"] and chk["leaks"]
    # redacted trace passes the scan
    (ev.dir / "trace-leader.jsonl").write_text(
        json.dumps(core.redact({"type": "GOTO", "payload": {
            "page": {"folder": "SecretShelf", "name": "p1.png"}}}),
            ensure_ascii=False) + "\n")
    chk = core.self_check(ev, mini_result(), ["result.json"])
    assert chk["passed"] and chk["leaks"] == []


def test_expect_match_fields():
    st = {"phase": "reading", "status": "connected",
          "transport": "server-ws", "applied": {"index": 3,
                                              "spreadId": "c0:s3"},
          "rejected": ["peer_offline"],
          "presence": {"a": {"status": "suspended"},
                       "b": {"status": "suspended"}},
          "marker": {"page": "p001.png", "side": "right",
                     "imgState": "ok"},
          "error": None, "endReason": None, "sessionId": "s"}
    assert ops_mod._expect_match(st, {"phase": "reading",
                                      "applied_index": 3})
    assert ops_mod._expect_match(st, {"rejected": "peer_offline"})
    assert ops_mod._expect_match(st, {"presence": "suspended"})
    assert ops_mod._expect_match(st, {"img_state": "ok",
                                      "marker_page": "p001.png"})
    assert not ops_mod._expect_match(st, {"phase": "loading"})
    assert not ops_mod._expect_match(st, {"rejected": "stale"})
    assert not ops_mod._expect_match(st, {"presence": "active"})


# ── S12 (#87) — real driver + peer_latency + /ctrl/event ─────────────

drivers_mod = runner_module("verify_drivers")


def test_real_driver_surface():
    """RealDriver: manual launch prints URLs, caps keep physical-action
    ops honestly classified, and automation methods raise Unsupported."""
    d = drivers_mod.RealDriver()
    assert d.name == "real" and d.preflight()["manual"] is True
    assert d.caps == {"ws_bystander"}
    spec = drivers_mod.ClientSpec("leader", "leader")
    for call in (lambda: d.tap(spec, 1, 1),
                 lambda: d.swipe(spec, 0, 0, 1, 1),
                 lambda: d.rotate(spec, "LANDSCAPE_LEFT"),
                 lambda: d.screenshot(spec, "/tmp/x.png"),
                 lambda: d.background(spec),
                 lambda: d.screen_size(spec),
                 lambda: d.tap_element(spec, "x")):
        with pytest.raises(core.UnsupportedError):
            call()
    urls = {"leader": "http://192.168.1.5:9/fixture?id=leader",
            "follower": "http://192.168.1.5:9/fixture?id=follower"}
    clients = {"leader": spec,
               "follower": drivers_mod.ClientSpec("follower", "follower")}
    d.launch(clients, urls)                      # prints, no automation
    devs = d.devices(clients)
    assert devs["leader"]["kind"] == "real-device"
    assert clients["leader"].launch_url == urls["leader"]
    d.stop()


def test_resolve_plan_real_driver():
    """'real' is a first-class driver: ctl-plane scenarios plan on it,
    device-input-requires classify env_unavailable (never unsupported,
    never a mid-run crash)."""
    data = {"scenarios": [
        {"name": "real_ok", "ops": [{"op": "pair"}]},
        {"name": "needs_hands", "ops": [{"op": "device_input",
                                         "kind": "tap"}],
         "requires": ["device_inputs"]},
        {"name": "needs_rtc", "ops": [{"op": "peer_latency"}],
         "requires": ["webrtc"]},
        {"name": "chrome_only", "ops": [{"op": "pair"}],
         "drivers": ["chrome"]},
    ]}
    real_caps = {"ws_bystander"}
    plan, skipped = core.resolve_plan(data, driver="real", route="lan",
                                      caps=real_caps, only=None)
    names = [e["name"] for e in plan]
    assert "real_ok" in names
    assert "needs_hands" not in names and "needs_rtc" not in names
    assert "chrome_only" not in names
    sk = {s["name"]: s for s in skipped}
    assert sk["needs_hands"]["status"] == "env_unavailable"
    assert sk["needs_rtc"]["status"] == "env_unavailable"


def test_bundled_real_scenarios_validate():
    """scenarios-real.json parses + plans on BOTH real and chrome."""
    data = core.load_scenarios(RUNNER_DIR / "scenarios-real.json")
    names = {s["name"] for s in data["scenarios"]}
    assert {"real_ws_acceptance", "real_peer_latency_ws",
            "real_peer_latency_dc", "real_disconnect_resume",
            "real_reload_resume", "real_work_change"} <= names
    for drv in ("real", "chrome"):
        plan, skipped = core.resolve_plan(
            data, driver=drv, route="lan",
            caps={"webrtc", "ws_bystander"}, repeat=1)
        assert len(plan) == len(data["scenarios"]), (drv, skipped)
        for e in plan:
            assert "drivers" not in e["scenario"] or \
                drv in e["scenario"]["drivers"]


def test_peer_latency_validation(tmp_path):
    def w(obj):
        p = tmp_path / "sc.json"
        p.write_text(json.dumps(obj))
        return p
    data = core.load_scenarios(w({"scenarios": [
        {"name": "a", "ops": [{"op": "peer_latency", "samples": 100,
                               "actor": "both"}]}]}))
    assert data["scenarios"][0]["ops"][0]["actor"] == "both"
    with pytest.raises(core.ScenarioError):
        core.load_scenarios(w({"scenarios": [
            {"name": "a", "ops": [{"op": "peer_latency", "samples": 0}]}]}))
    with pytest.raises(core.ScenarioError):
        core.load_scenarios(w({"scenarios": [
            {"name": "a", "ops": [{"op": "peer_latency",
                                   "actor": "ghost"}]}]}))


# ── /ctrl/event + wait_displayed ──────────────────────────────────────


def test_ctrl_event_wait_displayed(tmp_path):
    """post a displayed event -> wait_displayed returns it; min_rev
    filters; backlog arriving BEFORE the wait still matches."""
    srv = fserver.FixtureServer(port=0)
    srv.start()
    try:
        ctl = fx_control_client(srv, "leader")
        # backlog-before-wait: event lands first, wait still sees it
        ctl.post("/ctrl/event", {"id": "leader", "type": "displayed",
                                 "rev": 3, "imgState": "ok", "at": 1.0})
        ev = srv.wait_displayed("leader", min_rev=1, timeout=2)
        assert ev["rev"] == 3 and ev["imgState"] == "ok"
        # min_rev filtering: rev 3 must not satisfy rev>=4
        with pytest.raises(fserver.CmdError):
            srv.wait_displayed("leader", min_rev=4, timeout=0.4)
        # a later event clears the gate
        ctl.post("/ctrl/event", {"id": "leader", "type": "displayed",
                                 "rev": 4, "imgState": "blank", "at": 2.0})
        ev = srv.wait_displayed("leader", min_rev=4, timeout=2)
        assert ev["rev"] == 4
        # non-displayed events never satisfy
        ctl.post("/ctrl/event", {"id": "leader", "type": "other",
                                 "rev": 9})
        with pytest.raises(fserver.CmdError):
            srv.wait_displayed("leader", min_rev=5, timeout=0.4)
        # drain drops the backlog so only fresh events count
        srv.drain_events("leader")
        with pytest.raises(fserver.CmdError):
            srv.wait_displayed("leader", min_rev=1, timeout=0.4)
        # async post while blocked wakes the waiter
        def late():
            time.sleep(0.15)
            srv.post_event({"id": "leader", "type": "displayed",
                            "rev": 8, "imgState": "ok", "at": 3.0})
        threading.Thread(target=late, daemon=True).start()
        assert srv.wait_displayed("leader", min_rev=5,
                                  timeout=3)["rev"] == 8
    finally:
        srv.stop()


def test_fixture_shortcut_redirects():
    """/r/<client> 302s to the stored fixture URL; /r lists links."""
    import urllib.request
    import urllib.error

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    srv = fserver.FixtureServer(port=0)
    srv.start()
    try:
        url = srv.fixture_url("leader", "leader", "ws://x/ws",
                              "http://x", host="192.168.1.5")
        assert srv.short_url("leader").endswith("/r/leader")
        assert srv.short_url("leader").startswith("http://192.168.1.5:")
        opener = urllib.request.build_opener(NoRedirect)
        try:
            # dial the loopback bind — the stored URL itself is the LAN
            # host a phone would reach, which this machine can't.
            opener.open(srv.base_url() + "/r/leader", timeout=5)
            assert False, "expected 302"
        except urllib.error.HTTPError as e:
            assert e.code == 302 and e.headers["Location"] == url
        with urllib.request.urlopen(
                srv.base_url() + "/r", timeout=5) as r:
            assert b"/r/leader" in r.read()
    finally:
        srv.stop()


# ── peer_latency op ───────────────────────────────────────────────────


class _FakeFx:
    """Stand-in fixture server for op-level tests: state comes from a
    scripted map, wait_displayed consumes scripted events/timeouts."""

    def __init__(self, peer_rev=0, events=None):
        self.rev = peer_rev
        self.drained = []
        self.waits = []
        self._events = list(events or [])

    def drain_events(self, cid):
        self.drained.append(cid)

    def wait_displayed(self, cid, min_rev, timeout):
        self.waits.append((cid, min_rev, timeout))
        ev = self._events.pop(0) if self._events else None
        if ev is None:
            raise fserver.CmdError("env", "no displayed event")
        return ev


class _FakeSpec:
    def __init__(self, cid, role):
        self.id = cid
        self.role = role


class _FakeCtx:
    def __init__(self, fx, states=None, cmd_map=None):
        self.fx = fx
        self.surface = "fixture"
        self.clients = {"leader": _FakeSpec("leader", "leader"),
                        "follower": _FakeSpec("follower", "follower")}
        self._states = states or {}
        self._cmd_map = cmd_map or {}
        self._table = [{"id": "c0:s0"}, {"id": "c0:s1"}]
        self.assertions = []
        self.screenshots = []
        self.rtt = {}
        self.latency = {}

    def note(self, name, ok, detail=""):
        self.assertions.append({"name": name, "ok": bool(ok),
                                "detail": str(detail)})
        return ok

    def state(self, role):
        return self._states[role]

    def cmd(self, role, name, params=None, timeout=30.0):
        fn = self._cmd_map.get(name)
        if fn:
            return fn(role, params or {})
        return {"sent": True}


def _lat_ctx(events, rev=4, cmd_map=None):
    fx = _FakeFx(events=events)
    st = {"applied": {"revision": rev, "index": 0, "spreadId": "c0:s0"},
          "transport": "server-ws", "phase": "reading",
          "sessionId": "s", "workEpoch": 1}
    ctx = _FakeCtx(fx, states={"leader": dict(st),
                               "follower": dict(st)},
                   cmd_map=cmd_map)
    return ctx, fx


def test_peer_latency_records_stats():
    """Successful samples land under peer:<transport>:<a>→<p> with the
    spec stat shape; waits demand rev >= prev+1 after a drain."""
    ctx, fx = _lat_ctx(events=[{"rev": 5, "type": "displayed"}] * 8, rev=4)
    ops_mod.op_peer_latency(ctx, {"op": "peer_latency", "samples": 8,
                                  "actor": "leader"})
    key = "peer:server-ws:leader→follower"
    stats = ctx.latency[key]
    assert stats["n"] == 8 and stats["failures"] == 0
    assert stats["p50"] is not None and stats["p95"] is not None
    assert stats["max"] is not None and "upper bound" in stats["method"]
    # every wait: follower cid, rev >= prev_rev+1, after a drain
    assert len(fx.drained) == 8 and fx.drained == ["follower"] * 8
    assert all(w[0] == "follower" and w[1] == 5 for w in fx.waits)
    note = ctx.assertions[-1]
    assert note["name"] == "peer_latency_leader" and note["ok"]


def test_peer_latency_timeouts_and_refusals_counted():
    """Timeouts (no event) AND intent refusals (sent:false) land in
    failures — never silently dropped, never deleted as 'slow'."""
    events = [{"rev": 5, "type": "displayed"}, None,
              {"rev": 5, "type": "displayed"}, {"rev": 5,
                                              "type": "displayed"}]
    sends = iter([{"sent": True}, {"sent": True},
                  {"sent": False}, {"sent": True}])

    def turn(role, params):
        return next(sends)
    ctx, fx = _lat_ctx(events=events, rev=4, cmd_map={"turn": turn})
    ops_mod.op_peer_latency(ctx, {"op": "peer_latency", "samples": 4,
                                  "actor": "leader"})
    stats = ctx.latency["peer:server-ws:leader→follower"]
    assert stats["n"] == 2 and stats["failures"] == 2


def test_peer_latency_both_directions_separate_keys():
    ctx, fx = _lat_ctx(events=[{"rev": 5, "type": "displayed"}] * 8, rev=4)
    ops_mod.op_peer_latency(ctx, {"op": "peer_latency", "samples": 4,
                                  "actor": "both"})
    assert ctx.latency["peer:server-ws:leader→follower"]["n"] == 4
    assert ctx.latency["peer:server-ws:follower→leader"]["n"] == 4
    assert len(fx.waits) == 8
    cids = {w[0] for w in fx.waits}
    assert cids == {"leader", "follower"}


def test_peer_latency_uses_live_transport_key():
    """The stats key follows the peer's effective plane so webrtc-dc
    runs record under the right transport."""
    ctx, fx = _lat_ctx(events=[{"rev": 5, "type": "displayed"}] * 3, rev=4)
    for st in ctx._states.values():
        st["transport"] = "webrtc-dc"
    ops_mod.op_peer_latency(ctx, {"op": "peer_latency", "samples": 3,
                                  "actor": "leader"})
    assert "peer:webrtc-dc:leader→follower" in ctx.latency


def test_peer_latency_all_failed_is_a_bad_note():
    """n=0 with failures means the run honestly reports a failed
    assertion — never a fabricated zero-latency pass."""
    ctx, fx = _lat_ctx(events=[], rev=4)
    ops_mod.op_peer_latency(ctx, {"op": "peer_latency", "samples": 3,
                                  "actor": "leader"})
    stats = ctx.latency["peer:server-ws:leader→follower"]
    assert stats["n"] == 0 and stats["failures"] == 3
    assert ctx.assertions[-1]["ok"] is False


def test_peer_latency_needs_table_and_fixture():
    ctx, _ = _lat_ctx(events=[], rev=4)
    ctx._table = None
    with pytest.raises(core.UnsupportedError):
        ops_mod.op_peer_latency(ctx, {"op": "peer_latency"})
    ctx, _ = _lat_ctx(events=[], rev=4)
    ctx.surface = "ui"
    with pytest.raises(core.UnsupportedError):
        ops_mod.op_peer_latency(ctx, {"op": "peer_latency"})
