"""Deterministic correctness checks for the opt-in thumbnail timing probe."""

import argparse
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PROBE = (ROOT / "app/perf/thumbnail-probe.js").read_text(encoding="utf-8")
SPEC = importlib.util.spec_from_file_location(
    "measure_thumbnails", ROOT / "scripts/perf/measure_thumbnails.py"
)
MEASURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MEASURE)
PIXEL = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
BROKEN = "data:image/png;base64,AAAA"


def _start(browser, *, ready=True, source=PIXEL, extra="", timeout_ms=30000):
    context = browser.new_context(viewport={"width": 800, "height": 600})
    page = context.new_page()
    page.set_content(
        '<style>body{margin:0}.tile{height:80px}img{width:64px;height:64px}</style>'
        '<div id="sidebar" class="open"><div id="sidebar-scroll" '
        'style="height:200px;overflow:auto"><div id="tile-grid">'
        f'<div class="tile" data-path="a"><img src="{source}"></div>{extra}'
        '</div></div></div>'
    )
    page.evaluate("(ready) => { window.treeLoaded = ready; }", ready)
    page.evaluate("(timeoutMs) => { window.__thumbnailProbeConfig = {timeoutMs}; }", timeout_ms)
    page.evaluate(PROBE)
    return context, page


def _result(page):
    page.wait_for_function("() => window.__thumbnailProbe.done", timeout=5000)
    return page.evaluate("() => window.__thumbnailProbe")


def test_probe_waits_for_full_tree_and_successful_decode(browser):
    context, page = _start(browser, ready=False)
    try:
        page.wait_for_function("() => window.__thumbnailProbe.decoded_images === 1")
        assert not page.evaluate("() => window.__thumbnailProbe.done")
        assert page.evaluate("() => window.__thumbnailProbe.visible_ready_ms") is None
        ready_at = page.evaluate("() => { window.treeLoaded = true; return performance.now(); }")
        result = _result(page)
        assert result["visible_ready_ms"] >= ready_at
        assert result["first_visible_ms"] <= result["visible_ready_ms"]
        assert result["observed_ms"] - result["visible_ready_ms"] >= 250
        assert result["decoded_images"] == result["visible_images"] == 1
    finally:
        context.close()


def test_probe_broken_complete_image_is_not_success(browser):
    context, page = _start(browser, source=BROKEN)
    try:
        page.wait_for_function("() => window.__thumbnailProbe.broken_images === 1")
        assert page.evaluate("() => document.querySelector('img').complete")
        result = page.evaluate("() => window.__thumbnailProbe")
        assert not result["done"]
        assert result["visible_ready_ms"] is None
        page.evaluate("(src) => { document.querySelector('img').src = src; }", PIXEL)
        result = _result(page)
        assert result["broken_images"] == 0
        assert result["decoded_images"] == 1
    finally:
        context.close()


def test_probe_excludes_images_outside_scrollport(browser):
    extra = (
        '<div class="tile" data-path="off" style="margin-top:200px">'
        f'<img loading="lazy" src="{BROKEN}"></div>'
    )
    context, page = _start(browser, extra=extra)
    try:
        result = _result(page)
        assert result["rendered_tiles"] == 2
        assert result["visible_tiles"] == result["visible_images"] == 1
        assert result["broken_images"] == 0
    finally:
        context.close()


def test_probe_restarts_quiet_window_when_grid_replaced(browser):
    context, page = _start(browser)
    try:
        page.wait_for_function("() => window.__thumbnailProbe.decoded_images === 1")
        replaced_at = page.evaluate(
            """src => {
                document.getElementById('tile-grid').innerHTML =
                    '<div class="tile" data-path="b"><img src="' + src +
                    '"></div><div class="tile" data-path="c"><img src="' + src + '"></div>';
                return performance.now();
            }""",
            PIXEL,
        )
        result = _result(page)
        assert result["visible_images"] == result["decoded_images"] == 2
        assert result["visible_ready_ms"] >= replaced_at
    finally:
        context.close()


def test_probe_empty_grid_never_reports_success(browser):
    context, page = _start(browser)
    try:
        page.evaluate("() => { document.getElementById('tile-grid').innerHTML = ''; }")
        page.wait_for_function("() => window.__thumbnailProbe.visible_images === 0")
        assert page.evaluate("() => window.__thumbnailProbe.visible_ready_ms") is None
        assert not page.evaluate("() => window.__thumbnailProbe.done")
    finally:
        context.close()


def test_probe_timeout_exports_same_safe_terminal_report(browser):
    context, page = _start(browser, source=BROKEN, timeout_ms=500)
    try:
        result = _result(page)
        assert result["status"] == "broken_images"
        assert result["visible_ready_ms"] is None
        report = page.evaluate("() => window.__thumbnailProbeReport")
        assert report["result"] == result
        assert report["environment"]["browser_cache"] == "existing_browser_session_not_reset"
        assert report["environment"]["viewport"] == {"width": 800, "height": 600, "dpr": 1}
        assert report["metric_version"] == 1
        assert BROKEN not in json.dumps(report)
    finally:
        context.close()


def test_measure_thumbnails_real_app(browser, server):
    result = MEASURE.measure_once(browser, server + "/", "iphone", 15000)
    assert result["status"] == "ok", result
    assert result["full_tree_ready"]
    assert result["visible_images"] > 0
    assert result["visible_images"] == result["decoded_images"]
    assert result["visible_ready_ms"] is not None
    assert "/thumb/" not in json.dumps(result)


def test_summary_does_not_hide_failed_runs():
    good = {"status": "ok", "visible_ready_ms": 100}
    assert MEASURE.summarize([good, {"status": "ok", "visible_ready_ms": 200}])[
        "median_visible_ready_ms"
    ] == 150
    summary = MEASURE.summarize([good, {"status": "broken_images", "visible_ready_ms": None}])
    assert summary == {
        "successful_runs": 1, "total_runs": 2, "median_visible_ready_ms": None
    }


@pytest.mark.parametrize("url", [
    "https://example.com", "http://localhost/?secret=x", "http://localhost/#/browse/x",
    "http://user:password@localhost", "http://localhost/book",
])
def test_measurement_rejects_non_dev_root_url(url):
    with pytest.raises(argparse.ArgumentTypeError):
        MEASURE.local_url(url)
