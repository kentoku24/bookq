"""Unit tests for scripts/prod_monitor.py — classification, signature, verdict.

Pure-function coverage only: no network, no gh, no server fixture needed.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import prod_monitor as pm  # noqa: E402


def _http(status, body=b"", error=None, headers=None):
    return pm.HttpResult(
        status=status,
        headers=headers or {},
        body=body,
        latency_ms=1,
        error=error,
    )


# ---------------------------------------------------------------------------
# classify_status
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [520, 521, 522, 523, 524, 525, 526, 530])
def test_cf_error_statuses_are_cf_tunnel(status):
    assert pm.classify_status(status, b"") == "cf_tunnel"


def test_502_with_cloudflare_body_is_cf_tunnel():
    assert pm.classify_status(502, b"<html>cloudflare Error 502</html>") == "cf_tunnel"


def test_502_without_cloudflare_body_is_app_error():
    assert pm.classify_status(502, b'{"detail":"bad gateway"}') == "app_error"


def test_500_is_app_error():
    assert pm.classify_status(500, b"{}") == "app_error"


def test_404_is_contract_break():
    assert pm.classify_status(404, b"not found") == "contract_break"


# ---------------------------------------------------------------------------
# evaluators
# ---------------------------------------------------------------------------

def test_eval_health_ok():
    oc = pm.eval_health(_http(200, b'{"status":"ok"}'))
    assert oc.ok and oc.fail_class is None


def test_eval_health_500_is_app_error():
    oc = pm.eval_health(_http(500, b'{"detail":"x"}'))
    assert not oc.ok and oc.fail_class == "app_error"


def test_eval_health_transport_is_unreachable():
    oc = pm.eval_health(_http(None, error="timeout"))
    assert not oc.ok and oc.fail_class == "unreachable"


def test_eval_folders_gate_200_is_auth_bypass():
    oc = pm.eval_folders_gate(_http(200, b'{"folders":[]}'))
    assert not oc.ok and oc.fail_class == "auth_bypass"


def test_eval_folders_gate_401_passes():
    assert pm.eval_folders_gate(_http(401, b"")).ok


def test_eval_auth_login_redirect_ok():
    oc = pm.eval_auth_login(_http(302, headers={"location": "https://accounts.google.com/o/oauth2"}))
    assert oc.ok


def test_eval_auth_login_wrong_host_is_contract_break():
    oc = pm.eval_auth_login(_http(302, headers={"location": "https://evil.example.com"}))
    assert not oc.ok and oc.fail_class == "contract_break"


def test_eval_auth_login_200_is_contract_break():
    oc = pm.eval_auth_login(_http(200, b"<html>login</html>"))
    assert not oc.ok and oc.fail_class == "contract_break"


def test_eval_version_extracts_sha():
    oc = pm.eval_version(_http(200, b'{"version":"2.0.0","sha":"2614ce9c2fcc","build_date":"2026-11-09"}'))
    assert oc.ok and oc.extra["sha"] == "2614ce9c2fcc"


def test_eval_version_bad_sha_is_contract_break():
    oc = pm.eval_version(_http(200, b'{"sha":""}'))
    assert not oc.ok and oc.fail_class == "contract_break"


def test_eval_static_app_size_floor():
    assert not pm.eval_static_app(_http(200, b"x" * 50_000)).ok
    assert pm.eval_static_app(_http(200, b"x" * 200_000)).ok


# ---------------------------------------------------------------------------
# run_probe verdict / signature (fetch is stubbed)
# ---------------------------------------------------------------------------

def _stub_fetch_ok(url, timeout):
    path = url.split("bookq.mats.dpdns.org")[-1]
    bodies = {
        "/api/health": b'{"status":"ok"}',
        "/api/v1/health": b'{"status":"ok"}',
        "/api/version": b'{"version":"2.0.0","sha":"2614ce9c2fcc","build_date":"d"}',
        "/": "<html><title>bookq — ログイン</title></html>".encode(),
        "/api/folders": b"",
        "/static/sw.js": b"bookq-shell-v1",
        "/static/app.js": b"x" * 200_000,
    }
    if path == "/auth/login":
        return _http(302, headers={"location": "https://accounts.google.com/x"})
    status = 401 if path == "/api/folders" else 200
    return _http(status, bodies.get(path, b""))


def test_run_probe_all_ok(monkeypatch):
    monkeypatch.setattr(pm, "fetch", _stub_fetch_ok)
    result = pm.run_probe("https://bookq.mats.dpdns.org", 1)
    assert result["verdict"] == "ok"
    assert result["signature"] == "ok"
    assert result["version"]["sha"] == "2614ce9c2fcc"
    assert len(result["checks"]) == len(pm.CHECKS)


def test_run_probe_all_down(monkeypatch):
    def dead(url, timeout):
        return _http(None, error="timeout")
    monkeypatch.setattr(pm, "fetch", dead)
    result = pm.run_probe("https://bookq.mats.dpdns.org", 1)
    assert result["verdict"] == "fail"
    assert result["classification"] == "unreachable"
    assert result["signature"].startswith("unreachable:")
    assert "health" in result["signature"]


def test_run_probe_auth_bypass_is_top_class(monkeypatch):
    def bypass(url, timeout):
        if url.endswith("/api/folders"):
            return _http(200, b'{"folders":[]}')
        return _stub_fetch_ok(url, timeout)
    monkeypatch.setattr(pm, "fetch", bypass)
    result = pm.run_probe("https://bookq.mats.dpdns.org", 1)
    assert result["classification"] == "auth_bypass"
    assert "folders_gate" in result["signature"]


def test_probe_error_result_is_valid_fail():
    r = pm.probe_error_result(RuntimeError("boom"))
    assert r["verdict"] == "fail" and r["classification"] == "probe_error"
    assert r["signature"] == "probe_error:internal"


# ---------------------------------------------------------------------------
# report helpers
# ---------------------------------------------------------------------------

def test_sig_marker_roundtrip():
    body = "text <!-- monitor-sig:cf_tunnel:health,version --> tail"
    m = pm.SIG_MARKER_RE.search(body)
    assert m and m.group(1) == "cf_tunnel:health,version"


def test_decide_fail_action_signature_change_triggers_analysis():
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(minutes=5)).isoformat()
    action, needs = pm.decide_fail_action("old:sig", recent, "new:sig", now)
    assert action == "comment" and needs is True


def test_decide_fail_action_same_sig_fresh_is_skip():
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(minutes=5)).isoformat()
    action, needs = pm.decide_fail_action("same:sig", recent, "same:sig", now)
    assert action == "skip" and needs is False


def test_decide_fail_action_same_sig_stale_comments_without_analysis():
    now = datetime.now(timezone.utc)
    old = (now - timedelta(minutes=61)).isoformat()
    action, needs = pm.decide_fail_action("same:sig", old, "same:sig", now)
    assert action == "comment" and needs is False


def test_decide_fail_action_no_marker_counts_as_changed():
    now = datetime.now(timezone.utc)
    action, needs = pm.decide_fail_action(None, None, "any:sig", now)
    assert action == "comment" and needs is True


def test_format_duration():
    now = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
    assert pm.format_duration("2026-11-11T10:00:00Z", now) == "2h 0m"
    assert pm.format_duration("2026-11-11T11:30:00Z", now) == "30m"
    assert pm.format_duration(None, now) == "unknown"


def test_render_issue_body_has_marker_and_context():
    result = {
        "verdict": "fail", "classification": "cf_tunnel",
        "signature": "cf_tunnel:health", "summary": "cf_tunnel: 1/8 checks failed (health)",
        "ts": "2026-11-11T00:00:00Z", "base_url": "https://x",
        "version": {"sha": "abc1234", "build_date": "d"},
        "checks": [{"id": "health", "ok": False, "status": 502, "latency_ms": 5, "detail": "HTTP 502"}],
    }
    body = pm.render_issue_body(result, "https://run", "deadbeef")
    assert "<!-- monitor-sig:cf_tunnel:health -->" in body
    assert "abc1234" in body and "deadbeef" in body
    assert "cf_tunnel" in body
