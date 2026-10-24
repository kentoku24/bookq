"""S10 (#85) — shared core for the two-client dual-reader verification runner.

Pure stdlib: no playwright / websockets / PIL at import time so the unit tests
exercise every code path here without a browser or a device. The pieces:

- exit-code model (0 pass / 1 assert / 2 env / 3 unsupported)
- scenario file schema + validation + run-plan resolution
  (driver applicability, route class, transport expansion, capability gating)
- result-JSON builder with the spec-mandated required keys
- evidence writer + self-check (verify.py inspects its own required files)
- URL classification, secret redaction for traces/evidence
- synthetic-work spread-table builder (delegates to app.reader_sync.model)
- deterministic marker palette + PNG region check (PIL imported lazily)
"""
from __future__ import annotations

import colorsys
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

# ── exit codes (spec: 完了条件) ───────────────────────────────────────
EXIT_PASS = 0          # every scenario in the plan ran and passed
EXIT_ASSERT = 1        # at least one scenario assertion failed
EXIT_ENV = 2           # environment unavailable (device/dep/URL)
EXIT_UNSUPPORTED = 3   # an unsupported scenario/op/surface was requested

SCENARIO_STATUSES = ("pass", "fail", "env_unavailable", "unsupported")

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS_PATH = Path(__file__).resolve().parent / "scenarios.json"
EVIDENCE_ROOT = REPO_ROOT / ".omo" / "evidence" / "reader-sync"

# ops the runner understands — the single scenario-declaration contract both
# drivers interpret (spec: 同一 scenarios.json を両 driver が解釈する).
KNOWN_OPS = frozenset({
    "pair",             # CREATE/JOIN/APPROVE over the real /ws/reader-sync
    "open_work",        # OPEN_WORK -> manifest -> READY -> reading markers
    "turn",             # TURN through the real sequencer, actor-initiated
    "goto",             # GOTO to a named page
    "swap_sides",       # SWAP_SIDES leaderSide flip
    "set_offset",       # SET_OFFSET 0/1 (offset変更)
    "concurrent",       # fire several sub-ops "at once" (競合シナリオ)
    "burst",            # N rapid-fire turns then converge (連打)
    "rtt",              # client->server wire RTT probe (SET_ACTIVITY ack)
    "latency",          # intent->committed-apply latency distribution
    "expect",           # poll state for expected phase/spread/marker/…
    "dc_open",          # real WebRTC DataChannel via server-mediated signaling
    "dc_close",         # drop DC, TRANSPORT_STATE down, server-ws fallback
    "dc_restart",       # re-signal a fresh DataChannel mid-session
    "disconnect",       # hard-drop the actor's sync WS
    "resume",           # reconnect + RESUME within the window
    "resume_fail",      # RESUME with a bad/expired-token -> expect reject
    "fault",            # adapter-level fault injection (scoped to own session)
    "image_fault",      # client-side image delay/404/off render fault
    "suspend",          # SET_ACTIVITY suspended (lock/background equivalent)
    "activate",         # SET_ACTIVITY active again
    "reload",           # real page reload -> credential resume path
    "bfcache",          # pagehide->pageshow lifecycle round-trip
    "server_restart",   # restart the target env's viewer container
    "bystander_check",  # runner-side second session proves fault isolation
    "background",       # device input: send the app to background (sim)
    "foreground",       # device input: return the app to foreground (sim)
    "device_input",     # device input: tap/text/swipe/pinch/rotate (sim)
    "tap_element",      # tap a named fixture control (sim AX / dom click)
    "pwa_install",      # scripted Add-to-Home-Screen (sim, best effort)
    "ui_input",         # real-UI input path (chrome --surface ui)
    "open_url",         # navigate a ui-surface client to a work
    "wait",             # deterministic dwell between ops (fault windows)
    "screenshot",       # capture both screens + marker pixel check
    "leave",            # LEAVE -> END on both sides
})

FAULT_TYPES = frozenset({
    "ws_delay",        # delay every outbound sync frame by ms
    "ws_dup",          # duplicate every outbound sync frame
    "ws_drop",         # drop every outbound app frame (heartbeat kept alive)
    "sig_delay",       # delay only SIGNAL (signaling) frames
    "suppress_ice",    # ICE-establishment failure: drop outbound ice candidates
    "stale_base",      # rewrite next sequenced op's baseRevision back N
    "dup_next",        # send the next sequenced request twice (same requestId)
    "in_drop",         # drop N inbound frames of a type (e.g. MANIFEST)
    "in_corrupt",      # corrupt N inbound frames of a type (manifest digest)
    "in_inject",       # inject crafted inbound frames (e.g. v:2 STATE)
    "clear",           # remove all injected faults
})

# capabilities a run can advertise; scenarios gate on these via "requires".
CAP_WEBRTC = "webrtc"             # RTCPeerConnection + host-candidate path
CAP_DEVICE_INPUTS = "device_inputs"  # simctl/idb-driven tap/text/swipe/...
CAP_PWA = "pwa"                   # scripted Add-to-Home-Screen
CAP_BYSTANDER = "ws_bystander"    # runner-side raw-WS second session
CAP_REAL_UI = "ui"                # real app pairing UI surface (post-S09)
CAP_SERVER_RESTART = "server_restart"  # docker-manageable target env

ROUTE_CLASSES = ("tailnet", "lan", "loopback")

# fields that must never land in evidence (spec: コード・復帰credential・
# live folder名は収集しない).
_SECRET_KEYS = re.compile(
    r"(code|resumetoken|token|secret|password|credential|cookie)", re.I)
_FOLDER_KEYS = re.compile(
    r"^(folder|chapterfolder|path|fullpath|workfolder|parentfolder)$", re.I)
_FX_NAME = re.compile(r"^fx[:\-_ ]", re.I)


class ScenarioError(ValueError):
    """Invalid scenario declaration — maps to exit 3."""


class UnsupportedError(ScenarioError):
    """A scenario/op/surface the runner or driver cannot support — exit 3."""


class EnvError(RuntimeError):
    """Environment unavailable — maps to exit 2."""


class AssertError(AssertionError):
    """Scenario assertion failure — maps to exit 1."""


# ── URL classification ───────────────────────────────────────────────


def classify_url(url: str) -> str:
    """tailnet (https *.ts.net / any https) | loopback | lan."""
    u = (url or "").strip().lower()
    if u.startswith("https://"):
        return "tailnet"
    m = re.match(r"^http://([^/:]+)", u)
    host = m.group(1) if m else ""
    if host in ("127.0.0.1", "localhost", "::1", "[::1]"):
        return "loopback"
    return "lan"


def ws_url_for(base_url: str) -> str:
    """http(s) base -> ws(s)://<base>/ws/reader-sync."""
    u = base_url.rstrip("/")
    if u.startswith("https://"):
        return "wss://" + u[len("https://"):] + "/ws/reader-sync"
    if u.startswith("http://"):
        return "ws://" + u[len("http://"):] + "/ws/reader-sync"
    raise ScenarioError(f"unsupported --url scheme: {base_url!r}")


def redacted_url(url: str) -> str:
    """Scheme + host kind only — never the tailnet/userinfo specifics."""
    cls = classify_url(url)
    scheme = "https" if url.startswith("https") else "http"
    return f"{scheme}://<{cls}>"


# ── redaction ────────────────────────────────────────────────────────


def redact(obj):
    """Deep-copy with secret keys masked and non-fx folder names hashed.

    Pairing codes, resume tokens, credentials and real folder names never
    reach evidence — folder-ish keys (folder/chapterFolder/path/fullPath)
    collapse to a stable hash, while page ``name`` values stay readable
    (they are the marker labels the asserts reason about)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _SECRET_KEYS.search(str(k)):
                out[k] = "<redacted>"
            elif _FOLDER_KEYS.search(str(k)) and isinstance(v, str):
                out[k] = safe_name(v)
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    return obj


def safe_name(name) -> str:
    """A page/folder name is evidence-safe only when synthetic (``fx:``);
    anything else collapses to a stable hash."""
    if not isinstance(name, str):
        return "<n/a>"
    if _FX_NAME.match(name):
        return name
    return "sha256:" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:10]


# ── scenario loading / validation ────────────────────────────────────


def load_scenarios(path: Path | None = None) -> dict:
    p = Path(path) if path else SCENARIOS_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ScenarioError(f"cannot load scenarios file {p}: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("scenarios"), list):
        raise ScenarioError("scenarios file must be an object with a scenarios list")
    seen = set()
    for i, sc in enumerate(data["scenarios"]):
        _validate_scenario(sc, i, seen)
    return data


def _validate_scenario(sc: dict, i: int, seen: set) -> None:
    if not isinstance(sc, dict):
        raise ScenarioError(f"scenario[{i}] must be an object")
    name = sc.get("name")
    if not isinstance(name, str) or not name:
        raise ScenarioError(f"scenario[{i}] needs a non-empty name")
    if name in seen:
        raise ScenarioError(f"duplicate scenario name: {name}")
    seen.add(name)
    for key, allowed in (
        ("drivers", ("chrome", "simulator")),
        ("route", ("any",) + ROUTE_CLASSES),
        ("surface", ("fixture", "ui")),
        ("transports", ("server-ws", "webrtc-dc")),
    ):
        v = sc.get(key)
        if v is None:
            continue
        vals = v if isinstance(v, list) else [v]
        bad = [x for x in vals if x not in allowed]
        if bad:
            raise ScenarioError(f"scenario {name}: bad {key} value(s) {bad}")
    ops = sc.get("ops")
    if not isinstance(ops, list) or not ops:
        raise ScenarioError(f"scenario {name}: needs a non-empty ops list")
    for j, op in enumerate(ops):
        if not isinstance(op, dict) or op.get("op") not in KNOWN_OPS:
            raise ScenarioError(
                f"scenario {name} op[{j}]: unknown op {op!r}")
        _validate_op(name, j, op)
    _validate_work(name, sc.get("work"))
    _validate_clients(name, sc.get("clients"))
    rep = sc.get("repeat", 1)
    if not isinstance(rep, int) or isinstance(rep, bool) or rep < 1:
        raise ScenarioError(f"scenario {name}: repeat must be an int >= 1")
    for req in sc.get("requires") or []:
        if req not in KNOWN_CAPS:
            raise ScenarioError(
                f"scenario {name}: unknown capability {req!r}")


def _validate_work(name: str, work) -> None:
    """A scenario work is either:
    - the name of a synthetic work in tests/fixtures/reader_sync_tree.py
    - an inline ``{chapters:[{folder:'fx:…',pages:[…]}], offset}`` declaration
    - an ENV work ``{folder, page:{folder,name}, offset?}`` opened through
      the real OPEN_WORK path against the target env's own tree. Env names
      are synthetic dev-fixture folders, never user data — still, ops only
      echo page names (never folders) into evidence.
    """
    if work is None:
        return
    if isinstance(work, str):
        if not work.startswith("fx"):
            raise ScenarioError(
                f"scenario {name}: named work {work!r} is not an fx fixture")
        return
    if not isinstance(work, dict):
        raise ScenarioError(f"scenario {name}: work must be a name or object")
    if isinstance(work.get("chapters"), list):
        if work.get("offset", 0) not in (0, 1):
            raise ScenarioError(f"scenario {name}: work.offset must be 0 or 1")
        for ch in work["chapters"]:
            folder = ch.get("folder", "")
            if not isinstance(folder, str) or not _FX_NAME.match(folder):
                raise ScenarioError(
                    f"scenario {name}: chapter folder must be a synthetic "
                    f"fx: name, got {folder!r}")
        return
    # env work form
    if work.get("discover") is True:
        mp = work.get("minPages", 6)
        if not isinstance(mp, int) or mp < 2:
            raise ScenarioError(
                f"scenario {name}: discover work needs minPages >= 2")
        if work.get("offset", 0) not in (0, 1):
            raise ScenarioError(f"scenario {name}: work.offset must be 0 or 1")
        return
    if not isinstance(work.get("folder"), str):
        raise ScenarioError(
            f"scenario {name}: env work needs a folder string")
    if work.get("offset", 0) not in (0, 1):
        raise ScenarioError(f"scenario {name}: work.offset must be 0 or 1")
    page = work.get("page")
    if page is not None and not (
            isinstance(page, dict) and isinstance(page.get("name"), str)
            and isinstance(page.get("folder"), str)):
        raise ScenarioError(
            f"scenario {name}: env work page must be {{folder,name}}")


def _validate_clients(name: str, clients) -> None:
    """Per-role client kind: 'safari'|'pwa' (simulator web clip) — the
    Safari×Safari / PWA×PWA / Safari×PWA surface matrix of #86."""
    if clients is None:
        return
    if not isinstance(clients, dict):
        raise ScenarioError(f"scenario {name}: clients must be an object")
    for role, kind in clients.items():
        if role not in ("leader", "follower"):
            raise ScenarioError(
                f"scenario {name}: clients key must be leader|follower")
        if kind not in ("safari", "pwa"):
            raise ScenarioError(
                f"scenario {name}: client kind must be safari|pwa")


def _validate_op(name: str, j: int, op: dict) -> None:
    """Op-parameter shape checks — bad declarations fail at parse (exit 3),
    never mid-run."""
    t = op["op"]
    if t == "turn":
        if op.get("delta", 1) not in (1, -1):
            raise ScenarioError(
                f"scenario {name} op[{j}]: turn delta must be ±1, "
                f"got {op.get('delta')!r}")
        if op.get("actor", "leader") not in ("leader", "follower"):
            raise ScenarioError(
                f"scenario {name} op[{j}]: bad actor {op.get('actor')!r}")
    elif t == "goto":
        if not isinstance(op.get("page"), str) and not (
                isinstance(op.get("index"), int)):
            raise ScenarioError(
                f"scenario {name} op[{j}]: goto needs page name or int index")
    elif t == "set_offset":
        if op.get("offset") not in (0, 1):
            raise ScenarioError(
                f"scenario {name} op[{j}]: set_offset needs offset 0|1")
    elif t == "concurrent":
        subs = op.get("ops")
        if not isinstance(subs, list) or not subs:
            raise ScenarioError(
                f"scenario {name} op[{j}]: concurrent needs a non-empty ops")
        for k, sub in enumerate(subs):
            if not isinstance(sub, dict) or \
                    sub.get("op") not in ("turn", "goto", "swap_sides",
                                          "open_work"):
                raise ScenarioError(
                    f"scenario {name} op[{j}].ops[{k}]: concurrent sub-op "
                    f"must be turn|goto|swap_sides|open_work")
            _validate_op(name, f"{j}.{k}", sub)
    elif t == "burst":
        if not isinstance(op.get("count"), int) or op["count"] < 1:
            raise ScenarioError(
                f"scenario {name} op[{j}]: burst needs int count >= 1")
    elif t == "pair":
        if op.get("via") not in (None, "taps"):
            raise ScenarioError(
                f"scenario {name} op[{j}]: pair via must be 'taps' or absent")
    elif t == "latency":
        if op.get("samples", 8) < 1:
            raise ScenarioError(
                f"scenario {name} op[{j}]: latency needs samples >= 1")
    elif t == "fault":
        if op.get("type") not in FAULT_TYPES:
            raise ScenarioError(
                f"scenario {name} op[{j}]: unknown fault {op.get('type')!r}")
    elif t == "image_fault":
        if op.get("mode") not in ("ok", "404", "delay", "off"):
            raise ScenarioError(
                f"scenario {name} op[{j}]: image_fault mode "
                "ok|404|delay|off")
    elif t in ("suspend", "activate"):
        pass
    elif t == "device_input":
        if op.get("kind") not in (
                "tap", "text", "swipe", "pinch", "rotate", "describe"):
            raise ScenarioError(
                f"scenario {name} op[{j}]: unknown device_input kind")
    elif t == "ui_input":
        if op.get("kind") not in (
                "key", "tap_zone", "slider", "overview", "swipe",
                "spread_shift", "open_work", "browse"):
            raise ScenarioError(
                f"scenario {name} op[{j}]: unknown ui_input kind")
    elif t == "tap_element":
        if not isinstance(op.get("target"), str):
            raise ScenarioError(
                f"scenario {name} op[{j}]: tap_element needs a target label")
    elif t == "open_work":
        _validate_work(name, op.get("work"))
    elif t == "open_url":
        if not isinstance(op.get("path"), str):
            raise ScenarioError(
                f"scenario {name} op[{j}]: open_url needs a path")
    elif t == "expect":
        if not isinstance(op.get("actor"), str):
            raise ScenarioError(
                f"scenario {name} op[{j}]: expect needs an actor")
    elif t == "wait":
        if not isinstance(op.get("ms"), (int, float)) or op["ms"] < 0:
            raise ScenarioError(
                f"scenario {name} op[{j}]: wait needs ms >= 0")
    for req in op.get("requires") or []:
        if req not in KNOWN_CAPS:
            raise ScenarioError(
                f"scenario {name} op[{j}]: unknown capability {req!r}")


KNOWN_CAPS = frozenset({
    "webrtc", "device_inputs", "pwa", "ws_bystander", "ui", "server_restart",
})


# ── run-plan resolution ─────────────────────────────────────────────


def resolve_plan(scenario_data: dict, *, driver: str, route: str,
                 caps: set, only: list | None = None,
                 repeat: int | None = None,
                 surface: str = "fixture") -> tuple[list, list]:
    """Expand the declaration into a run plan.

    Returns (plan, skipped). ``plan`` entries are
    ``{name, transport, scenario, run}`` where ``run`` is the 1-based
    repeat index (spec: 同一固定操作列の連続runを全てPASSさせる — the same
    fixed op sequence, back to back, every repetition passing);
    ``skipped`` entries are ``{name, transport, status, reason}``
    placeholders recorded into the result JSON so a skipped-but-requested
    scenario is never silent.
    """
    only_set = set(only or [])
    plan, skipped = [], []
    for sc in scenario_data["scenarios"]:
        name = sc["name"]
        if only_set and name not in only_set:
            continue
        drivers = sc.get("drivers") or ["chrome", "simulator"]
        if driver not in drivers:
            if only_set:
                skipped.append({"name": name, "transport": None,
                                "status": "unsupported",
                                "reason": f"driver {driver} not in {drivers}"})
            continue
        sc_route = sc.get("route", "any")
        if sc_route != "any" and sc_route != route:
            if only_set:
                skipped.append({"name": name, "transport": None,
                                "status": "env_unavailable",
                                "reason": f"route {sc_route} != {route}"})
            continue
        # ``surface`` declares the surfaces a scenario can run on —
        # undeclared means fixture-only (fixture cmds are the control
        # plane; only scenarios written against ui_input/open_url/expect
        # run on the real-UI surface).
        sc_surfaces = sc.get("surface") or ["fixture"]
        allowed_s = sc_surfaces if isinstance(sc_surfaces, list) \
            else [sc_surfaces]
        if surface not in allowed_s:
            # structural mismatch — filtered silently unless the
            # scenario was explicitly requested by name.
            if only_set:
                skipped.append({"name": name, "transport": None,
                                "status": "unsupported",
                                "reason": f"surface {allowed_s} does "
                                          f"not include {surface}"})
            continue
        requires = set(sc.get("requires") or [])
        missing = requires - caps
        if missing:
            status = "unsupported" \
                if (missing & _hard_requires(driver, surface)) \
                else "env_unavailable"
            skipped.append({"name": name, "transport": None, "status": status,
                            "reason": f"missing capabilities: {sorted(missing)}"})
            continue
        transports = sc.get("transports") or ["server-ws"]
        picked = []
        for t in transports:
            if t == "webrtc-dc" and CAP_WEBRTC not in caps:
                continue
            picked.append(t)
        if not picked:
            skipped.append({"name": name, "transport": None,
                            "status": "env_unavailable",
                            "reason": "no usable transport "
                                      f"(declared {transports})"})
            continue
        reps = repeat if repeat is not None else int(sc.get("repeat", 1))
        for t in picked:
            for run in range(1, reps + 1):
                plan.append({"name": name, "transport": t, "scenario": sc,
                             "run": run})
    if only_set:
        found = {e["name"] for e in plan} | {e["name"] for e in skipped}
        for n in sorted(only_set - found):
            skipped.append({"name": n, "transport": None, "status": "unsupported",
                            "reason": "no such scenario"})
    return plan, skipped


def _hard_requires(driver: str, surface: str = "fixture") -> set:
    """Requires this driver/surface combination can never satisfy —
    a structural mismatch (``unsupported``), not a transient environment
    gap. ``ui``-capable scenarios only exist on the real-UI surface, so
    requesting one under the fixture surface is unsupported, not
    env_unavailable."""
    hard = set()
    if driver == "chrome":
        hard |= {CAP_DEVICE_INPUTS, CAP_PWA}
    if surface != "ui":
        hard.add(CAP_REAL_UI)
    return hard


# ── exit-code mapping ────────────────────────────────────────────────


def exit_code_for(statuses) -> int:
    """fail(1) > unsupported(3) > env(2) > pass(0).

    An assertion failure is the most actionable signal, then a bad request,
    then an unavailable environment. A run with zero scenarios is a
    declaration problem -> 3.
    """
    sts = list(statuses)
    if not sts:
        return EXIT_UNSUPPORTED
    if any(s == "fail" for s in sts):
        return EXIT_ASSERT
    if any(s == "unsupported" for s in sts):
        return EXIT_UNSUPPORTED
    if any(s == "env_unavailable" for s in sts):
        return EXIT_ENV
    return EXIT_PASS


# ── result JSON / evidence ──────────────────────────────────────────

RESULT_REQUIRED_KEYS = ("run_id", "driver", "scenarios",
                        "build_sha", "devices", "os")


def new_run_id(driver: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    rand = hashlib.sha256(os.urandom(8)).hexdigest()[:6]
    return f"{stamp}-{driver}-{rand}"


def os_info() -> dict:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }


def runner_git_sha(repo_root: Path = REPO_ROOT) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo_root),
            capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def target_git_sha(base_url: str, timeout: float = 5.0) -> str:
    """The deployed build sha reported by the target env's /api/version."""
    try:
        with urllib.request.urlopen(
                base_url.rstrip("/") + "/api/version", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return str(data.get("sha") or "")
    except Exception:
        return ""


def check_url_reachable(base_url: str, timeout: float = 8.0) -> dict:
    try:
        with urllib.request.urlopen(
                base_url.rstrip("/") + "/api/health", timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8"))
        return {"ok": body.get("status") == "ok"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


class EvidenceWriter:
    """Writes ``.omo/evidence/reader-sync/<run_id>/`` atomically-ish."""

    def __init__(self, run_id: str, root: Path = EVIDENCE_ROOT):
        self.run_id = run_id
        self.dir = Path(root) / run_id
        self.dir.mkdir(parents=True, exist_ok=True)

    def scenario_dir(self, name: str, transport: str | None) -> Path:
        slug = re.sub(r"[^a-z0-9_\-]+", "-", name.lower()).strip("-")
        d = self.dir / slug / (transport or "none")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_json(self, path: Path, obj) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
        return path

    def write_bytes(self, path: Path, data: bytes) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def write_trace(self, path: Path, events: list) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(redact(ev), ensure_ascii=False) + "\n")
        return path


def result_required_ok(result: dict) -> tuple[bool, list]:
    """Spec self-check: result JSON must carry the required keys and every
    scenario entry must carry name/transport/status/assertions."""
    missing = [k for k in RESULT_REQUIRED_KEYS if k not in result]
    scs = result.get("scenarios")
    if not isinstance(scs, list):
        missing.append("scenarios(list)")
    else:
        for i, s in enumerate(scs):
            for k in ("name", "transport", "status", "assertions"):
                if not isinstance(s, dict) or k not in s:
                    missing.append(f"scenarios[{i}].{k}")
    return (not missing, missing)


def self_check(evidence: EvidenceWriter, result: dict,
               expected_files: list) -> dict:
    """verify.py's own post-run inspection of required artifacts — plus
    the spec's 実folder名 leak scan over every json/jsonl artifact."""
    ok, missing_keys = result_required_ok(result)
    missing_files = [str(p) for p in expected_files
                     if not (evidence.dir / p).exists()]
    leaks = leak_scan([evidence.dir])
    return {"passed": ok and not missing_files and not leaks,
            "missing_keys": missing_keys,
            "missing_files": missing_files,
            "leaks": leaks}


# ── synthetic work -> shared spread table ───────────────────────────


def _model():
    sys.path.insert(0, str(REPO_ROOT))
    from app.reader_sync import model  # noqa: PLC0415
    return model


def build_spread_table(work: dict) -> list[dict]:
    """The real S01 table builder applied to a synthetic declaration —
    the same pairing logic the production server will use."""
    model = _model()
    chapters = []
    for ch in work["chapters"]:
        pages = [{"folder": ch["folder"], "name": n} for n in ch["pages"]]
        chapters.append({"folder": ch["folder"], "pages": pages})
    plan = model.build_spread_plan(chapters, int(work.get("offset", 0)))
    return [model.serialize_spread(s) for s in plan]


def expected_marker(table: list[dict], index: int, role: str,
                    leader_side: str) -> dict:
    """Which page marker a device should show for a committed index.

    Returns {"side": "right"|"left", "ref": PageRef|None} — mirrors
    pairModel.projectSpread exactly (kept here in python for runner-side
    asserts; the JS copy lives in the shipped production module)."""
    if index < 0 or index >= len(table):
        return {"side": None, "ref": None}
    spread = table[index]
    if role == "leader":
        side = leader_side
    else:
        side = "left" if leader_side == "right" else "right"
    ref = spread.get(side)
    return {"side": side, "ref": ref}


def spread_table_index(table: list[dict], spread_id: str) -> int:
    """Index of a spread id inside EITHER table shape: a real manifest's
    spreadTable ({id,right:{folder,name},left}) or the fixture-reported
    summary ({id,r:name,l:name})."""
    for i, s in enumerate(table or []):
        if isinstance(s, dict) and s.get("id") == spread_id:
            return i
    return -1


def expected_page(table: list[dict], spread_id: str, role: str,
                  leader_side: str) -> dict:
    """Expected {side, page} for a member at ``spread_id`` — normalized over
    both table shapes so runner asserts work against the server-built
    manifest as reported by the client."""
    idx = spread_table_index(table, spread_id)
    if idx < 0:
        return {"side": None, "page": None}
    s = table[idx]
    side = leader_side if role == "leader" else (
        "left" if leader_side == "right" else "right")
    ref = s.get(side)
    page = None
    if isinstance(ref, dict):
        page = ref.get("name")
    elif ref is not None:
        page = ref
    # summary shape uses r/l keys
    if page is None:
        key = "r" if side == "right" else "l"
        page = s.get(key)
    return {"side": side, "page": page, "index": idx}


# ── deterministic marker palette ────────────────────────────────────


def _fnv1a(text: str) -> int:
    h = 0x811C9DC5
    for b in text.encode("utf-8"):
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h


def marker_rgb(page_name: str) -> tuple:
    """Synthetic-page color — identical formula in fixture.html (JS)."""
    h = (_fnv1a(page_name) % 360) / 360.0
    r, g, b = colorsys.hls_to_rgb(h, 0.55, 0.68)
    return (round(r * 255), round(g * 255), round(b * 255))


BLANK_RGB = (34, 34, 44)          # the blank-side marker color
BLANK_LABEL = "BLANK"


def png_region_rgb(png_path: Path, box=(0.30, 0.35, 0.70, 0.65)) -> tuple:
    """Median RGB of a relative box (l,t,r,b fractions) — PIL lazily.

    Median, not mean: the marker region contains the page-name text and
    its shadow; a mean would drag every channel. The uniform marker
    background is always the majority pixel, so the median IS the marker
    color regardless of glyph coverage."""
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError as e:
        raise EnvError("PIL unavailable — install pillow for pixel checks") from e
    import statistics
    with Image.open(png_path) as im:
        im = im.convert("RGB")
        w, h = im.size
        crop = im.crop((int(w * box[0]), int(h * box[1]),
                        int(w * box[2]), int(h * box[3])))
        px = list(crop.getdata())
    if not px:
        return (0, 0, 0)
    return tuple(int(statistics.median(c[i] for c in px)) for i in range(3))


def rgb_close(a: tuple, b: tuple, tol: int = 24) -> bool:
    return all(abs(int(x) - int(y)) <= tol for x, y in zip(a, b))


def png_marker_fraction(png_path: Path, want_rgb: tuple,
                        tol: int = 14) -> float:
    """Fraction of pixels within ``tol`` of ``want_rgb``.

    Orientation/layout-proof alternative to a fixed sample box: in
    landscape Safari chrome plus the fixture's own bars push the marker
    below the portrait-tuned region, so instead assert the marker color
    actually dominates a real screen region wherever it sits. Tight tol
    keeps the blank marker (34,34,44) distinct from the page bg (17,17,17).
    """
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError as e:
        raise EnvError("PIL unavailable — install pillow for pixel checks") from e
    with Image.open(png_path) as im:
        im = im.convert("RGB")
        px = list(im.getdata())
    if not px:
        return 0.0
    wr, wg, wb = want_rgb
    hit = sum(1 for (r, g, b) in px
              if abs(r - wr) <= tol and abs(g - wg) <= tol
              and abs(b - wb) <= tol)
    return hit / len(px)


def percentile(sorted_vals: list, p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def rtt_stats(samples: list) -> dict:
    s = sorted(float(x) for x in samples)
    return {"samples": [round(x, 2) for x in s], "n": len(s),
            "min": round(s[0], 2) if s else None,
            "max": round(s[-1], 2) if s else None,
            "p50": round(percentile(s, 0.50), 2) if s else None,
            "p95": round(percentile(s, 0.95), 2) if s else None}


# ── evidence leak scan (spec: 実folder名が成果物に現れないこと) ──────


_LEAK_KEYS = re.compile(
    r'"(?:folder|chapterFolder|path|fullPath|workFolder)"\s*:\s*"([^"]+)"')
_LEAK_SAFE = re.compile(
    r"^(fx[:\-_ ].*|sha256:[0-9a-f]+|<.*>|)$", re.I)


def leak_scan(paths, allowed_folders: set | None = None) -> list:
    """Pattern-scan evidence files for real folder names.

    Walks ``paths`` (files and/or directories), extracts every
    ``"folder"/"chapterFolder"/"path"`` JSON string value, and returns the
    violations — values that are neither fx:-synthetic, already hashed
    (sha256:…), empty, placeholder (<…>), nor explicitly allowed synthetic
    dev-fixture names in ``allowed_folders``.
    """
    allowed = set(allowed_folders or ())
    violations = []
    files = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            files.extend(x for x in p.rglob("*") if x.is_file()
                         and x.suffix in (".json", ".jsonl", ".txt", ".log"))
        elif p.is_file():
            files.append(p)
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _LEAK_KEYS.finditer(text):
            val = m.group(1)
            if _LEAK_SAFE.match(val) or val in allowed:
                continue
            violations.append(f"{f.name}: {val!r}")
    return violations
