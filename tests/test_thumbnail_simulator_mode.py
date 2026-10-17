"""Integration tests for the dev-only ?thumb_perf=1 instrumentation mode.

These tests assert the route/template wiring only — the metric itself is
authored in scripts/perf/measure_thumbnails.py + app/perf/thumbnail-probe.js
and covered by tests/test_thumbnail_measurement.py.
"""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PROBE_MARKER = "__thumbnailProbeReport"
FIXTURE_NAME = "simple_manga"


@pytest.fixture
def plain_client(downloads_tree, monkeypatch):
    """TestClient with TEST_MODE left as imported (true under pytest)."""
    monkeypatch.setenv("DOWNLOADS_DIR", str(downloads_tree))
    from app.main import create_app
    from fastapi.testclient import TestClient

    return TestClient(create_app())


def test_normal_index_has_no_probe_or_panel(plain_client):
    r = plain_client.get("/")
    assert r.status_code == 200
    html = r.text
    assert PROBE_MARKER not in html
    assert "thumbnail-perf-panel" not in html
    assert "no-store" not in r.headers.get("cache-control", "")


def test_probe_source_is_not_public_static_asset(plain_client):
    """The shared probe is inlined by the gated route, never publicly served."""
    r = plain_client.get("/static/thumbnail-probe.js")
    assert r.status_code == 404


def test_thumb_perf_inlines_probe_and_panel(plain_client):
    r = plain_client.get("/?thumb_perf=1")
    assert r.status_code == 200
    html = r.text
    # Shared probe source inlined before the deferred app.js script tag.
    probe_pos = html.index("Measurement-only observer")
    assert PROBE_MARKER in html[probe_pos:]
    assert probe_pos < html.index("/static/app.js")
    assert 'id="thumbnail-perf-panel"' in html
    assert 'id="thumbnail-perf-json"' in html
    assert 'id="thumbnail-perf-copy"' in html
    assert 'id="thumbnail-perf-save"' in html
    assert 'id="thumbnail-perf-reload"' in html
    assert 'id="thumbnail-perf-close"' in html
    assert r.headers["cache-control"] == "no-store"


def test_thumb_perf_disabled_without_test_mode(plain_client, monkeypatch):
    """Production path: _TEST_MODE false + authenticated user -> no injection."""
    import app.auth as auth_module
    import app.routes as routes_module

    monkeypatch.setattr(auth_module, "_TEST_MODE", False)

    async def authenticated_user(request):
        return {"email": "test@example.com", "name": "Test User", "picture": ""}

    monkeypatch.setattr(routes_module, "get_current_user", authenticated_user)
    r = plain_client.get("/?thumb_perf=1")
    assert r.status_code == 200
    html = r.text
    assert PROBE_MARKER not in html
    assert "thumbnail-perf-panel" not in html
    assert "no-store" not in r.headers.get("cache-control", "")


def test_thumb_perf_page_finishes_and_panel_matches_report(browser, server):
    context = browser.new_context(viewport={"width": 800, "height": 600})
    page = context.new_page()
    try:
        page.goto(server + "/?thumb_perf=1", wait_until="commit")
        # Panel markup is server-rendered but must stay hidden (attribute AND
        # computed style — the panel's own display rule must not beat [hidden])
        # until the terminal report arrives.
        page.wait_for_selector(
            "#thumbnail-perf-panel[hidden]", state="attached", timeout=5000
        )
        assert (
            page.eval_on_selector(
                "#thumbnail-perf-panel", "el => getComputedStyle(el).display"
            )
            == "none"
        )
        page.wait_for_function(
            "() => window.__thumbnailProbeReport", timeout=45000
        )
        page.wait_for_selector("#thumbnail-perf-panel:not([hidden])", timeout=5000)
        report = page.evaluate("() => window.__thumbnailProbeReport")
        assert report["metric_version"] == 1
        env = report["environment"]
        assert env["user_agent"]
        assert env["viewport"]["width"] == 800
        assert env["viewport"]["height"] == 600
        assert env["viewport"]["dpr"] == 1
        assert env["browser_cache"] == "existing_browser_session_not_reset"
        assert env["timeout_ms"] == 30000
        assert "navigation_time_origin_ms" in env
        result = report["result"]
        assert result["status"] == "ok"
        assert result["visible_ready_ms"] is not None

        textarea_json = page.eval_on_selector(
            "#thumbnail-perf-json", "el => el.value"
        )
        assert textarea_json == json.dumps(report, indent=2)
        assert "/thumb/" not in textarea_json
        assert "/image/" not in textarea_json
        assert FIXTURE_NAME not in textarea_json

        # Close hides the panel.
        page.click("#thumbnail-perf-close")
        page.wait_for_selector(
            "#thumbnail-perf-panel[hidden]", state="attached", timeout=5000
        )
    finally:
        context.close()


def test_thumb_perf_reload_produces_new_run(browser, server):
    context = browser.new_context(viewport={"width": 800, "height": 600})
    page = context.new_page()
    try:
        page.goto(server + "/?thumb_perf=1", wait_until="commit")
        page.wait_for_function(
            "() => window.__thumbnailProbeReport", timeout=45000
        )
        first_origin = page.evaluate(
            "() => window.__thumbnailProbeReport.environment.navigation_time_origin_ms"
        )
        page.click("#thumbnail-perf-reload")
        page.wait_for_function(
            "() => window.__thumbnailProbeReport", timeout=45000
        )
        second_origin = page.evaluate(
            "() => window.__thumbnailProbeReport.environment.navigation_time_origin_ms"
        )
        assert second_origin != first_origin
    finally:
        context.close()


def test_thumb_perf_copy_button_writes_exact_json(browser, server):
    context = browser.new_context(viewport={"width": 800, "height": 600})
    page = context.new_page()
    try:
        page.goto(server + "/?thumb_perf=1", wait_until="commit")
        page.wait_for_function(
            "() => window.__thumbnailProbeReport", timeout=45000
        )
        page.wait_for_selector("#thumbnail-perf-panel:not([hidden])", timeout=5000)
        page.evaluate(
            """() => {
                window.__copied = [];
                Object.defineProperty(navigator, "clipboard", {
                    configurable: true,
                    value: {writeText: (t) => {
                        window.__copied.push(t);
                        return Promise.resolve();
                    }},
                });
            }"""
        )
        page.click("#thumbnail-perf-copy")
        copied = page.evaluate("() => window.__copied")
        expected = page.eval_on_selector("#thumbnail-perf-json", "el => el.value")
        assert copied == [expected]
    finally:
        context.close()
