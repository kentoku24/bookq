#!/usr/bin/env python3
"""CI bottleneck metrics — collect GitHub Actions timings, upsert a tracking issue.

Usage:
    ci_metrics.py collect   # fetch runs/jobs/steps via gh, write ci-metrics.json
    ci_metrics.py report    # read ci-metrics.json, upsert the `ci-metrics` issue

Environment:
    GH_TOKEN / GH_REPO      gh CLI auth + repo (collect and report)
    CI_METRICS_OUT          collect output path (default ci-metrics.json)
    CI_METRICS_RESULT       report input path (default ci-metrics.json)
    CI_METRICS_RUN_URL      Actions run URL recorded in the issue body
    GITHUB_RUN_ID           fallback for the state marker run id

`report --dry-run` prints the planned gh actions without mutating anything.

Design notes (mirrors scripts/prod_monitor.py):
- stdlib only + `gh` CLI subprocess — runs on GH-hosted runners and any
  dev machine with gh installed.
- `collect` exits 0 whenever it managed to write the metrics JSON.
- The tracking issue is the state store: a single open issue labelled
  `ci-metrics` holds the latest 7d-vs-prior-7d table. History is carried by
  the analyze job's diagnosis comments, so `report` rewrites the body on
  every run instead of commenting.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

SCHEMA = 1
LABEL = "ci-metrics"
MARKER_RE = re.compile(r"<!--\s*ci-metrics:run:([A-Za-z0-9_.\-]+)\s*-->")

# Week-over-week comparison windows (days).
CURRENT_DAYS = 7
BASELINE_DAYS = 7

# Signal thresholds (see decide_analysis).
MIN_N = 5
DURATION_REGRESSION_RATIO = 1.5
MACOS_QUEUE_P90_S = 15 * 60
FAILURE_RATE_THRESHOLD = 0.10

# Heavy jobs get per-step timings; light jobs only contribute run/job times
# (API call budget: one extra call per heavy job).
HEAVY_JOB_SUBSTRINGS = ("ios-test", "safari-gate", "safari", "pytest", "rust", "viewer")


# ---------------------------------------------------------------------------
# pure helpers (pytest targets)
# ---------------------------------------------------------------------------

def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def seconds_between(start: str | None, end: str | None) -> float | None:
    """Return (end - start) in seconds, or None when either side is missing."""
    s, e = parse_ts(start), parse_ts(end)
    if s is None or e is None:
        return None
    return max(0.0, (e - s).total_seconds())


def percentile(sorted_vals: list[float], q: float) -> float | None:
    """Nearest-rank percentile over an already-sorted list (q in 0..100)."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    import math

    rank = math.ceil(q / 100 * len(sorted_vals))
    rank = min(max(rank, 1), len(sorted_vals))
    return float(sorted_vals[rank - 1])


def is_macos(labels: list[str] | None) -> bool:
    for lb in labels or []:
        low = str(lb).lower()
        if "macos" in low or "xcode" in low:
            return True
    return False


def is_heavy_job(job_name: str) -> bool:
    low = (job_name or "").lower()
    return any(sub in low for sub in HEAVY_JOB_SUBSTRINGS)


def job_key(workflow: str, job_name: str) -> str:
    return f"{workflow} / {job_name}"


def fmt_dur(sec: float | None) -> str:
    if sec is None:
        return "—"
    sec = float(sec)
    if sec < 60:
        return f"{sec:.0f}s"
    m = sec / 60
    if m < 60:
        return f"{m:.1f}m"
    return f"{m / 60:.1f}h"


def fmt_pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


# ---------------------------------------------------------------------------
# aggregation (pure)
# ---------------------------------------------------------------------------

def aggregate_jobs(samples: list[dict]) -> dict[str, dict]:
    """Aggregate job samples by `workflow / job` key.

    Samples with null started_at/completed_at are excluded from
    duration/queue stats (never counted as 0). Failure rate counts only
    verdict-bearing conclusions: `cancelled`/`skipped` jobs and jobs whose
    whole run was cancelled are tracked separately (`n_cancelled` /
    `n_skipped`) — a superseded run is churn, not a test failure, and an
    `always()` aggregate that "fails" on cancelled needs is the same
    artifact (#651).
    """
    by_key: dict[str, dict] = {}
    for s in samples:
        key = job_key(s.get("workflow") or "?", s.get("job_name") or "?")
        b = by_key.setdefault(key, {
            "workflow": s.get("workflow") or "?",
            "job_name": s.get("job_name") or "?",
            "macos": False,
            "durations": [],
            "queues": [],
            "n_conclusion": 0,
            "n_failed": 0,
            "n_cancelled": 0,
            "n_skipped": 0,
            "run_ids": set(),
        })
        if s.get("is_macos"):
            b["macos"] = True
        dur = seconds_between(s.get("started_at"), s.get("completed_at"))
        if dur is not None:
            b["durations"].append(dur)
        que = seconds_between(s.get("created_at"), s.get("started_at"))
        if que is not None:
            b["queues"].append(que)
        concl = s.get("conclusion")
        if concl == "skipped":
            b["n_skipped"] += 1
        elif concl == "cancelled" or (
            concl in ("failure", "timed_out")
            and s.get("run_conclusion") == "cancelled"
        ):
            b["n_cancelled"] += 1
        elif concl:
            b["n_conclusion"] += 1
            if concl in ("failure", "timed_out"):
                b["n_failed"] += 1
        if s.get("run_id") is not None:
            b["run_ids"].add(s.get("run_id"))
    out: dict[str, dict] = {}
    for key, b in by_key.items():
        ds = sorted(b["durations"])
        qs = sorted(b["queues"])
        n_fail_denom = b["n_conclusion"]
        out[key] = {
            "workflow": b["workflow"],
            "job_name": b["job_name"],
            "macos": b["macos"],
            "n": len(ds),
            "duration_p50": percentile(ds, 50),
            "duration_p90": percentile(ds, 90),
            "queue_p50": percentile(qs, 50),
            "queue_p90": percentile(qs, 90),
            "failure_rate": (b["n_failed"] / n_fail_denom) if n_fail_denom else None,
            "n_failed": b["n_failed"],
            "n_conclusion": n_fail_denom,
            "n_cancelled": b["n_cancelled"],
            "n_skipped": b["n_skipped"],
            "n_runs": len(b["run_ids"]),
        }
    return out


def aggregate_steps(samples: list[dict]) -> dict[str, dict]:
    """Aggregate step samples by `workflow / job / step` key (duration p50)."""
    by_key: dict[str, list[float]] = {}
    meta: dict[str, dict] = {}
    for s in samples:
        key = f"{s.get('workflow') or '?'} / {s.get('job_name') or '?'} / {s.get('step_name') or '?'}"
        dur = seconds_between(s.get("started_at"), s.get("completed_at"))
        if dur is None:
            continue
        by_key.setdefault(key, []).append(dur)
        meta.setdefault(key, {
            "workflow": s.get("workflow"), "job_name": s.get("job_name"),
            "step_name": s.get("step_name"),
        })
    out: dict[str, dict] = {}
    for key, vals in by_key.items():
        vals.sort()
        out[key] = {**meta[key], "n": len(vals), "duration_p50": percentile(vals, 50)}
    return out


def split_periods(samples: list[dict], now: datetime) -> tuple[list[dict], list[dict]]:
    """Split job samples into current (last 7d) vs baseline (prior 7d).

    Uses started_at, falling back to created_at. Samples without any
    timestamp are dropped from both periods (never Bucketed as 0).
    """
    cur_cut = now - timedelta(days=CURRENT_DAYS)
    base_cut = now - timedelta(days=CURRENT_DAYS + BASELINE_DAYS)
    cur, base = [], []
    for s in samples:
        ts = parse_ts(s.get("started_at")) or parse_ts(s.get("created_at"))
        if ts is None:
            continue
        if ts >= cur_cut:
            cur.append(s)
        elif ts >= base_cut:
            base.append(s)
    return cur, base


def decide_analysis(current: dict[str, dict],
                    baseline: dict[str, dict]) -> tuple[bool, list[str]]:
    """Pure gate: return (needs_analysis, reasons).

    Signals (current-period stats only, n>=MIN_N):
    - job duration p90 > 1.5x baseline p90
    - macOS job queue p90 > 15min (absolute)
    - job failure rate > 10% (absolute)
    - new workflow/job key vs baseline
    A missing/empty baseline disables regression + new-job checks but
    absolute thresholds (queue/failure) still apply.
    """
    reasons: list[str] = []
    baseline_missing = not baseline
    if baseline_missing:
        reasons.append("info: baseline empty (first run?) — regression/new-job checks skipped")
    for key, c in sorted(current.items()):
        n = c.get("n") or 0
        if n < MIN_N:
            continue
        b = baseline.get(key)
        if b is not None and (b.get("n") or 0) >= MIN_N:
            base_p90 = b.get("duration_p90")
            cur_p90 = c.get("duration_p90")
            if (base_p90 is not None and cur_p90 is not None
                    and base_p90 > 0
                    and cur_p90 > base_p90 * DURATION_REGRESSION_RATIO):
                reasons.append(
                    f"signal: duration regression {key}: "
                    f"p90 {fmt_dur(cur_p90)} > 1.5x baseline {fmt_dur(base_p90)} (n={n})")
        elif b is None and not baseline_missing:
            reasons.append(f"signal: new workflow/job {key} (n={n})")
        fr = c.get("failure_rate")
        if fr is not None and (c.get("n_conclusion") or 0) >= MIN_N and fr > FAILURE_RATE_THRESHOLD:
            reasons.append(
                f"signal: failure rate {key}: {fmt_pct(fr)} "
                f"({c.get('n_failed')}/{c.get('n_conclusion')}, n={n})")
        if c.get("macos"):
            qp90 = c.get("queue_p90")
            if qp90 is not None and qp90 > MACOS_QUEUE_P90_S:
                reasons.append(
                    f"signal: macOS queue spike {key}: "
                    f"queue p90 {fmt_dur(qp90)} > 15m (n={n})")
    signals = [r for r in reasons if r.startswith("signal:")]
    return (len(signals) > 0, reasons)


# ---------------------------------------------------------------------------
# gh plumbing (mirrors prod_monitor.py — gh failure must never look like
# "no open issue", or a duplicate tracking issue gets filed)
# ---------------------------------------------------------------------------

def gh(args: list[str], dry_run: bool = False) -> subprocess.CompletedProcess:
    if dry_run:
        print(f"[dry-run] gh {' '.join(args)}")
        return subprocess.CompletedProcess(["gh", *args], 0, "", "")
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def gh_ok(cp: subprocess.CompletedProcess) -> bool:
    return cp.returncode == 0


def gh_api(path: str) -> tuple[dict | list | None, bool]:
    """GET a gh api path. Returns (parsed_json|None, ok)."""
    cp = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    if cp.returncode != 0:
        return None, False
    try:
        return json.loads(cp.stdout or "null"), True
    except Exception:
        return None, False


def resolve_repo(cli_repo: str | None) -> str:
    if cli_repo:
        return cli_repo
    env = os.environ.get("GH_REPO", "")
    if env:
        return env
    cp = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner",
                         "--jq", ".nameWithOwner"],
                        capture_output=True, text=True)
    if cp.returncode == 0 and cp.stdout.strip():
        return cp.stdout.strip()
    return ""


def find_open_issue(dry_run: bool) -> tuple[int | None, bool]:
    cp = gh([
        "issue", "list", "--label", LABEL, "--state", "open",
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


def ensure_label(dry_run: bool) -> bool:
    cp = gh(["label", "create", LABEL, "--color", "0E8A16",
             "--description", "CI bottleneck metrics tracking (auto-filed by ci-metrics workflow)"],
            dry_run=dry_run)
    if dry_run or gh_ok(cp):
        return True
    chk = gh(["label", "list", "--search", LABEL, "--json", "name"], dry_run=False)
    try:
        names = {row.get("name") for row in json.loads(chk.stdout or "[]")}
    except Exception:
        names = set()
    return LABEL in names


# ---------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------

def fetch_paginated(base_path: str, max_pages: int, per_page: int = 100) -> tuple[list, bool]:
    """Fetch `base_path` pages 1..max_pages. Returns (items, truncated)."""
    items: list = []
    sep = "&" if "?" in base_path else "?"
    for page in range(1, max_pages + 1):
        data, ok = gh_api(f"{base_path}{sep}per_page={per_page}&page={page}")
        if not ok or data is None:
            break
        batch = data.get("workflow_runs", data.get("jobs", [])) if isinstance(data, dict) else data
        if not batch:
            break
        items.extend(batch)
        if len(batch) < per_page:
            break
    else:
        return items, True
    return items, False


def cmd_collect(args) -> int:
    repo = resolve_repo(args.repo)
    if not repo:
        print("cannot determine repo (pass --repo or set GH_REPO)", file=sys.stderr)
        return 1
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=args.days)
    since_s = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    runs_path = f"repos/{repo}/actions/runs?created=>={since_s}"
    runs, runs_truncated = fetch_paginated(runs_path, args.max_pages)
    print(f"fetched {len(runs)} runs since {since_s}", file=sys.stderr)

    job_samples: list[dict] = []
    step_samples: list[dict] = []
    heavy_fetched = 0
    heavy_skipped = False

    def fetch_run_jobs(run_id):
        data, ok = gh_api(f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100")
        if not ok or not isinstance(data, dict):
            return []
        return data.get("jobs") or []

    # One jobs-list call per run dominates collect time (~2k runs/14d) —
    # fetch them concurrently, order preserved, so the page cap can cover
    # the full window without blowing the job timeout (#651).
    if args.workers > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            jobs_per_run = list(
                pool.map(lambda r: fetch_run_jobs(r.get("id")), runs))
    else:
        jobs_per_run = [fetch_run_jobs(r.get("id")) for r in runs]

    for run, jobs in zip(runs, jobs_per_run):
        run_id = run.get("id")
        workflow = run.get("name") or run.get("path") or "?"
        run_created = run.get("created_at")
        run_url = run.get("html_url") or ""
        run_conclusion = run.get("conclusion")
        for job in jobs:
            name = job.get("name") or "?"
            labels = job.get("labels") or []
            sample = {
                "run_id": run_id,
                "run_created_at": run_created,
                "run_url": run_url,
                "run_conclusion": run_conclusion,
                "workflow": workflow,
                "job_name": name,
                "labels": labels,
                "is_macos": is_macos(labels),
                "is_heavy": is_heavy_job(name),
                "status": job.get("status"),
                "conclusion": job.get("conclusion"),
                "created_at": job.get("created_at"),
                "started_at": job.get("started_at"),
                "completed_at": job.get("completed_at"),
                "html_url": job.get("html_url") or run_url,
            }
            job_samples.append(sample)
            if not is_heavy_job(name):
                continue
            if heavy_fetched >= args.max_step_jobs:
                heavy_skipped = True
                continue
            detail, ok = gh_api(f"repos/{repo}/actions/jobs/{job.get('id')}")
            if not ok or not isinstance(detail, dict):
                continue
            heavy_fetched += 1
            for st in detail.get("steps") or []:
                step_samples.append({
                    "workflow": workflow,
                    "job_name": name,
                    "step_name": st.get("name") or "?",
                    "started_at": st.get("started_at"),
                    "completed_at": st.get("completed_at"),
                })

    payload = {
        "schema": SCHEMA,
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo": repo,
        "window_days": args.days,
        "since": since_s,
        "runs_fetched": len(runs),
        "runs_truncated": runs_truncated,
        "heavy_steps_truncated": heavy_skipped,
        "jobs": job_samples,
        "steps": step_samples,
    }
    out = args.out or os.environ.get("CI_METRICS_OUT", "ci-metrics.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"wrote {out} runs={len(runs)} jobs={len(job_samples)} "
          f"steps={len(step_samples)} heavy_fetched={heavy_fetched}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def render_table(current: dict[str, dict], baseline: dict[str, dict]) -> str:
    keys = sorted(set(current) | set(baseline))
    if not keys:
        return "_(no job data in window)_"
    rows = []
    for key in keys:
        c, b = current.get(key), baseline.get(key)
        n_c = c.get("n") if c else 0
        n_b = b.get("n") if b else 0
        reg = ""
        if (c and b and (b.get("n") or 0) >= MIN_N and (c.get("n") or 0) >= MIN_N
                and b.get("duration_p90") and c.get("duration_p90")
                and c["duration_p90"] > b["duration_p90"] * DURATION_REGRESSION_RATIO):
            reg = " 🔺regression"
        elif c and not b and (c.get("n") or 0) >= MIN_N:
            reg = " 🆕new"
        mac = " 🍎" if (c or b or {}).get("macos") else ""
        rows.append(
            f"| `{key}`{mac} | {n_c} | {fmt_dur(c.get('duration_p50') if c else None)} | "
            f"{fmt_dur(c.get('duration_p90') if c else None)} | "
            f"{fmt_dur(c.get('queue_p50') if c else None)} | "
            f"{fmt_dur(c.get('queue_p90') if c else None)} | "
            f"{fmt_pct(c.get('failure_rate') if c else None)} | "
            f"{n_b} | {fmt_dur(b.get('duration_p90') if b else None)} | "
            f"{fmt_pct(b.get('failure_rate') if b else None)} |{reg} |")
    return ("| job | n (7d) | dur p50 | dur p90 | queue p50 | queue p90 | fail | "
            "n (prev7d) | base p90 | base fail | Notes |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|\n" + "\n".join(rows))


def render_hotspot(steps: dict[str, dict], limit: int = 15) -> str:
    if not steps:
        return "_(no heavy-job step data — increase collect window or step cap)_"
    top = sorted(steps.items(),
                 key=lambda kv: kv[1].get("duration_p50") or 0, reverse=True)[:limit]
    rows = [f"| `{k}` | {v['n']} | {fmt_dur(v.get('duration_p50'))} |" for k, v in top]
    return ("| step | n | dur p50 |\n|---|---|---|\n" + "\n".join(rows))


def render_issue_body(metrics: dict, current: dict[str, dict],
                      baseline: dict[str, dict], reasons: list[str],
                      run_url: str, run_id: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    repo = metrics.get("repo", "")
    signals = [r for r in reasons if r.startswith("signal:")]
    infos = [r for r in reasons if not r.startswith("signal:")]
    verdict = "🔴 signals detected" if signals else "🟢 no signals"
    lines = [
        "## 📊 CI bottleneck metrics — bookq",
        "",
        "| | |",
        "|---|---|",
        f"| Verdict | **{verdict}** |",
        f"| Window | last {CURRENT_DAYS}d vs prior {BASELINE_DAYS}d "
        f"(collected {metrics.get('ts')}, window_days={metrics.get('window_days')}) |",
        f"| Runs fetched | {metrics.get('runs_fetched')} |",
        f"| Last updated | {now} (run {run_url}) |",
        "",
    ]
    if signals:
        lines += ["### Signals", "", "".join(f"- {s}\n" for s in signals), ""]
    else:
        lines += ["### Signals", "", "_none — all jobs within thresholds_", "", ]
    if infos:
        lines += ["### Notes", "", "".join(f"- {s}\n" for s in infos), ""]
    if not baseline:
        lines += ["> ⚠️ No baseline (first run?) — absolute values only, "
                  "regression comparison unavailable.", ""]
    if metrics.get("runs_truncated"):
        lines += ["> ⚠️ Run list hit the --max-pages cap — the job windows "
                  "below may be incomplete (oldest runs dropped).", ""]
    lines += [
        "### Jobs: last 7d vs prior 7d",
        "",
        render_table(current, baseline),
        "",
        "🍎 = macOS runner job (queue p90 > 15m is a cost/pressure signal).",
        "",
        "### Step hotspot (heavy jobs, current window)",
        "",
        render_hotspot(aggregate_steps(
            [s for s in metrics.get("steps") or []])),
        "",
        "### Context",
        "",
        f"- Repo: {repo}",
        f"- Metrics run: {run_url}",
        f"- ci-metrics.json attached as workflow artifact of that run",
        "",
        f"<!-- ci-metrics:run:{run_id} -->",
    ]
    return "\n".join(lines)


def issue_title() -> str:
    return "[ci-metrics] CI bottleneck tracking"


def emit_outputs(action: str, needs_analysis: bool,
                 issue_number: int | None, summary: str) -> None:
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
    metrics_path = args.result or os.environ.get("CI_METRICS_RESULT", "ci-metrics.json")
    dry_run = args.dry_run
    run_url = os.environ.get("CI_METRICS_RUN_URL", "")
    if not run_url:
        server = os.environ.get("GITHUB_SERVER_URL", "")
        repo_env = os.environ.get("GITHUB_REPOSITORY", "")
        run_id_env = os.environ.get("GITHUB_RUN_ID", "")
        if server and repo_env and run_id_env:
            run_url = f"{server}/{repo_env}/actions/runs/{run_id_env}"
    run_id = (os.environ.get("CI_METRICS_RUN_ID")
              or os.environ.get("GITHUB_RUN_ID") or "local")

    try:
        with open(metrics_path, encoding="utf-8") as f:
            metrics = json.load(f)
    except Exception as e:
        print(f"cannot read metrics JSON {metrics_path}: {e}", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    cur_samples, base_samples = split_periods(metrics.get("jobs") or [], now)
    current = aggregate_jobs(cur_samples)
    baseline = aggregate_jobs(base_samples)
    needs, reasons = decide_analysis(current, baseline)
    signals = [r for r in reasons if r.startswith("signal:")]
    summary = "; ".join(signals[:3]) if signals else (
        f"no signals ({len(current)} jobs in window)")

    if not ensure_label(dry_run):
        print(f"cannot ensure label {LABEL}", file=sys.stderr)
        emit_outputs("error", False, None, summary)
        return 1

    number, listed = find_open_issue(dry_run)
    if not listed:
        emit_outputs("error", False, None, summary)
        return 1
    body = render_issue_body(metrics, current, baseline, reasons, run_url, run_id)
    if number is None:
        title = issue_title()
        if dry_run:
            print(f"[dry-run] would create issue: {title}\nlabels: {LABEL}\n--- body ---\n{body}")
            emit_outputs("create", needs, None, summary)
            return 0
        cp = gh(["issue", "create", "--title", title, "--label", LABEL, "--body", body])
        if not gh_ok(cp):
            print(f"gh issue create failed: {cp.stderr}", file=sys.stderr)
            emit_outputs("error", False, None, summary)
            return 1
        m = re.search(r"/issues/(\d+)", cp.stdout or "")
        new_num = int(m.group(1)) if m else None
        emit_outputs("create", needs, new_num, summary)
        return 0

    # Single tracking issue: rewrite the body every run (history lives in
    # analyze comments). needs_analysis gates the analyze job only.
    if dry_run:
        print(f"[dry-run] would edit issue #{number} body:\n{body}")
    else:
        cp = gh(["issue", "edit", str(number), "--body", body])
        if not gh_ok(cp):
            print(f"gh issue edit failed: {cp.stderr}", file=sys.stderr)
            emit_outputs("error", needs, number, summary)
            return 1
    emit_outputs("update", needs, number, summary)
    return 0


# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="CI bottleneck metrics (collect + report)")
    sub = p.add_subparsers(dest="cmd", required=True)

    cp_ = sub.add_parser("collect", help="fetch Actions timings and write metrics JSON")
    cp_.add_argument("--repo", help="owner/repo (default: $GH_REPO or gh repo view)")
    cp_.add_argument("--days", type=int, default=14,
                     help="lookback window in days (default 14 = 7d current + 7d baseline)")
    cp_.add_argument("--out", help="output JSON path (default: $CI_METRICS_OUT or ci-metrics.json)")
    cp_.add_argument("--max-pages", type=int, default=30,
                     help="max run-list pages (100 runs each); 30 covers "
                          "~3000 runs — the repo does ~2500/14d, so 10 "
                          "silently truncated the baseline window (#651)")
    cp_.add_argument("--max-step-jobs", type=int, default=200,
                     help="max heavy jobs to fetch steps for")
    cp_.add_argument("--workers", type=int, default=8,
                     help="parallel jobs-list fetches (1 = serial)")
    cp_.set_defaults(func=cmd_collect)

    rp = sub.add_parser("report", help="upsert the ci-metrics tracking issue")
    rp.add_argument("--result", help="input JSON path (default: $CI_METRICS_RESULT or ci-metrics.json)")
    rp.add_argument("--dry-run", action="store_true",
                    help="print planned gh actions without executing")
    rp.set_defaults(func=cmd_report)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
