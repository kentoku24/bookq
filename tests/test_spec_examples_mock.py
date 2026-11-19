"""PWA runs on spec examples alone — no server implementation (#237).

The app shell HTML comes from the test server (static template), but EVERY
data response (tree/images/search/facets/positions/visibility + all media)
is fulfilled from ``api/dist/examples.json`` via ``spec_examples``. The
assertions below read spec-fixture names ("Series", "work.cbz"), never the
conftest synthetic tree ("long", "series"), so they fail if any call leaks
through to the real implementation.

Run:
    uv run --with pytest --with playwright --with-requirements requirements-dev.txt \
        pytest tests/test_spec_examples_mock.py -q
"""
from playwright.sync_api import expect

from tests.spec_examples import bundle, example, install_spec_routes


def _boot_spec_only(browser, server, scenario=None, status_overrides=None):
    """Fresh context with spec routes installed BEFORE first navigation."""
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    hits = install_spec_routes(page, scenario=scenario,
                               status_overrides=status_overrides)
    page.goto(server)
    return context, page, hits


def test_bundle_covers_major_endpoints():
    """Guard: the fixture source actually covers the PWA's major calls."""
    majors = ["getTree", "listImages", "search", "getFacets", "refreshIndex",
              "listReadingPositions", "putReadingPosition", "deleteReadingPosition",
              "getFolderVisibility", "putFolderVisibility", "exchangeToken",
              "getVersion", "getHealth"]
    ops = {e["operationId"] for e in bundle()["examples"]}
    for op in majors:
        assert op in ops, f"bundle missing {op}"
        assert any(e["operationId"] == op and e["status"] == "200"
                   and e["name"] == "default" for e in bundle()["examples"]), \
            f"bundle missing {op} 200 [default]"
    # Edge cases the mock scenarios rely on.
    assert example("getTree", "200", "children")["kind"] == "children"
    assert example("putReadingPosition", "200", "stale") == {"status": "stale"}
    assert "owner_mismatch" in [e["name"] for e in bundle()["examples"]
                                if e["operationId"] == "putFolderVisibility"
                                and e["status"] == "409"]


def test_browse_renders_from_spec_examples_only(browser, server):
    """Tiles come from the spec tree fixture, not the server tree."""
    context, page, hits = _boot_spec_only(browser, server)
    try:
        page.wait_for_selector(".tile", timeout=15000)
        paths = page.locator(".tile").evaluate_all(
            "els => els.map(e => e.getAttribute('data-path'))")
        assert "Series" in paths, paths
        assert "work.cbz" in paths, paths
        assert not any((p or "").split("/")[0] == "long" for p in paths), paths
        assert hits.get("/api/v1/tree", 0) >= 1
    finally:
        context.close()


def test_tree_202_index_building_recovers_from_spec_example(browser, server):
    """First full-tree hit 202s (Retry-After: 0), retry serves 200 — UI heals."""
    scenario = {"getTree": [(202, "index_building"), (200, "default")]}
    context, page, hits = _boot_spec_only(browser, server, scenario=scenario)
    try:
        page.wait_for_selector(".tile", timeout=20000)
        paths = page.locator(".tile").evaluate_all(
            "els => els.map(e => e.getAttribute('data-path'))")
        assert "Series" in paths, paths
        assert hits.get("/api/v1/tree", 0) >= 2, hits
    finally:
        context.close()
