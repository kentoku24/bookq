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
    "open_work",        # load a synthetic work + render initial spread markers
    "turn",             # spread turn, leader-sequenced, actor-initiated
    "goto",             # absolute spread move
    "swap_sides",       # leaderSide flip
    "signal",           # SIGNAL relay round-trip (both directions)
    "rtt",              # device-to-device RTT probe over the active data plane
    "dc_open",          # real WebRTC DataChannel via server-mediated signaling
    "dc_close",         # drop DC, TRANSPORT_STATE down, server-ws fallback
    "disconnect",       # hard-drop the actor's sync WS
    "resume",           # reconnect + RESUME within the window
    "resume_fail",      # RESUME with a bad/expired-token -> expect reject
    "fault",            # adapter-level fault injection (scoped to own session)
    "bystander_check",  # runner-side second session proves fault isolation
    "background",       # device input: send the app to background (sim)
    "foreground",       # device input: return the app to foreground (sim)
    "device_input",     # device input: tap/text/swipe/pinch/rotate (sim)
    "tap_element",      # tap a named fixture control (sim AX / dom click)
    "pwa_install",      # scripted Add-to-Home-Screen (sim, best effort)
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
    "clear",           # remove all injected faults
})

# capabilities a run can advertise; scenarios gate on these via "requires".
CAP_WEBRTC = "webrtc"             # RTCPeerConnection + host-candidate path
CAP_DEVICE_INPUTS = "device_inputs"  # simctl/idb-driven tap/text/swipe/...
CAP_PWA = "pwa"                   # scripted Add-to-Home-Screen
CAP_BYSTANDER = "ws_bystander"    # runner-side raw-WS second session
CAP_REAL_UI = "ui"                # real app pairing UI surface (post-S09)

ROUTE_CLASSES = ("tailnet", "lan", "loopback")

# fields that must never land in evidence (spec: コード・復帰credential・
# live folder名は収集しない).
_SECRET_KEYS = re.compile(
    r"(code|resumetoken|token|secret|password|credential|cookie)", re.I)
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
    reach evidence. Synthetic fixture page names (``fx:…``) stay readable —
    they carry no user data by construction.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _SECRET_KEYS.search(str(k)):
                out[k] = "<redacted>"
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
    for req in sc.get("requires") or []:
        if req not in KNOWN_CAPS:
            raise ScenarioError(
                f"scenario {name}: unknown capability {req!r}")


def _validate_work(name: str, work) -> None:
    """A scenario work is either an inline declaration or the name of a
    synthetic work in tests/fixtures/reader_sync_tree.py."""
    if work is None:
        return
    if isinstance(work, str):
        if not work.startswith("fx"):
            raise ScenarioError(
                f"scenario {name}: named work {work!r} is not an fx fixture")
        return
    if not isinstance(work, dict) or not isinstance(work.get("chapters"), list):
        raise ScenarioError(f"scenario {name}: work needs a chapters list")
    if work.get("offset", 0) not in (0, 1):
        raise ScenarioError(f"scenario {name}: work.offset must be 0 or 1")
    for ch in work["chapters"]:
        folder = ch.get("folder", "")
        if not isinstance(folder, str) or not _FX_NAME.match(folder):
            raise ScenarioError(
                f"scenario {name}: chapter folder must be a synthetic "
                f"fx: name, got {folder!r}")


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
        if not isinstance(op.get("index"), int):
            raise ScenarioError(f"scenario {name} op[{j}]: goto needs int index")
    elif t == "fault":
        if op.get("type") not in FAULT_TYPES:
            raise ScenarioError(
                f"scenario {name} op[{j}]: unknown fault {op.get('type')!r}")
    elif t == "device_input":
        if op.get("kind") not in (
                "tap", "text", "swipe", "pinch", "rotate", "describe"):
            raise ScenarioError(
                f"scenario {name} op[{j}]: unknown device_input kind")
    elif t == "tap_element":
        if not isinstance(op.get("target"), str):
            raise ScenarioError(
                f"scenario {name} op[{j}]: tap_element needs a target label")
    elif t == "open_work":
        _validate_work(name, op.get("work"))
    elif t == "wait":
        if not isinstance(op.get("ms"), (int, float)) or op["ms"] < 0:
            raise ScenarioError(
                f"scenario {name} op[{j}]: wait needs ms >= 0")
    for req in op.get("requires") or []:
        if req not in KNOWN_CAPS:
            raise ScenarioError(
                f"scenario {name} op[{j}]: unknown capability {req!r}")


KNOWN_CAPS = frozenset({
    "webrtc", "device_inputs", "pwa", "ws_bystander", "ui",
})


# ── run-plan resolution ─────────────────────────────────────────────


def resolve_plan(scenario_data: dict, *, driver: str, route: str,
                 caps: set, only: list | None = None) -> tuple[list, list]:
    """Expand the declaration into a run plan.

    Returns (plan, skipped). ``plan`` entries are
    ``{name, transport, scenario}``; ``skipped`` entries are
    ``{name, transport, status, reason}`` placeholders recorded into the
    result JSON so a skipped-but-requested scenario is never silent.
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
        requires = set(sc.get("requires") or [])
        missing = requires - caps
        if missing:
            status = "unsupported" if (missing & _hard_requires(driver)) \
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
        for t in picked:
            plan.append({"name": name, "transport": t, "scenario": sc})
    if only_set:
        found = {e["name"] for e in plan} | {e["name"] for e in skipped}
        for n in sorted(only_set - found):
            skipped.append({"name": n, "transport": None, "status": "unsupported",
                            "reason": "no such scenario"})
    return plan, skipped


def _hard_requires(driver: str) -> set:
    """Requires a driver class can never satisfy (driver mismatch, not env)."""
    if driver == "chrome":
        return {CAP_DEVICE_INPUTS, CAP_PWA}
    return set()


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
    """verify.py's own post-run inspection of required artifacts."""
    ok, missing_keys = result_required_ok(result)
    missing_files = [str(p) for p in expected_files
                     if not (evidence.dir / p).exists()]
    return {"passed": ok and not missing_files,
            "missing_keys": missing_keys,
            "missing_files": missing_files}


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
