#!/usr/bin/env python3
"""S10 (#85) — automated two-client verification runner for dual-reader (#75).

Drives ONE declarative scenario suite (scenarios.json) through two
interchangeable drivers:

    # two Chrome windows (two BrowserContexts, one Playwright process)
    uv run --with playwright --with websockets --with pillow \
        python scripts/reader-sync/verify.py \
        --driver chrome --url https://dev-03.<tailnet>.ts.net

    # two real iOS Simulators (explicit UDIDs)
    uv run --with websockets \
        python scripts/reader-sync/verify.py --driver simulator \
        --leader-udid <A> --follower-udid <B> --url http://127.0.0.1:8080

Exit codes (spec — いずれの失敗も exit 0 にしない):
    0  every scenario PASS
    1  an assertion failed (result JSON names the scenario)
    2  environment unavailable (no devices / idb / unreachable URL)
    3  an unsupported scenario, op, or surface was requested

Evidence lands in .omo/evidence/reader-sync/<run-id>/ — result.json with
the spec's required keys, both-screen screenshots per scenario, wire
traces, and the scenario definitions used. Codes, resume tokens,
credentials and real folder names are never recorded.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from verify_core import (  # noqa: E402
    CAP_WEBRTC,
    EXIT_ASSERT,
    EXIT_ENV,
    EXIT_PASS,
    EXIT_UNSUPPORTED,
    AssertError,
    EnvError,
    ScenarioError,
    UnsupportedError,
    EvidenceWriter,
    check_url_reachable,
    classify_url,
    exit_code_for,
    load_scenarios,
    new_run_id,
    os_info,
    resolve_plan,
    result_required_ok,
    runner_git_sha,
    self_check,
    target_git_sha,
    ws_url_for,
)
from verify_fixture_server import FixtureServer  # noqa: E402
from verify_drivers import (  # noqa: E402
    ChromeDriver,
    SimulatorDriver,
    ClientSpec,
)
from verify_ops import RunCtx, run_scenario  # noqa: E402

PREFLIGHT_TIMEOUT = 12.0


def _dev_url(name: str, repo_root: Path) -> str:
    out = subprocess.run(["./dev.sh", "url", name], cwd=str(repo_root),
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0 or not out.stdout.strip().startswith("http"):
        raise EnvError(f"dev.sh url {name}: {out.stderr.strip() or out.stdout.strip()}")
    return out.stdout.strip()


def _lan_ip_for(url: str) -> str:
    """Outbound IP that would reach the target host — the fixture origin
    for lan-route runs (an http://<lan-ip> page can ws:// the LAN box)."""
    host = url.split("//", 1)[-1].split("/")[0].split(":")[0]
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((host, 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError as e:
        raise EnvError(f"cannot determine LAN ip for {host}: {e}") from e


def _external_snapshot() -> dict:
    """Devices/envs the run must NOT disturb — compared before/after."""
    snap = {"simulators": [], "docker": []}
    try:
        out = subprocess.run(
            ["xcrun", "simctl", "list", "devices", "booted", "-j"],
            capture_output=True, text=True, timeout=15)
        if out.returncode == 0:
            data = json.loads(out.stdout)
            for lst in (data.get("devices") or {}).values():
                snap["simulators"] += [d["udid"] for d in lst
                                       if d.get("state") == "Booted"]
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    try:
        out = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=15)
        if out.returncode == 0:
            snap["docker"] = sorted(
                n for n in out.stdout.splitlines() if "bookq" in n)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return snap


def _parse(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="verify.py",
        description="dual-reader two-client verification runner (#85/S10)")
    p.add_argument("--driver", required=True,
                   choices=["chrome", "simulator"],
                   help="chrome = two Playwright BrowserContexts; "
                        "simulator = two explicit-UDID iOS Simulators")
    p.add_argument("--url", help="target env base URL "
                                 "(or use --dev-env to resolve via dev.sh)")
    p.add_argument("--dev-env", metavar="NAME",
                   help="resolve the URL via ./dev.sh url NAME")
    p.add_argument("--headed", action="store_true",
                   help="chrome driver: real visible windows")
    p.add_argument("--leader-udid", help="simulator: leader device UDID")
    p.add_argument("--follower-udid", help="simulator: follower device UDID")
    p.add_argument("--scenarios", metavar="PATH",
                   help="scenario declarations (default: bundled scenarios.json)")
    p.add_argument("--scenario", action="append", metavar="NAME",
                   help="run only the named scenario (repeatable)")
    p.add_argument("--surface", default="fixture",
                   choices=["fixture", "ui"],
                   help="fixture = runner-served test page (default); "
                        "ui = the real app pairing UI (needs S09)")
    p.add_argument("--run-id", help="override the evidence run id")
    p.add_argument("--evidence-root", metavar="DIR",
                   help="evidence root (default .omo/evidence/reader-sync)")
    p.add_argument("--keep", action="store_true",
                   help="leave launched resources up (debugging)")
    p.add_argument("--cmd-timeout", type=float, default=30.0,
                   help="per-control-command timeout in seconds")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse(argv)

    # ── declaration phase (exit 3 territory) ──────────────────────────
    try:
        scenario_data = load_scenarios(
            Path(args.scenarios) if args.scenarios else None)
    except ScenarioError as e:
        print(f"[verify] scenario declaration error: {e}", file=sys.stderr)
        return EXIT_UNSUPPORTED

    if args.driver == "simulator":
        if not args.leader_udid or not args.follower_udid:
            print("[verify] --driver simulator needs "
                  "--leader-udid and --follower-udid", file=sys.stderr)
            return EXIT_UNSUPPORTED
        if args.leader_udid == args.follower_udid:
            print("[verify] leader/follower UDIDs must differ", file=sys.stderr)
            return EXIT_UNSUPPORTED
    if args.surface == "ui" and args.driver == "simulator":
        print("[verify] --surface ui is not implemented for the simulator "
              "driver (real-UI AX driving lands with S09)", file=sys.stderr)
        return EXIT_UNSUPPORTED

    base_url = args.url
    if args.dev_env:
        try:
            base_url = _dev_url(args.dev_env, REPO_ROOT_RESOLVED)
        except EnvError as e:
            print(f"[verify] {e}", file=sys.stderr)
            return EXIT_ENV
    if not base_url:
        print("[verify] --url or --dev-env is required", file=sys.stderr)
        return EXIT_UNSUPPORTED
    base_url = base_url.rstrip("/")
    route = classify_url(base_url)
    try:
        ws_url = ws_url_for(base_url)
    except ScenarioError as e:
        print(f"[verify] {e}", file=sys.stderr)
        return EXIT_UNSUPPORTED

    run_id = args.run_id or new_run_id(args.driver)
    root = Path(args.evidence_root) if args.evidence_root else None
    evidence = EvidenceWriter(run_id, root) if root else EvidenceWriter(run_id)
    print(f"[verify] run_id={run_id} driver={args.driver} "
          f"surface={args.surface} route={route} url={base_url}")

    result = {
        "run_id": run_id,
        "driver": args.driver,
        "surface": args.surface,
        "route": route,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "build_sha": target_git_sha(base_url) or runner_git_sha(),
        "devices": {},
        "os": os_info(),
        "scenarios": [],
        "preflight": {},
    }
    before = _external_snapshot()
    result["external_state"] = {"before": before}

    fx: FixtureServer | None = None
    driver = None
    exit_code = EXIT_PASS
    expected_files = ["result.json"]
    try:
        # ── preflight: URL + driver + fixture server (exit 2 territory) ──
        health = check_url_reachable(base_url, timeout=PREFLIGHT_TIMEOUT)
        result["preflight"]["url"] = health
        if not health.get("ok"):
            raise EnvError(f"target unreachable: {health}")

        if args.driver == "chrome":
            driver = ChromeDriver(headed=args.headed)
        else:
            driver = SimulatorDriver(args.leader_udid, args.follower_udid)
        result["preflight"]["driver"] = driver.preflight()

        # fixture bind host: LAN runs need a non-loopback origin so ws://
        # to the LAN box is not mixed-content-blocked; loopback otherwise.
        if route == "lan":
            fx_host = _lan_ip_for(base_url)
            fx = FixtureServer(host="0.0.0.0", port=0)
            fx_url_host = fx_host
        else:
            fx = FixtureServer(host="127.0.0.1", port=0)
            fx_url_host = "127.0.0.1"
        fx.start()
        result["preflight"]["fixture_port"] = fx.port

        clients = {"leader": ClientSpec("leader", "leader"),
                   "follower": ClientSpec("follower", "follower")}

        if args.surface == "fixture":
            urls = {cid: fx.fixture_url(cid, spec.role, ws_url, base_url,
                                        host=fx_url_host)
                    for cid, spec in clients.items()}
        else:
            urls = {cid: base_url for cid in clients}

        driver.launch(clients, urls)
        result["devices"] = driver.devices(clients)

        hellos = {}
        if args.surface == "fixture":
            for cid in clients:
                hellos[cid] = fx.wait_hello(cid, timeout=60)
                if not hellos[cid].get("mods"):
                    raise EnvError(
                        f"fixture {cid}: production modules failed to load "
                        f"from {base_url}: {hellos[cid].get('error')}")
            result["devices"]["leader"]["ua"] = hellos["leader"].get("ua")
            result["devices"]["follower"]["ua"] = hellos["follower"].get("ua")
            result["devices"]["leader"]["hasRTC"] = hellos["leader"].get("hasRTC")
            result["devices"]["follower"]["hasRTC"] = \
                hellos["follower"].get("hasRTC")

        # capabilities -> run plan
        caps = set(driver.caps)
        if (hellos.get("leader", {}).get("hasRTC")
                and hellos.get("follower", {}).get("hasRTC")):
            caps.add(CAP_WEBRTC)
        result["capabilities"] = sorted(caps)
        plan, skipped = resolve_plan(
            scenario_data, driver=args.driver, route=route,
            caps=caps, only=args.scenario)
        result["plan"] = [{"name": e["name"], "transport": e["transport"]}
                          for e in plan]
        for sk in skipped:
            result["scenarios"].append(
                {"name": sk["name"], "transport": sk["transport"],
                 "status": sk["status"],
                 "assertions": [{"name": "skipped", "ok": False,
                                 "detail": sk["reason"]}]})

        # ── scenario loop ─────────────────────────────────────────────
        evidence.write_json(evidence.dir / "scenarios.json",
                            {"declared": scenario_data,
                             "plan": result["plan"],
                             "skipped": skipped})
        expected_files.append("scenarios.json")

        for entry in plan:
            sc = entry["scenario"]
            t = entry["transport"]
            sdir = evidence.scenario_dir(sc["name"], t)
            ops = list(sc["ops"])
            if t == "webrtc-dc" and not any(o["op"] == "dc_open" for o in ops):
                # the dc plane can only attach once a pair exists — splice
                # dc_open right after the pair op.
                for i, o in enumerate(ops):
                    if o["op"] == "pair":
                        ops.insert(i + 1, {"op": "dc_open"})
                        break
            ctx = RunCtx(fx=fx, driver=driver, clients=clients,
                         ws_url=ws_url, base_url=base_url,
                         evidence_dir=sdir, surface=args.surface)
            print(f"[verify] scenario {sc['name']} [{t}] ...")
            sres = run_scenario(ctx, {**sc, "ops": ops}, t)
            result["scenarios"].append(sres)
            for rel in (sres.get("artifacts") or {}).get("screenshots", []):
                expected_files.append(rel)
            print(f"[verify]   -> {sres['status']}"
                  f" ({sres['duration_ms']}ms)")

    except EnvError as e:
        result["preflight_error"] = str(e)
        print(f"[verify] environment unavailable: {e}", file=sys.stderr)
        exit_code = EXIT_ENV
    except UnsupportedError as e:
        print(f"[verify] unsupported: {e}", file=sys.stderr)
        exit_code = EXIT_UNSUPPORTED
    except AssertError as e:
        print(f"[verify] assertion: {e}", file=sys.stderr)
        exit_code = EXIT_ASSERT
    finally:
        # ── teardown: only what the runner itself started ─────────────
        if driver is not None and not args.keep:
            try:
                driver.stop()
            except Exception as e:
                print(f"[verify] driver stop error: {e}", file=sys.stderr)
        if fx is not None:
            try:
                fx.stop()
            except Exception:
                pass
        after = _external_snapshot()
        result["external_state"]["after"] = after
        result["external_state"]["unchanged"] = (
            after["simulators"] == before["simulators"])
        result["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        if exit_code == EXIT_PASS:
            exit_code = exit_code_for(
                [s["status"] for s in result["scenarios"]])
        summary = {"pass": 0, "fail": 0, "unsupported": 0,
                   "env_unavailable": 0}
        for s in result["scenarios"]:
            summary[s["status"]] = summary.get(s["status"], 0) + 1
        result["summary"] = summary
        # write first so the self-check sees result.json on disk, then
        # rewrite with the check folded in.
        evidence.write_json(evidence.dir / "result.json", result)
        result["self_check"] = self_check(evidence, result, expected_files)
        evidence.write_json(evidence.dir / "result.json", result)
        print(f"[verify] evidence: {evidence.dir}")
        print(f"[verify] result: {summary} -> exit {exit_code}")
    return exit_code


REPO_ROOT_RESOLVED = Path(__file__).resolve().parents[2]


if __name__ == "__main__":
    sys.exit(main())
