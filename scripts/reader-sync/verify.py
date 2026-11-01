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
    CAP_SERVER_RESTART,
    CAP_REAL_UI,
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
    RealDriver,
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


def _dev_container(name: str) -> str | None:
    """The viewer container behind a named dev env — found via the
    com.bookq.name docker label (the env pool's source of truth)."""
    try:
        out = subprocess.run(
            ["docker", "ps", "--filter", f"label=com.bookq.name={name}",
             "--filter", "label=com.docker.compose.service=viewer",
             "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    names = [n for n in out.stdout.splitlines() if n.strip()]
    return names[0] if out.returncode == 0 and names else None


def _lan_ip_for(url: str) -> str:
    """Outbound IP that would reach the target host — the fixture origin
    for lan-route runs (an http://<lan-ip> page can ws:// the LAN box)."""
    host = url.split("//", 1)[-1].split("/")[0].split(":")[0]
    return _ip_towards(host)


def _ip_towards(host: str) -> str:
    """Source IP the OS would use to reach ``host`` — the Mac's LAN
    address for a LAN target, its tailscale address for a tailnet one.
    Either way it is an address a physical device can route to."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((host, 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError as e:
        raise EnvError(f"cannot determine LAN ip for {host}: {e}") from e


def _url_host(url: str) -> str:
    return url.split("//", 1)[-1].split("/")[0].split(":")[0]


def _replace_url_host(url: str, host: str) -> str:
    pre, _, rest = url.partition("//")
    hostport, _, path = rest.partition("/")
    port = ""
    if ":" in hostport:
        port = ":" + hostport.split(":", 1)[1]
    return f"{pre}//{host}{port}{'/' + path if path else ''}"


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
                   choices=["chrome", "simulator", "real"],
                   help="chrome = two Playwright BrowserContexts; "
                        "simulator = two explicit-UDID iOS Simulators; "
                        "real = two PHYSICAL devices a human opens the "
                        "printed fixture URLs on (S12/#87)")
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
    p.add_argument("--repeat", type=int, metavar="N",
                   help="override every scenario's repeat count "
                        "(同一固定操作列の連続run)")
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
    if args.surface == "ui" and args.driver == "real":
        print("[verify] --surface ui is not implemented for the real "
              "driver (the real-UI DOM has no automation path on a "
              "physical device)", file=sys.stderr)
        return EXIT_UNSUPPORTED
    if args.driver == "real" and (args.leader_udid or args.follower_udid):
        print("[verify] --leader-udid/--follower-udid are simulator-only; "
              "the real driver takes physical devices as they are",
              file=sys.stderr)
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
    # a wildcard bind address (dev.sh --local --lan-allow reports
    # http://0.0.0.0:<port>) is listenable but not dialable — physical
    # devices need the Mac's real LAN address instead.
    if args.driver == "real" and _url_host(base_url) in ("0.0.0.0", "::"):
        lan_ip = _ip_towards("8.8.8.8")
        base_url = _replace_url_host(base_url, lan_ip)
        print(f"[verify] real driver: --url host rewritten "
              f"0.0.0.0 -> {lan_ip} (devices dial the LAN address)")
    route = classify_url(base_url)
    if args.driver == "real" and route == "loopback":
        print("[verify] real driver: physical devices cannot reach a "
              "loopback target — use a tailnet dev env (--dev-env NAME) "
              "or a LAN-exposed ./dev.sh up NAME --local --lan-allow URL",
              file=sys.stderr)
        return EXIT_ENV
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
        elif args.driver == "real":
            driver = RealDriver()
        else:
            driver = SimulatorDriver(args.leader_udid, args.follower_udid)
        result["preflight"]["driver"] = driver.preflight()

        # fixture bind host: LAN runs need a non-loopback origin so ws://
        # to the LAN box is not mixed-content-blocked; loopback otherwise.
        # real devices ALWAYS need the reachable interface — the phones
        # open the fixture over LAN/tailnet even when --url is loopback
        # or tailnet-hosted (the ws target stays the --url origin).
        if route == "lan" or args.driver == "real":
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
        if args.driver == "simulator":
            for cid, spec in clients.items():
                spec.kind = "safari"

        if args.surface == "fixture":
            urls = {cid: fx.fixture_url(cid, spec.role, ws_url, base_url,
                                        host=fx_url_host)
                    for cid, spec in clients.items()}
        else:
            # ui surface: the ?pair_report=<cid> param arms the in-page
            # test-mode state hook (pairSync.js -> /api/v1/pair-report) —
            # the state channel for clients the runner cannot JS-eval
            # (iOS Safari / PWA webclip). Chrome ignores it harmlessly.
            urls = {cid: base_url
                    + ("&" if "?" in base_url else "?")
                    + "pair_report=" + cid + "&pair_reset=1"
                    + "&rpt_gen=" + str(int(time.time() * 1000))
                    for cid in clients}

        driver.launch(clients, urls, fx=fx)
        result["devices"] = driver.devices(clients)

        hellos = {}
        if args.surface == "fixture":
            # real devices: the gate IS the human opening the page —
            # give them manual-step time, not scripted-device time.
            hello_wait = RealDriver.MANUAL_TIMEOUT \
                if args.driver == "real" else 60.0
            for cid in clients:
                hellos[cid] = fx.wait_hello(cid, timeout=hello_wait)
                if not hellos[cid].get("mods"):
                    raise EnvError(
                        f"fixture {cid}: production modules failed to load "
                        f"from {base_url}: {hellos[cid].get('error')}")
            for cid in clients:
                result["devices"][cid]["ua"] = hellos[cid].get("ua")
                result["devices"][cid]["hasRTC"] = hellos[cid].get("hasRTC")
                result["devices"][cid]["standalone"] = \
                    hellos[cid].get("standalone")

        # capabilities -> run plan
        caps = set(driver.caps)
        if args.surface == "ui":
            caps.add(CAP_REAL_UI)
        if (hellos.get("leader", {}).get("hasRTC")
                and hellos.get("follower", {}).get("hasRTC")):
            caps.add(CAP_WEBRTC)
        dev_container = _dev_container(args.dev_env) if args.dev_env else None
        if dev_container:
            caps.add(CAP_SERVER_RESTART)
        result["capabilities"] = sorted(caps)
        if dev_container:
            result["dev_container"] = dev_container
        plan, skipped = resolve_plan(
            scenario_data, driver=args.driver, route=route,
            caps=caps, only=args.scenario, repeat=args.repeat,
            surface=args.surface)
        result["plan"] = [{"name": e["name"], "transport": e["transport"],
                           "run": e["run"]}
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
            run_i = entry["run"]
            sdir = evidence.scenario_dir(
                sc["name"], f"{t}" + (f"-r{run_i}" if run_i > 1 else ""))
            ops = list(sc["ops"])
            if t == "webrtc-dc" and not any(o["op"] == "dc_open" for o in ops):
                # the dc plane can only attach once a pair exists — splice
                # dc_open right after the pair op.
                for i, o in enumerate(ops):
                    if o["op"] == "pair":
                        ops.insert(i + 1, {"op": "dc_open"})
                        break
            # per-scenario client kinds (Safari×Safari / PWA×PWA /
            # Safari×PWA on the simulator driver — spec #86): the page is
            # already up, so a kind change is a surface switch — install +
            # webclip launch for pwa, plain openurl for safari.
            kinds = sc.get("clients") or {}
            print(f"[verify] scenario {sc['name']} [{t}] run#{run_i} ...")
            t0 = time.monotonic()
            try:
                for role, kind in kinds.items():
                    spec = clients[role]
                    if spec.kind != kind:
                        driver.ensure_surface(spec, kind, fx, spec.id)
                ctx = RunCtx(
                    fx=fx, driver=driver, clients=clients,
                    ws_url=ws_url, base_url=base_url,
                    evidence_dir=sdir, surface=args.surface,
                    dev_container=dev_container,
                    # simulator webkit runs measurably slower — scale
                    # every state-poll deadline rather than loosening
                    # per-scenario timeouts one at a time.
                    tscale=2.5 if args.driver == "simulator" else 1.0)
                sres = run_scenario(ctx, {**sc, "ops": ops}, t,
                                    run_index=run_i)
            except EnvError as e:
                # a surface switch that the environment can't do (e.g. a
                # webclip install whose AX labels aren't there) must not
                # abort the remaining plan — record it like a skipped op.
                sres = {"name": sc["name"], "transport": t, "run": run_i,
                        "status": "env_unavailable",
                        "assertions": [{"name": "env", "ok": False,
                                        "detail": str(e)}],
                        "started_at":
                            time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "duration_ms": int((time.monotonic() - t0) * 1000)}
            except UnsupportedError as e:
                sres = {"name": sc["name"], "transport": t, "run": run_i,
                        "status": "unsupported",
                        "assertions": [{"name": "unsupported", "ok": False,
                                        "detail": str(e)}],
                        "started_at":
                            time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "duration_ms": int((time.monotonic() - t0) * 1000)}
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
