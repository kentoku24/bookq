#!/usr/bin/env python3
"""bookq production monitor — probe public endpoints, upsert the incident issue.

Usage:
    prod_monitor.py probe    # run HTTP checks, write verdict JSON
    prod_monitor.py report   # read verdict JSON, manage the `monitor` issue via gh

Environment:
    BOOKQ_MONITOR_BASE_URL   probe target (default https://bookq.mats.dpdns.org)
    BOOKQ_MONITOR_OUT        probe output path (default monitor-result.json)
    BOOKQ_MONITOR_RESULT     report input path (default monitor-result.json)
    BOOKQ_MONITOR_RUN_URL    Actions run URL recorded in the issue body
    BOOKQ_MONITOR_TIMEOUT    per-request timeout seconds (default 10)
    GH_TOKEN / GH_REPO       gh CLI auth (report only)

`report --dry-run` prints the planned gh actions without mutating anything.

Design notes:
- stdlib only (urllib) + `gh` CLI subprocess — runs on GH-hosted runners and
  any dev machine with gh installed. No third-party deps, no secrets needed
  for the probe itself (all checks are unauthenticated endpoints).
- `probe` exits 0 whenever it managed to write the verdict JSON — the verdict
  is the signal, not the exit code. Internal probe errors are emitted as
  verdict=fail / classification=probe_error so a broken monitor still files an
  issue instead of going silently blind.
- The incident issue is the state store: an open issue labelled `monitor`
  means "incident in progress". Signature markers `<!-- monitor-sig:... -->`
  in the body/comments deduplicate repeat detections.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

DEFAULT_BASE_URL = "https://bookq.mats.dpdns.org"
DEFAULT_TIMEOUT = 10.0
MAX_BODY = 1_000_000
MONITOR_LABEL = "monitor"
SIG_MARKER_RE = re.compile(r"<!--\s*monitor-sig:([A-Za-z0-9_:+,.\-]+)\s*-->")
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
COMMENT_THROTTLE_MIN = 60

# HTTP statuses that mean "Cloudflare edge could not get a usable response
# from the origin" — tunnel down, cloudflared dead, or viewer refusing.
CF_ERROR_STATUSES = {520, 521, 522, 523, 525, 526, 530}
# 502/524 also appear for origin failures; only count them as tunnel-layer
# when the body carries Cloudflare's error branding.
CF_BRANDED_STATUSES = {502, 504}
CF_BODY_MARKER = b"cloudflare"

SEVERITY = {
    "auth_bypass": 5,
    "unreachable": 4,
    "cf_tunnel": 3,
    "app_error": 2,
    "contract_break": 1,
}


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------

@dataclass
class HttpResult:
    status: int | None
    headers: dict
    body: bytes
    latency_ms: int
    error: str | None = None


@dataclass
class CheckOutcome:
    ok: bool
    detail: str
    fail_class: str | None = None
    extra: dict | None = None


@dataclass
class CheckResult:
    id: str
    ok: bool
    status: int | None
    latency_ms: int
    detail: str
    fail_class: str | None = None
    cf_ray: str | None = None
    extra: dict | None = None


@dataclass
class Check:
    id: str
    path: str
    evaluate: Callable[[HttpResult], CheckOutcome]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects — /auth/login's 302 is itself the signal."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)
_UA = {"User-Agent": "bookq-prod-monitor/1.0", "Accept": "*/*"}


def fetch(url: str, timeout: float) -> HttpResult:
    req = urllib.request.Request(url, headers=_UA)
    start = time.monotonic()
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            body = resp.read(MAX_BODY)
            return HttpResult(
                status=resp.status,
                headers={k.lower(): v for k, v in resp.headers.items()},
                body=body,
                latency_ms=int((time.monotonic() - start) * 1000),
            )
    except urllib.error.HTTPError as e:
        try:
            body = e.read(MAX_BODY) or b""
        except Exception:
            body = b""
        return HttpResult(
            status=e.code,
            headers={k.lower(): v for k, v in e.headers.items()} if e.headers else {},
            body=body,
            latency_ms=int((time.monotonic() - start) * 1000),
        )
    except Exception as e:
        return HttpResult(
            status=None,
            headers={},
            body=b"",
            latency_ms=int((time.monotonic() - start) * 1000),
            error=f"{type(e).__name__}: {e}",
        )


def classify_status(status: int, body: bytes) -> str:
    """Map an HTTP status (+ body) to a failure layer class."""
    if status in CF_ERROR_STATUSES:
        return "cf_tunnel"
    if status in CF_BRANDED_STATUSES and CF_BODY_MARKER in body[:65536].lower():
        return "cf_tunnel"
    if status >= 500:
        return "app_error"
    return "contract_break"


def _unreachable_or_status(r: HttpResult) -> CheckOutcome | None:
    """Shared preamble: transport error or non-200 status."""
    if r.error:
        return CheckOutcome(False, r.error, "unreachable")
    if r.status != 200:
        return CheckOutcome(False, f"HTTP {r.status}", classify_status(r.status, r.body))
    return None


def _json_body(r: HttpResult):
    try:
        return json.loads(r.body)
    except Exception:
        return None


def eval_health(r: HttpResult) -> CheckOutcome:
    pre = _unreachable_or_status(r)
    if pre:
        return pre
    data = _json_body(r)
    if not isinstance(data, dict) or data.get("status") != "ok":
        return CheckOutcome(False, "unexpected payload", "contract_break")
    return CheckOutcome(True, "ok")


def eval_version(r: HttpResult) -> CheckOutcome:
    pre = _unreachable_or_status(r)
    if pre:
        return pre
    data = _json_body(r)
    if not isinstance(data, dict):
        return CheckOutcome(False, "non-JSON body", "contract_break")
    sha = str(data.get("sha") or "")
    if not SHA_RE.match(sha):
        return CheckOutcome(False, f"missing/invalid sha: {sha[:40]!r}", "contract_break")
    return CheckOutcome(
        True,
        f"sha={sha[:12]}",
        extra={"sha": sha, "build_date": str(data.get("build_date") or "")},
    )


def eval_root(r: HttpResult) -> CheckOutcome:
    pre = _unreachable_or_status(r)
    if pre:
        return pre
    if b"<title>bookq" not in r.body:
        return CheckOutcome(False, "login page marker missing", "contract_break")
    return CheckOutcome(True, "ok")


def eval_auth_login(r: HttpResult) -> CheckOutcome:
    if r.error:
        return CheckOutcome(False, r.error, "unreachable")
    if r.status is not None and 300 <= r.status < 400:
        loc = r.headers.get("location") or ""
        if "accounts.google.com" in loc:
            return CheckOutcome(True, "302 → accounts.google.com")
        return CheckOutcome(False, f"redirect to unexpected host: {loc[:80]}", "contract_break")
    cls = classify_status(r.status or 0, r.body)
    return CheckOutcome(False, f"HTTP {r.status} (expected 3xx)", cls)


def eval_folders_gate(r: HttpResult) -> CheckOutcome:
    """Auth gate must answer exactly 401 unauthenticated.

    200 means the allowlist/auth path is broken — the most severe class.
    """
    if r.error:
        return CheckOutcome(False, r.error, "unreachable")
    if r.status == 401:
        return CheckOutcome(True, "401")
    if r.status == 200:
        return CheckOutcome(False, "auth gate returned 200 without credentials", "auth_bypass")
    return CheckOutcome(False, f"HTTP {r.status} (expected 401)", classify_status(r.status, r.body))


def eval_static_sw(r: HttpResult) -> CheckOutcome:
    pre = _unreachable_or_status(r)
    if pre:
        return pre
    if b"bookq-" not in r.body:
        return CheckOutcome(False, "SW cache-name marker missing", "contract_break")
    return CheckOutcome(True, "ok")


def eval_static_app(r: HttpResult) -> CheckOutcome:
    pre = _unreachable_or_status(r)
    if pre:
        return pre
    if len(r.body) <= 100_000:
        return CheckOutcome(False, f"app.js suspiciously small ({len(r.body)}B)", "contract_break")
    return CheckOutcome(True, f"{len(r.body)}B")


CHECKS = [
    Check("health", "/api/health", eval_health),
    Check("v1_health", "/api/v1/health", eval_health),
    Check("version", "/api/version", eval_version),
    Check("root", "/", eval_root),
    Check("auth_login", "/auth/login", eval_auth_login),
    Check("folders_gate", "/api/folders", eval_folders_gate),
    Check("static_sw", "/static/sw.js", eval_static_sw),
    Check("static_app", "/static/app.js", eval_static_app),
]


def run_probe(base_url: str, timeout: float) -> dict:
    base = base_url.rstrip("/")
    started = datetime.now(timezone.utc)
    outcomes: list[CheckResult] = []
    version_extra = None
    for check in CHECKS:
        r = fetch(base + check.path, timeout)
        try:
            oc = check.evaluate(r)
        except Exception as e:  # evaluator bug must not kill the probe
            oc = CheckOutcome(False, f"evaluator error: {e}", "contract_break")
        cf_ray = r.headers.get("cf-ray")
        outcomes.append(CheckResult(
            id=check.id,
            ok=oc.ok,
            status=r.status,
            latency_ms=r.latency_ms,
            detail=oc.detail,
            fail_class=oc.fail_class,
            cf_ray=cf_ray,
            extra=oc.extra,
        ))
        if check.id == "version" and oc.extra:
            version_extra = oc.extra

    failed = [c for c in outcomes if not c.ok]
    if not failed:
        verdict = "ok"
        classification = "ok"
        signature = "ok"
        summary = f"all {len(outcomes)} checks passed"
    else:
        verdict = "fail"
        classification = max(
            (c.fail_class or "contract_break" for c in failed),
            key=lambda c: SEVERITY.get(c, 0),
        )
        signature = f"{classification}:{','.join(sorted(c.id for c in failed))}"
        ids = ",".join(c.id for c in failed)
        if len(ids) > 120:
            ids = ids[:117] + "..."
        summary = f"{classification}: {len(failed)}/{len(outcomes)} checks failed ({ids})"

    return {
        "schema": 1,
        "ts": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "base_url": base,
        "verdict": verdict,
        "classification": classification,
        "signature": signature,
        "summary": summary,
        "version": version_extra,
        "checks": [
            {
                "id": c.id,
                "ok": c.ok,
                "status": c.status,
                "latency_ms": c.latency_ms,
                "detail": c.detail,
                **({"cf_ray": c.cf_ray} if c.cf_ray else {}),
            }
            for c in outcomes
        ],
        "error": None,
    }


def probe_error_result(exc: BaseException) -> dict:
    """Valid fail-verdict JSON for a monitor-internal error (never go blind)."""
    return {
        "schema": 1,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "base_url": os.environ.get("BOOKQ_MONITOR_BASE_URL", DEFAULT_BASE_URL),
        "verdict": "fail",
        "classification": "probe_error",
        "signature": "probe_error:internal",
        "summary": f"probe_error: monitor internal error — {type(exc).__name__}: {exc}",
        "version": None,
        "checks": [],
        "error": f"{type(exc).__name__}: {exc}",
    }


def cmd_probe(args) -> int:
    out_path = args.out or os.environ.get("BOOKQ_MONITOR_OUT", "monitor-result.json")
    base = os.environ.get("BOOKQ_MONITOR_BASE_URL", DEFAULT_BASE_URL)
    timeout = float(os.environ.get("BOOKQ_MONITOR_TIMEOUT", DEFAULT_TIMEOUT))
    try:
        result = run_probe(base, timeout)
    except Exception as e:
        result = probe_error_result(e)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.stdout:
        print(payload)
    else:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(payload + "\n")
        print(f"wrote {out_path} verdict={result['verdict']} signature={result['signature']}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def gh(args: list[str], dry_run: bool = False, capture: bool = True) -> subprocess.CompletedProcess:
    """Run `gh <args>`; in dry-run mode print instead of executing."""
    if dry_run:
        print(f"[dry-run] gh {' '.join(args)}")
        return subprocess.CompletedProcess(["gh", *args], 0, "", "")
    return subprocess.run(["gh", *args], capture_output=capture, text=True)


def gh_ok(cp: subprocess.CompletedProcess) -> bool:
    return cp.returncode == 0


def git_head() -> str:
    try:
        cp = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
        return cp.stdout.strip() if cp.returncode == 0 else ""
    except Exception:
        return ""


def find_open_issue(dry_run: bool) -> tuple[int | None, bool]:
    """Return (issue_number|None, gh_ok). gh failure must NOT be treated as
    'no open issue' — that would file a duplicate incident."""
    cp = gh([
        "issue", "list", "--label", MONITOR_LABEL, "--state", "open",
        "--limit", "20", "--json", "number,createdAt",
    ], dry_run=dry_run)
    if not gh_ok(cp):
        return None, False
    try:
        issues = json.loads(cp.stdout or "[]")
    except Exception:
        return None, False
    if not issues:
        return None, True
    latest = max(issues, key=lambda i: i.get("createdAt") or "")
    return int(latest["number"]), True


def get_issue_state(number: int, dry_run: bool) -> dict:
    """Return {last_sig, last_activity, created_at} for throttle decisions."""
    cp = gh([
        "issue", "view", str(number), "--json", "createdAt,body,comments",
    ], dry_run=dry_run)
    state = {"last_sig": None, "last_activity": None, "created_at": None}
    if not gh_ok(cp):
        return state
    try:
        data = json.loads(cp.stdout or "{}")
    except Exception:
        return state
    state["created_at"] = data.get("createdAt")

    # Newest marker wins: scan comments newest-first, then the issue body.
    candidates = []
    for c in reversed(data.get("comments") or []):
        author = (c.get("author") or {}).get("login") or ""
        if author.endswith("[bot]") or author == "github-actions[bot]":
            candidates.append((c.get("createdAt"), c.get("body") or ""))
    body = data.get("body") or ""
    m = SIG_MARKER_RE.search(body)
    if m:
        candidates.append((data.get("createdAt"), m.group(0)))

    for ts, text in candidates:
        m = SIG_MARKER_RE.search(text)
        if m:
            state["last_sig"] = m.group(1)
            state["last_activity"] = ts or data.get("createdAt")
            return state
    # No marker anywhere — fall back to issue creation as activity baseline.
    state["last_activity"] = data.get("createdAt")
    return state


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def decide_fail_action(last_sig: str | None, last_activity: str | None,
                       signature: str, now: datetime) -> tuple[str, bool]:
    """Pure decision: returns (action, needs_analysis).

    action: "comment" | "skip"
    """
    sig_changed = last_sig != signature  # None (no marker) counts as changed
    activity = parse_ts(last_activity)
    stale = activity is None or (now - activity).total_seconds() >= COMMENT_THROTTLE_MIN * 60
    if sig_changed or stale:
        return "comment", sig_changed
    return "skip", False


def format_duration(start: str | None, end: datetime) -> str:
    s = parse_ts(start)
    if s is None:
        return "unknown"
    secs = max(0, int((end - s).total_seconds()))
    h, rem = divmod(secs, 3600)
    m = rem // 60
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m"
    return f"{secs}s"


def render_checks_table(checks: list[dict], failed_only: bool = False) -> str:
    rows = []
    for c in checks:
        if failed_only and c.get("ok"):
            continue
        mark = "✅" if c.get("ok") else "❌"
        status = c.get("status") if c.get("status") is not None else "—"
        ms = c.get("latency_ms")
        detail = c.get("detail") or ""
        rows.append(f"| `{c['id']}` | {mark} | {status} | {ms}ms | {detail} |")
    if not rows:
        return "_(no check data)_"
    return "| check | result | HTTP | ms | detail |\n|---|---|---|---|---|\n" + "\n".join(rows)


def render_issue_body(result: dict, run_url: str, repo_head: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ver = result.get("version") or {}
    lines = [
        "## 🚨 Production monitor alert — bookq",
        "",
        "| | |",
        "|---|---|",
        f"| Verdict | **{result['verdict'].upper()}** |",
        f"| Classification | `{result['classification']}` |",
        f"| Signature | `{result['signature']}` |",
        f"| Summary | {result['summary']} |",
        f"| First detected | {result['ts']} (run {run_url}) |",
        f"| Last updated | {now} |",
        "",
        "### Check results",
        "",
        render_checks_table(result.get("checks") or []),
        "",
        "### Context",
        "",
        f"- Deployed version: sha `{ver.get('sha', 'unknown')}` build_date `{ver.get('build_date', 'unknown')}`",
        f"- Repo HEAD: `{repo_head or 'unknown'}`",
        f"- Probe run: {run_url}",
        f"- Base URL: {result.get('base_url')}",
        "",
        "<details><summary>Raw result JSON</summary>",
        "",
        "```json",
        json.dumps(result, ensure_ascii=False, indent=2),
        "```",
        "",
        "</details>",
        "",
        f"<!-- monitor-sig:{result['signature']} -->",
    ]
    return "\n".join(lines)


def render_fail_comment(result: dict, run_url: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"**Still failing** — {now}",
        "",
        f"- Classification: `{result['classification']}`",
        f"- Signature: `{result['signature']}`",
        f"- Summary: {result['summary']}",
        "",
        render_checks_table(result.get("checks") or [], failed_only=True),
        "",
        f"Probe run: {run_url}",
        "",
        f"<!-- monitor-sig:{result['signature']} -->",
    ]
    return "\n".join(lines)


def render_recovery_comment(result: dict, issue_created_at: str | None) -> str:
    now_dt = datetime.now(timezone.utc)
    now = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    duration = format_duration(issue_created_at, now_dt)
    n = len(result.get("checks") or [])
    lines = [
        f"✅ All checks passed at {now} — incident duration ~{duration}.",
        "",
        f"- Checks: {n}/{n} green",
        f"- Last failing signature before recovery: see comments above",
        "",
        "<!-- monitor-sig:ok -->",
    ]
    return "\n".join(lines)


def issue_title(result: dict) -> str:
    title = f"[monitor] bookq prod 障害: {result['summary']}"
    return title[:200]


def emit_outputs(action: str, needs_analysis: bool, issue_number: int | None, summary: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    pairs = {
        "action": action,
        "needs_analysis": "true" if needs_analysis else "false",
        "issue_number": str(issue_number or ""),
        "summary": summary,
    }
    if out:
        with open(out, "a", encoding="utf-8") as f:
            for k, v in pairs.items():
                f.write(f"{k}={v}\n")
    for k, v in pairs.items():
        print(f"{k}={v}")


def cmd_report(args) -> int:
    result_path = args.result or os.environ.get("BOOKQ_MONITOR_RESULT", "monitor-result.json")
    dry_run = args.dry_run
    run_url = os.environ.get("BOOKQ_MONITOR_RUN_URL", "")

    try:
        with open(result_path, encoding="utf-8") as f:
            result = json.load(f)
    except Exception as e:
        print(f"cannot read result JSON {result_path}: {e}", file=sys.stderr)
        return 1

    summary = result.get("summary") or ""
    verdict = result.get("verdict")

    if verdict == "ok":
        number, listed = find_open_issue(dry_run)
        if not listed:
            emit_outputs("error", False, None, summary)
            return 1
        if number is None:
            emit_outputs("none", False, None, summary)
            return 0
        state = get_issue_state(number, dry_run)
        body = render_recovery_comment(result, state.get("created_at"))
        if dry_run:
            print(f"[dry-run] would close issue #{number} with comment:\n{body}")
        else:
            cp = gh(["issue", "close", str(number), "--comment", body])
            if not gh_ok(cp):
                print(f"gh issue close failed: {cp.stderr}", file=sys.stderr)
                emit_outputs("error", False, number, summary)
                return 1
        emit_outputs("recover", False, number, summary)
        return 0

    # verdict == "fail"
    ensure = gh([
        "label", "create", MONITOR_LABEL,
        "--color", "B60205",
        "--description", "Production monitor incident (auto-filed by prod-monitor workflow)",
    ], dry_run=dry_run)
    if not dry_run and not gh_ok(ensure):
        # Label likely already exists — gh errors on duplicates; verify instead of failing.
        chk = gh(["label", "list", "--search", MONITOR_LABEL, "--json", "name"], dry_run=False)
        try:
            names = {l.get("name") for l in json.loads(chk.stdout or "[]")}
        except Exception:
            names = set()
        if MONITOR_LABEL not in names:
            print(f"cannot create label {MONITOR_LABEL}: {ensure.stderr}", file=sys.stderr)
            emit_outputs("error", False, None, summary)
            return 1

    number, listed = find_open_issue(dry_run)
    if not listed:
        emit_outputs("error", False, None, summary)
        return 1
    if number is None:
        repo_head = git_head()
        body = render_issue_body(result, run_url, repo_head)
        title = issue_title(result)
        if dry_run:
            print(f"[dry-run] would create issue: {title}\nlabels: {MONITOR_LABEL}\n--- body ---\n{body}")
            emit_outputs("create", True, None, summary)
            return 0
        cp = gh([
            "issue", "create",
            "--title", title,
            "--label", MONITOR_LABEL,
            "--body", body,
        ])
        if not gh_ok(cp):
            print(f"gh issue create failed: {cp.stderr}", file=sys.stderr)
            emit_outputs("error", False, None, summary)
            return 1
        m = re.search(r"/issues/(\d+)", cp.stdout or "")
        new_num = int(m.group(1)) if m else None
        emit_outputs("create", True, new_num, summary)
        return 0

    # Open incident exists — comment only on signature change or throttle expiry.
    state = get_issue_state(number, dry_run)
    now = datetime.now(timezone.utc)
    action, needs_analysis = decide_fail_action(
        state.get("last_sig"), state.get("last_activity"),
        result.get("signature") or "", now,
    )
    if action == "comment":
        body = render_fail_comment(result, run_url)
        if dry_run:
            print(f"[dry-run] would comment on issue #{number}:\n{body}")
        else:
            cp = gh(["issue", "comment", str(number), "--body", body])
            if not gh_ok(cp):
                print(f"gh issue comment failed: {cp.stderr}", file=sys.stderr)
                emit_outputs("error", needs_analysis, number, summary)
                return 1
    emit_outputs(action, needs_analysis, number, summary)
    return 0


# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="bookq production monitor")
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("probe", help="run HTTP checks and write verdict JSON")
    pp.add_argument("--out", help="output JSON path (default: $BOOKQ_MONITOR_OUT or monitor-result.json)")
    pp.add_argument("--stdout", action="store_true", help="print JSON to stdout instead of file")
    pp.set_defaults(func=cmd_probe)

    rp = sub.add_parser("report", help="upsert the monitor incident issue from a verdict JSON")
    rp.add_argument("--result", help="input JSON path (default: $BOOKQ_MONITOR_RESULT or monitor-result.json)")
    rp.add_argument("--dry-run", action="store_true", help="print planned gh actions without executing")
    rp.set_defaults(func=cmd_report)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
