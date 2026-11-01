"""``?since=<stale>`` full-tree response rides ``folders_body_cache`` (plan folders-since-cache).

The mismatch path of ``_folder_tree`` (``GET /api/folders?since=<stale>`` and
``GET /api/v1/tree?since=<stale>``) used to re-serialize the multi-MB tree on
EVERY poll request — the fill-poll hot spot. It now goes through
``_folders_response`` keyed ``(key_ns + "-since", "", None)`` under
``(gen, gen_int)``, so json.dumps+sha1 run once per generation and the
response carries ETag/Cache-Control like every other tree payload.

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_folders_since_cache.py -q
"""

import os
import sys
from pathlib import Path

import pytest

os.environ["BOOKQ_TEST_MODE"] = "true"

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from tests.helpers import get_folders_ready  # noqa: E402


@pytest.fixture
def client(downloads_tree: Path, monkeypatch):
    """TestClient serving the shared conftest fixture tree (no browser needed)."""
    monkeypatch.setenv("DOWNLOADS_DIR", str(downloads_tree))
    from app.main import create_app

    from fastapi.testclient import TestClient

    return TestClient(create_app())


def _generation(client) -> int:
    cache = client.app.state.metadata_index_cache
    return int(cache.get("generation", 1) or 1)


def test_since_current_generation_still_304(client):
    """The since == generation short-circuit is unchanged (no ETag needed)."""
    get_folders_ready(client)
    gen = _generation(client)
    r = client.get("/api/folders", params={"since": gen})
    assert r.status_code == 304
    assert r.content == b""


def test_since_mismatch_body_serialized_once_per_generation(client, monkeypatch):
    """Two ``?since=<stale>`` polls in one generation serialize only once."""
    import app.routes as routes

    get_folders_ready(client)
    calls = {"n": 0}
    real = routes.json.dumps

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(routes.json, "dumps", counting)

    r1 = client.get("/api/folders", params={"since": 0})
    assert r1.status_code == 200
    first = calls["n"]
    assert first >= 1
    r2 = client.get("/api/folders", params={"since": 0})
    assert r2.status_code == 200
    assert calls["n"] == first, "second ?since=<stale> re-serialized the tree"
    assert r2.content == r1.content  # byte-identical cached body
    assert r2.headers["etag"] == r1.headers["etag"]
    # If-None-Match against the since-ETag also never re-serializes.
    r3 = client.get(
        "/api/folders", params={"since": 0}, headers={"If-None-Match": r1.headers["etag"]}
    )
    assert r3.status_code == 304
    assert calls["n"] == first


def test_since_mismatch_carries_etag_and_304_roundtrip(client):
    """The mismatch response now carries ETag + private Cache-Control."""
    get_folders_ready(client)
    r1 = client.get("/api/folders", params={"since": 0})
    assert r1.status_code == 200
    etag = r1.headers.get("etag")
    assert etag, "since-mismatch response must carry an ETag"
    assert r1.headers.get("cache-control") == "private, max-age=60"
    r2 = client.get(
        "/api/folders", params={"since": 0}, headers={"If-None-Match": etag}
    )
    assert r2.status_code == 304
    assert r2.content == b""
    assert r2.headers.get("etag") == etag
    # Same round-trip on the v1 endpoint.
    v1 = client.get("/api/v1/tree", params={"since": 0})
    v1_etag = v1.headers.get("etag")
    assert v1_etag
    r3 = client.get(
        "/api/v1/tree", params={"since": 0}, headers={"If-None-Match": v1_etag}
    )
    assert r3.status_code == 304


def test_since_mismatch_shapes_v0_and_v1(client):
    """Payload envelopes stay byte-shape compatible with the pre-cache code."""
    get_folders_ready(client)
    gen = _generation(client)

    r0 = client.get("/api/folders", params={"since": 0})
    assert r0.status_code == 200
    body0 = r0.json()
    assert set(body0) == {"tree", "generation"}
    assert isinstance(body0["tree"], list) and body0["tree"]
    assert body0["generation"] == gen

    r1 = client.get("/api/v1/tree", params={"since": 0})
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["kind"] == "tree"
    assert isinstance(body1["nodes"], list) and body1["nodes"]
    assert body1["generation"] == gen
    # The since body wraps the same nodes as the normal full-tree payload.
    full = client.get("/api/v1/tree").json()
    assert body1["nodes"] == full["nodes"]
    assert body1["generation"] == full["generation"]


def test_since_cache_key_does_not_alias_full_tree(client):
    """(key_ns+"-since", "", None) must not collide with (key_ns, "", None).

    The same (path, depth) coordinates hold a bare list on the normal path
    and a dict envelope on the since path — only the namespaced key keeps
    either from being served at the other's URL.
    """
    get_folders_ready(client)
    r_list = client.get("/api/folders")
    assert r_list.status_code == 200 and isinstance(r_list.json(), list)
    list_etag = r_list.headers["etag"]

    r_since = client.get("/api/folders", params={"since": 0})
    assert isinstance(r_since.json(), dict), (
        "since body must be the {tree,generation} envelope, not the bare list"
    )
    assert r_since.headers["etag"] != list_etag

    # The normal full-tree entry is untouched by the since fill.
    r_list2 = client.get("/api/folders")
    assert isinstance(r_list2.json(), list)
    assert r_list2.headers["etag"] == list_etag

    # Cross-applying either ETag at the other's URL must not produce a 304.
    assert (
        client.get(
            "/api/folders", params={"since": 0}, headers={"If-None-Match": list_etag}
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/folders", headers={"If-None-Match": r_since.headers["etag"]}
        ).status_code
        == 200
    )
    # v1 is namespaced independently too.
    v1_etag = client.get("/api/v1/tree", params={"since": 0}).headers["etag"]
    assert v1_etag != r_since.headers["etag"]


def test_since_body_changes_across_generation_bump(client):
    """A generation bump invalidates the cached since body wholesale."""
    get_folders_ready(client)
    r1 = client.get("/api/folders", params={"since": 0})
    gen1 = r1.json()["generation"]
    etag1 = r1.headers["etag"]

    # Simulate the background fill's in-place bump (routes.py
    # _background_fill: cache["generation"] += 1 under _index_lock).
    client.app.state.metadata_index_cache["generation"] = gen1 + 1

    r2 = client.get("/api/folders", params={"since": 0})
    assert r2.status_code == 200
    assert r2.json()["generation"] == gen1 + 1, (
        "stale since body leaked across the generation bump"
    )
    assert r2.content != r1.content
    assert r2.headers["etag"] != etag1  # "-g<gen>" suffix rolls
    # The pre-bump ETag no longer validates — a stale-body 304 is impossible.
    r3 = client.get(
        "/api/folders", params={"since": 0}, headers={"If-None-Match": etag1}
    )
    assert r3.status_code == 200
    # And the once-stale client now hits the since==generation 304 short-cut.
    r4 = client.get("/api/folders", params={"since": gen1 + 1})
    assert r4.status_code == 304


def test_poll_304s_do_not_stamp_fill_idle_gate(client):
    """#243: payload-free poll responses must not re-stamp ``last_request_at``.

    ``_background_fill`` pauses while ``app.state.last_request_at`` is fresh
    (``_FILL_IDLE_SECONDS``). The stamp used to sit at the top of
    ``_folder_tree``, so a connected client's ``?since=<current>`` poll —
    every few seconds while a tab stays open — kept the idle gate shut and
    the fill starved forever. Only responses carrying a body may stamp.
    """
    get_folders_ready(client)
    gen = _generation(client)
    state = client.app.state

    # The fill-poller's hot path: ?since=<current> → 304, no stamp.
    state.last_request_at = 0.0
    r = client.get("/api/folders", params={"since": gen})
    assert r.status_code == 304
    assert state.last_request_at == 0.0, (
        "since==generation 304 re-stamped last_request_at — "
        "the fill's idle gate stays shut while a client polls"
    )
    # Same contract on the v1 endpoint.
    r_v1 = client.get("/api/v1/tree", params={"since": gen})
    assert r_v1.status_code == 304
    assert state.last_request_at == 0.0

    # An If-None-Match revalidation is payload-free too — no stamp.
    etag = client.get("/api/folders").headers["etag"]
    state.last_request_at = 0.0
    r_inm = client.get("/api/folders", headers={"If-None-Match": etag})
    assert r_inm.status_code == 304
    assert state.last_request_at == 0.0

    # Body responses still stamp — the gate's whole purpose.
    state.last_request_at = 0.0
    r_full = client.get("/api/folders")
    assert r_full.status_code == 200
    assert state.last_request_at > 0.0, "full-tree payload must stamp"

    state.last_request_at = 0.0
    r_mismatch = client.get("/api/folders", params={"since": 0})
    assert r_mismatch.status_code == 200
    assert state.last_request_at > 0.0, "since-mismatch payload must stamp"

    state.last_request_at = 0.0
    r_shallow = client.get("/api/folders", params={"depth": 1})
    assert r_shallow.status_code == 200
    assert state.last_request_at > 0.0, "shallow payload must stamp"
