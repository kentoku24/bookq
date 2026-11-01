"""metadata/facets attach runs once per cache generation (#244).

``_folder_tree`` used to run ``_attach_metadata`` + ``_attach_subtree_facets``
on EVERY ``/api/folders`` full-tree request — two whole-tree walks (~17ms on
the live library) mutating the shared cached tree in place. Their output
depends only on ``(tree, index)``, fixed for the whole cache generation, so
``metadata_index_cache`` now carries an ``attach_gen`` marker holding the
``(timestamp, generation)`` pair the last attach ran under:

- same-generation requests (full tree, ``?since=<stale>``, v1) skip both walks;
- ``_background_fill``'s ``generation`` bump, a rebuild/warmup//api/refresh
  dict swap, and the signature gate's in-place ``timestamp`` re-stamp each
  force exactly one re-attach on the next body-serving request.

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_attach_once.py -q
"""

import os
import sys
import time
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


@pytest.fixture
def attach_calls(monkeypatch):
    """Count TOP-LEVEL _attach_metadata/_attach_subtree_facets invocations.

    Both functions recurse through their module-global name, so a plain
    wraps-count would tally every node — only ``parent_path == ""`` marks a
    request-level attach pass.
    """
    import app.routes as routes

    calls = {"metadata": 0, "facets": 0}
    real_metadata = routes._attach_metadata
    real_facets = routes._attach_subtree_facets

    def count_metadata(nodes, lookup, parent_path):
        if parent_path == "":
            calls["metadata"] += 1
        return real_metadata(nodes, lookup, parent_path)

    def count_facets(nodes, subtree, parent_path):
        if parent_path == "":
            calls["facets"] += 1
        return real_facets(nodes, subtree, parent_path)

    monkeypatch.setattr(routes, "_attach_metadata", count_metadata)
    monkeypatch.setattr(routes, "_attach_subtree_facets", count_facets)
    return calls


def test_same_generation_requests_skip_attach(client, attach_calls):
    """One attach per generation across full-tree, ?since=<stale>, and v1."""
    get_folders_ready(client)  # cold build lands + the first attach runs here
    assert attach_calls == {"metadata": 1, "facets": 1}, (
        "the first body-serving request must attach exactly once"
    )
    cache = client.app.state.metadata_index_cache
    assert cache["attach_gen"] == (cache["timestamp"], int(cache["generation"]))

    for params in ({}, {"since": 0}):
        r = client.get("/api/folders", params=params)
        assert r.status_code == 200
    r_v1 = client.get("/api/v1/tree")
    assert r_v1.status_code == 200
    assert attach_calls == {"metadata": 1, "facets": 1}, (
        "same-generation request re-ran the attach walks"
    )
    # The attached fields still reach the client — they live on the shared
    # tree, not in the per-request pass that used to write them.
    assert all("has_metadata" in n for n in r_v1.json()["nodes"])


def test_generation_bump_triggers_exactly_one_reattach(client, attach_calls):
    """A fill-style in-place generation bump re-attaches once, then skips."""
    get_folders_ready(client)
    assert attach_calls["metadata"] == 1
    cache = client.app.state.metadata_index_cache
    gen = int(cache.get("generation", 1) or 1)

    # Same shape as routes.py _background_fill: cache["generation"] += 1.
    cache["generation"] = gen + 1
    r = client.get("/api/folders")
    assert r.status_code == 200
    assert attach_calls == {"metadata": 2, "facets": 2}, (
        "generation bump must force exactly one re-attach"
    )
    assert cache["attach_gen"] == (cache["timestamp"], gen + 1)

    client.get("/api/folders")
    client.get("/api/folders", params={"since": 0})
    assert attach_calls == {"metadata": 2, "facets": 2}, (
        "post-bump same-generation requests must skip the attach"
    )


def test_fresh_cache_dict_reattaches_once(client, attach_calls):
    """A rebuild/warmup-style dict swap carries no marker — one re-attach."""
    get_folders_ready(client)
    assert attach_calls["metadata"] == 1
    old = client.app.state.metadata_index_cache

    # Same keys the rebuild swap writes (routes.py / _rebuild_into_cache):
    # no attach_gen on the fresh dict, generation carried over.
    client.app.state.metadata_index_cache = {
        "tree": old["tree"],
        "index": old["index"],
        "timestamp": time.time(),
        "generation": old["generation"],
        "signature": old.get("signature"),
    }
    assert client.get("/api/folders").status_code == 200
    assert attach_calls == {"metadata": 2, "facets": 2}, (
        "a fresh cache_data must attach exactly once"
    )
    assert client.get("/api/folders").status_code == 200
    assert attach_calls == {"metadata": 2, "facets": 2}


def test_timestamp_restamp_reattaches_once(client, attach_calls):
    """The signature gate's in-place timestamp refresh re-attaches once.

    The marker is a (timestamp, generation) pair, so an unchanged-tree TTL
    re-stamp (``cur["timestamp"] = time.time()`` in _background_index_refresh)
    mismatches it — one cheap re-attach per refresh cycle doubles as a
    self-heal for any mutation that skipped the generation bump.
    """
    get_folders_ready(client)
    assert attach_calls["metadata"] == 1
    cache = client.app.state.metadata_index_cache

    cache["timestamp"] = time.time() + 1.0  # strictly newer — a re-stamp
    assert client.get("/api/folders").status_code == 200
    assert attach_calls == {"metadata": 2, "facets": 2}, (
        "timestamp re-stamp must force exactly one re-attach"
    )
    assert client.get("/api/folders").status_code == 200
    assert attach_calls == {"metadata": 2, "facets": 2}


def test_since_equal_generation_304_never_attaches(client, attach_calls):
    """The since==gen 304 short-circuit stays ahead of the attach block."""
    get_folders_ready(client)
    gen = int(client.app.state.metadata_index_cache["generation"])
    r = client.get("/api/folders", params={"since": gen})
    assert r.status_code == 304
    assert attach_calls == {"metadata": 1, "facets": 1}


def test_shallow_path_serves_attached_nodes(client):
    """Shallow copies ride the same shared tree the one attach stamped.

    attach mutates ``cache_data["tree"]`` in place, so after a single
    attach the shallow listing's ``{**c, "children": []}`` copies carry
    ``has_metadata`` — identical to the pre-change per-request behavior.
    """
    get_folders_ready(client)  # lands the generation's single attach
    r = client.get("/api/folders", params={"path": "", "depth": 1})
    assert r.status_code == 200
    nodes = r.json()
    assert nodes and all("has_metadata" in n for n in nodes)

    # And the ?since=<stale> envelope serves the same attached nodes.
    r2 = client.get("/api/folders", params={"since": 0})
    assert r2.status_code == 200
    assert all("has_metadata" in n for n in r2.json()["tree"])
