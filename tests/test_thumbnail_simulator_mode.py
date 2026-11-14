"""Integration tests for the dev-only ?thumb_perf=1 instrumentation mode
(Phase 2c v1 port, #487 — both lanes: uvicorn + BOOKQ_TEST_SERVER=rs).

These tests assert the route/template wiring only — the metric itself is
authored in scripts/perf/measure_thumbnails.py + app/perf/thumbnail-probe.js
and covered by tests/test_thumbnail_measurement.py.

rs serves the same shell: shell.rs renders index.html with PROBE_EXPR +
templates/thumbnail_perf.html when test mode + ?thumb_perf=1, so the
inlined probe/panel shape matches the Python lane byte for byte in the
asserted parts.
"""

import json
import urllib.error
import urllib.request

PROBE_MARKER = "__thumbnailProbeReport"
FIXTURE_NAME = "simple_manga"


def _get(server: str, path: str):
    req = urllib.request.Request(server + path)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return (
                resp.status,
                {k.lower(): v for k, v in resp.headers.items()},
                resp.read().decode("utf-8"),
            )
    except urllib.error.HTTPError as e:
        return (
            e.code,
            {k.lower(): v for k, v in (e.headers or {}).items()},
            (e.read() or b"").decode("utf-8", "replace"),
        )


def test_normal_index_has_no_probe_or_panel(server):
    status, headers, html = _get(server, "/")
    assert status == 200
    assert PROBE_MARKER not in html
    assert "thumbnail-perf-panel" not in html
    assert "no-store" not in headers.get("cache-control", "")


def test_probe_source_is_not_public_static_asset(server):
    """The shared probe is inlined by the gated route, never publicly served."""
    status, _, _ = _get(server, "/static/thumbnail-probe.js")
    assert status == 404


def test_thumb_perf_inlines_probe_and_panel(server):
    status, headers, html = _get(server, "/?thumb_perf=1")
    assert status == 200
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
    assert headers["cache-control"] == "no-store"


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
