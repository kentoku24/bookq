"""Unit tests for scripts/ci_metrics.py — thresholds, aggregation, rendering.

Pure-function coverage only: no network, no gh, no server fixture needed.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ci_metrics as cm  # noqa: E402


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _sample(workflow="ci", job="pytest", *, start, dur_s=600, queue_s=30,
            conclusion="success", labels=None, run_id=1):
    s = parse_start = start
    created = _iso(s - timedelta(seconds=queue_s))
    started = _iso(s)
    completed = _iso(s + timedelta(seconds=dur_s))
    return {
        "run_id": run_id,
        "workflow": workflow,
        "job_name": job,
        "labels": labels or [],
        "is_macos": cm.is_macos(labels or []),
        "conclusion": conclusion,
        "created_at": created,
        "started_at": started,
        "completed_at": completed,
    }


NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def test_percentile_basic():
    assert cm.percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.0
    assert cm.percentile([1.0, 2.0, 3.0, 4.0], 90) == 4.0
    assert cm.percentile([], 90) is None
    assert cm.percentile([7.0], 90) == 7.0


def test_seconds_between_null_excluded():
    assert cm.seconds_between(None, _iso(NOW)) is None
    assert cm.seconds_between(_iso(NOW), None) is None
    assert cm.seconds_between("bogus", _iso(NOW)) is None
    assert cm.seconds_between(_iso(NOW), _iso(NOW + timedelta(seconds=5))) == 5.0


def test_is_macos_labels():
    assert cm.is_macos(["macos-15", "xcode-27"])
    assert cm.is_macos(["macOS-latest"])
    assert not cm.is_macos(["ubuntu-latest"])
    assert not cm.is_macos(None)
    assert not cm.is_macos([])


def test_is_heavy_job():
    assert cm.is_heavy_job("ios-test")
    assert cm.is_heavy_job("safari-gate (0)")
    assert cm.is_heavy_job("pytest-rs-unit")
    assert not cm.is_heavy_job("lint")
    assert not cm.is_heavy_job("")


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------

def test_aggregate_null_times_excluded_not_zero():
    s_ok = _sample(start=NOW - timedelta(days=1), dur_s=100, queue_s=10)
    s_null = dict(s_ok)
    s_null["started_at"] = None
    s_null["completed_at"] = None
    agg = cm.aggregate_jobs([s_ok, s_null])
    key = "ci / pytest"
    assert agg[key]["n"] == 1
    assert agg[key]["duration_p50"] == pytest.approx(100.0)


def test_aggregate_failure_rate_and_macos():
    samples = [
        _sample(start=NOW - timedelta(days=1), conclusion="success", labels=["macos-15"]),
        _sample(start=NOW - timedelta(days=1), conclusion="failure", labels=["macos-15"]),
    ]
    agg = cm.aggregate_jobs(samples)
    st = agg["ci / pytest"]
    assert st["failure_rate"] == pytest.approx(0.5)
    assert st["macos"] is True
    assert st["n_conclusion"] == 2


def test_aggregate_skipped_cancelled_excluded_from_failure_rate():
    # cancelled/skipped carry no verdict — they must not count as failures
    # nor dilute the denominator (ios.yml cancel-in-progress regression).
    samples = [
        _sample(start=NOW - timedelta(days=1), conclusion="success"),
        _sample(start=NOW - timedelta(days=1), conclusion="failure"),
        _sample(start=NOW - timedelta(days=1), conclusion="cancelled"),
        _sample(start=NOW - timedelta(days=1), conclusion="skipped"),
        _sample(start=NOW - timedelta(days=1), conclusion="timed_out"),
    ]
    agg = cm.aggregate_jobs(samples)
    st = agg["ci / pytest"]
    assert st["n_conclusion"] == 3          # success + failure + timed_out
    assert st["n_failed"] == 2              # failure + timed_out
    assert st["failure_rate"] == pytest.approx(2 / 3)


def test_aggregate_steps_null_excluded():
    steps = [
        {"workflow": "ci", "job_name": "ios-test", "step_name": "Run iOS tests",
         "started_at": _iso(NOW), "completed_at": _iso(NOW + timedelta(seconds=900))},
        {"workflow": "ci", "job_name": "ios-test", "step_name": "Run iOS tests",
         "started_at": None, "completed_at": None},
    ]
    agg = cm.aggregate_steps(steps)
    assert agg["ci / ios-test / Run iOS tests"]["n"] == 1
    assert agg["ci / ios-test / Run iOS tests"]["duration_p50"] == pytest.approx(900.0)


def test_split_periods_boundary():
    cur = _sample(start=NOW - timedelta(days=1))
    edge = _sample(start=NOW - timedelta(days=7) + timedelta(seconds=1))
    base = _sample(start=NOW - timedelta(days=10))
    old = _sample(start=NOW - timedelta(days=30))
    timeless = dict(cur)
    timeless["started_at"] = timeless["created_at"] = None
    c, b = cm.split_periods([cur, edge, base, old, timeless], NOW)
    assert len(c) == 2 and len(b) == 1


# ---------------------------------------------------------------------------
# decide_analysis
# ---------------------------------------------------------------------------

def _stats(*, n=10, dur_p90=600.0, queue_p90=60.0, failure_rate=0.0,
           macos=False, n_conclusion=10, n_failed=0):
    return {"n": n, "duration_p50": dur_p90 / 2, "duration_p90": dur_p90,
            "queue_p50": queue_p90 / 2, "queue_p90": queue_p90,
            "failure_rate": failure_rate, "n_conclusion": n_conclusion,
            "n_failed": n_failed, "macos": macos, "n_runs": n}


def test_decide_no_signal_when_equal():
    cur = {"ci / pytest": _stats()}
    base = {"ci / pytest": _stats()}
    needs, reasons = cm.decide_analysis(cur, base)
    assert needs is False
    assert not [r for r in reasons if r.startswith("signal:")]


def test_decide_duration_regression_above_ratio():
    cur = {"ci / ios": _stats(n=6, dur_p90=960.0)}
    base = {"ci / ios": _stats(n=6, dur_p90=600.0)}
    needs, reasons = cm.decide_analysis(cur, base)
    assert needs is True
    assert any("regression" in r for r in reasons)


def test_decide_duration_boundary_exactly_1_5x_is_not_signal():
    cur = {"ci / ios": _stats(n=6, dur_p90=900.0)}
    base = {"ci / ios": _stats(n=6, dur_p90=600.0)}
    needs, _ = cm.decide_analysis(cur, base)
    assert needs is False


def test_decide_small_n_excluded():
    cur = {"ci / ios": _stats(n=4, dur_p90=5000.0)}
    base = {"ci / ios": _stats(n=10, dur_p90=100.0)}
    needs, _ = cm.decide_analysis(cur, base)
    assert needs is False


def test_decide_small_baseline_n_skips_regression():
    cur = {"ci / ios": _stats(n=10, dur_p90=5000.0)}
    base = {"ci / ios": _stats(n=3, dur_p90=100.0)}
    needs, reasons = cm.decide_analysis(cur, base)
    assert not any("regression" in r for r in reasons)


def test_decide_macos_queue_spike():
    cur = {"ci / ios": _stats(n=5, queue_p90=16 * 60, macos=True)}
    needs, reasons = cm.decide_analysis(cur, {})
    assert needs is True
    assert any("queue spike" in r for r in reasons)


def test_decide_macos_queue_boundary_15m_is_not_signal():
    cur = {"ci / ios": _stats(n=5, queue_p90=15 * 60, macos=True)}
    needs, _ = cm.decide_analysis(cur, {})
    assert needs is False


def test_decide_non_macos_queue_ignored():
    cur = {"ci / lint": _stats(n=10, queue_p90=3600.0, macos=False)}
    base = {"ci / lint": _stats(n=10, queue_p90=3600.0)}
    needs, _ = cm.decide_analysis(cur, base)
    assert needs is False


def test_decide_failure_rate_signal_and_boundary():
    cur = {"ci / x": _stats(n=10, failure_rate=0.2, n_conclusion=10, n_failed=2)}
    needs, reasons = cm.decide_analysis(cur, {"ci / x": _stats()})
    assert needs is True
    assert any("failure rate" in r for r in reasons)
    boundary = {"ci / x": _stats(n=10, failure_rate=0.10, n_conclusion=10, n_failed=1)}
    needs_b, _ = cm.decide_analysis(boundary, {"ci / x": _stats()})
    assert needs_b is False


def test_decide_failure_rate_small_n_excluded():
    cur = {"ci / x": _stats(n=3, failure_rate=1.0, n_conclusion=3, n_failed=3)}
    needs, _ = cm.decide_analysis(cur, {"ci / x": _stats()})
    assert needs is False


def test_decide_new_job_signal():
    cur = {"ci / brand-new": _stats(n=5)}
    needs, reasons = cm.decide_analysis(cur, {"ci / old": _stats()})
    assert needs is True
    assert any("new workflow/job" in r for r in reasons)


def test_decide_baseline_missing_skips_new_but_keeps_absolute():
    cur = {"ci / whatever": _stats(n=5)}
    needs, reasons = cm.decide_analysis(cur, {})
    assert needs is False
    assert any("baseline" in r for r in reasons)
    # absolute thresholds still fire on first run
    bad = {"ci / ios": _stats(n=5, queue_p90=20 * 60, macos=True)}
    needs2, _ = cm.decide_analysis(bad, {})
    assert needs2 is True


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def test_render_body_has_table_and_marker():
    metrics = {"ts": "2026-11-22T00:00:00Z", "repo": "o/r", "window_days": 14,
               "runs_fetched": 3, "jobs": [], "steps": []}
    cur = {"ci / pytest": _stats(n=10, dur_p90=600.0)}
    base = {"ci / pytest": _stats(n=10, dur_p90=600.0)}
    body = cm.render_issue_body(metrics, cur, base, [], "https://run/1", "123")
    assert "| job | n (7d)" in body
    assert "ci / pytest" in body
    assert "<!-- ci-metrics:run:123 -->" in body
    assert "No baseline" not in body


def test_render_body_marks_regression_and_new():
    metrics = {"ts": "t", "repo": "o/r", "window_days": 14,
               "runs_fetched": 1, "jobs": [], "steps": []}
    cur = {"ci / a": _stats(n=6, dur_p90=1000.0), "ci / b": _stats(n=6)}
    base = {"ci / a": _stats(n=6, dur_p90=100.0)}
    body = cm.render_issue_body(metrics, cur, base,
                                ["signal: x"], "https://run/1", "9")
    assert "regression" in body
    assert "new" in body


def test_render_body_baseline_missing_note():
    body = cm.render_issue_body(
        {"ts": "t", "repo": "o/r", "window_days": 14, "runs_fetched": 0,
         "jobs": [], "steps": []},
        {}, {}, ["info: baseline empty (first run?) — regression/new-job checks skipped"],
        "", "local")
    assert "absolute values only" in body


# ---------------------------------------------------------------------------
# collect pagination boundary (mocked gh_api)
# ---------------------------------------------------------------------------

def test_fetch_paginated_stops_on_short_page(monkeypatch):
    calls = []

    def fake_gh_api(path):
        calls.append(path)
        if "page=1" in path:
            return {"jobs": [{"id": 1}]}, True
        return {"jobs": []}, True

    monkeypatch.setattr(cm, "gh_api", fake_gh_api)
    items, truncated = cm.fetch_paginated("repos/o/r/actions/runs/1/jobs", 5, per_page=100)
    assert len(items) == 1 and truncated is False
    assert len(calls) == 1


def test_fetch_paginated_truncates_at_max_pages(monkeypatch):
    def fake_gh_api(path):
        return {"jobs": [{"id": 1}] * 100}, True

    monkeypatch.setattr(cm, "gh_api", fake_gh_api)
    items, truncated = cm.fetch_paginated("repos/o/r/actions/runs/1/jobs", 3, per_page=100)
    assert len(items) == 300 and truncated is True


def test_find_open_issue_gh_failure_is_not_no_issue(monkeypatch):
    class FakeCP:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(cm, "gh", lambda args, dry_run=False: FakeCP())
    number, listed = cm.find_open_issue(dry_run=False)
    assert number is None and listed is False
