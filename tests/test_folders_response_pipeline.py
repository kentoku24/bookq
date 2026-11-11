"""``_folders_response`` pipeline: cached gzip variant + orjson body (issue #241).

Every cached ``folders_body_cache`` entry now carries a lazily-built
``gzip.compress`` variant served to ``Accept-Encoding: gzip`` clients —
ApiGZipMiddleware's GZipResponder passes responses that already carry
``Content-Encoding`` through untouched, so compression runs once per
generation per (path, depth) instead of once per request per client.
Serialization moved to ``orjson.dumps(payload, default=str)``, byte
compatible with the stdlib call it replaced (raw UTF-8, compact
separators, default= callable).

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_folders_response_pipeline.py -q
"""

import gzip
import json
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


def test_gzip_accepting_request_gets_content_encoding_gzip(client):
    """gzip-accepting clients get the cached compressed variant on the wire."""
    get_folders_ready(client)
    r = client.get("/api/folders", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"
    assert "accept-encoding" in r.headers.get("vary", "").lower()
    # httpx transparently decoded the wire bytes — had the middleware
    # re-compressed our pre-compressed body, one decode pass would leave
    # gzip bytes and .json() would fail.
    body = r.json()
    assert isinstance(body, list) and body
    # Wire proof the body really traveled compressed (Content-Length is the
    # compressed byte count; r.content is the decoded JSON).
    assert int(r.headers["content-length"]) < len(r.content)


def test_second_gzip_request_serves_cached_compressed_bytes(client, monkeypatch):
    """gzip.compress runs exactly once per generation — later hits stay cached."""
    import app.routes as routes

    calls = {"n": 0}
    real = gzip.compress

    def counting(data, *a, **kw):
        calls["n"] += 1
        return real(data, *a, **kw)

    monkeypatch.setattr(routes.gzip, "compress", counting)
    get_folders_ready(client)

    r1 = client.get("/api/folders", headers={"Accept-Encoding": "gzip"})
    assert r1.status_code == 200
    assert r1.headers["content-encoding"] == "gzip"
    assert calls["n"] == 1, "gzip variant must be built once, not per request"

    r2 = client.get("/api/folders", headers={"Accept-Encoding": "gzip"})
    assert r2.status_code == 200
    assert calls["n"] == 1, "second gzip request re-compressed the body"
    assert r2.headers["content-length"] == r1.headers["content-length"]
    assert r2.headers["etag"] == r1.headers["etag"]

    # A 304 revalidation never compresses either.
    r3 = client.get(
        "/api/folders",
        headers={"Accept-Encoding": "gzip", "If-None-Match": r1.headers["etag"]},
    )
    assert r3.status_code == 304
    assert r3.content == b""
    assert calls["n"] == 1, "304 revalidation re-compressed"


def test_non_gzip_request_gets_identity_body(client):
    """Clients without gzip get the identity body from the same cache entry."""
    get_folders_ready(client)
    # Fill the gzip variant first: proves identity is served alongside it,
    # not instead of it.
    rg = client.get("/api/folders", headers={"Accept-Encoding": "gzip"})
    assert rg.headers["content-encoding"] == "gzip"

    r = client.get("/api/folders", headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200
    assert "content-encoding" not in r.headers
    body = r.json()
    assert isinstance(body, list) and body
    # Same representation, same ETag — encoding varies, entity does not.
    assert r.headers["etag"] == rg.headers["etag"]


def test_orjson_body_is_wire_compatible(client):
    """The orjson-serialized body parses identically to the old stdlib output."""
    import orjson

    get_folders_ready(client)
    r = client.get("/api/folders", headers={"Accept-Encoding": "identity"})
    body = r.json()
    assert isinstance(body, list) and body

    # Byte-for-byte equal to the replaced stdlib call on a tree-shaped
    # payload covering unicode, escapes, floats, null, bool.
    sample = {
        "tree": body,
        "generation": 1,
        "node": {
            "name": '第<十>話 "q" \\ ünïçødé\t',
            "v": 1.5,
            "n": None,
            "b": False,
        },
    }
    expected = json.dumps(
        sample,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    assert orjson.dumps(sample, default=str) == expected

    # default= keeps the stdlib contract: invoked for unserializable types.
    class _Weird:
        pass

    weird = _Weird()  # same instance -> same repr in both outputs
    assert orjson.dumps({"x": weird}, default=str) == json.dumps(
        {"x": weird}, default=str, separators=(",", ":")
    ).encode("utf-8")

    # The actual endpoint body round-trips through stdlib json unchanged.
    assert json.loads(r.content) == body
