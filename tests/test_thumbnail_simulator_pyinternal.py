"""?thumb_perf=1 production-gate test — Python internals (Phase 2c retire, #487).

Outside test mode the probe must not inject. This reaches into
``app.auth._TEST_MODE`` / ``app.routes.get_current_user`` and boots an
in-process TestClient, so it cannot oracle the Rust backend: it stays
quarantined under BOOKQ_TEST_SERVER=rs (collection hook: ``import app``)
and retires with ``app/`` in Phase 3. The HTTP/browser contract lives in
tests/test_thumbnail_simulator_mode.py and runs in both lanes.
"""

import pytest


@pytest.fixture
def plain_client(downloads_tree, monkeypatch):
    """TestClient with TEST_MODE left as imported (true under pytest)."""
    monkeypatch.setenv("DOWNLOADS_DIR", str(downloads_tree))
    from app.main import create_app
    from fastapi.testclient import TestClient

    return TestClient(create_app())


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
    assert "__thumbnailProbeReport" not in html
    assert "thumbnail-perf-panel" not in html
    assert "no-store" not in r.headers.get("cache-control", "")
