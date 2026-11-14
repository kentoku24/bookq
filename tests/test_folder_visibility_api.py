"""Visibility API v1 contract over live HTTP (both lanes).

Exercises GET/PUT folder-visibility against the session server fixture so
the same file runs under the default uvicorn lane and under
BOOKQ_TEST_SERVER=rs. TestClient and store internals live in the
sibling py-only module, which stays quarantined until it retires.

The hidden set is library-global and shared: every test that hides a root
unhides it in a finally block (the roundtrip pattern from test_api_v1).
"""

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

VIS_PATH = "/api/v1/folder-visibility"

ROOT_A = "long"
ROOT_B = "series"


def _headers(resp):
    return {k.lower(): v for k, v in resp.headers.items()}


def _req(server, method, path, body=None, headers=None):
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(server + path, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None), _headers(resp)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null"), _headers(e)
        except Exception:
            return e.code, None, _headers(e)


def _req_raw(server, method, path, raw, content_type=None, headers=None):
    hdrs = dict(headers or {})
    if content_type is not None:
        hdrs["Content-Type"] = content_type
    req = urllib.request.Request(server + path, data=raw, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = resp.read()
            try:
                body = json.loads(payload) if payload else None
            except Exception:
                body = None
            return resp.status, body, _headers(resp)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null"), _headers(e)
        except Exception:
            return e.code, None, _headers(e)


def _get(server, path=VIS_PATH):
    return _req(server, "GET", path)


def _owner(server):
    status, body, _ = _get(server)
    assert status == 200
    return body["owner"]


def _put(server, root, hidden, owner):
    return _req(
        server,
        "PUT",
        VIS_PATH,
        {"root": root, "hidden": hidden},
        headers={"x-bookq-visibility-owner": owner},
    )


def _ensure_visible(server, owner, *roots):
    for root in roots:
        try:
            _put(server, root, False, owner)
        except Exception:
            pass


def _private_headers(headers):
    assert headers.get("cache-control") == "private, no-store"
    assert headers.get("vary") == "Cookie"


# ── shape ────────────────────────────────────────────────────────────


def test_get_shape(server):
    status, body, headers = _get(server)
    assert status == 200
    assert set(body) == {"owner", "revision", "hiddenRoots"}
    assert isinstance(body["owner"], str) and body["owner"]
    assert isinstance(body["revision"], int)
    assert isinstance(body["hiddenRoots"], list)
    _private_headers(headers)


def test_user_query_param_ignored(server):
    _, snap, _ = _get(server)
    status, body, _ = _get(server, VIS_PATH + "?user=bob@example.com")
    assert status == 200
    assert body["owner"] == snap["owner"]


def test_error_responses_are_private_no_store(server):
    owner = _owner(server)
    status, _, headers = _req(
        server,
        "PUT",
        VIS_PATH,
        {"root": "a//b", "hidden": True},
        headers={"x-bookq-visibility-owner": owner},
    )
    assert status == 400
    _private_headers(headers)


# ── happy path ───────────────────────────────────────────────────────


def test_hide_get_unhide_flow(server):
    owner = _owner(server)
    _ensure_visible(server, owner, ROOT_A)
    try:
        _, before, _ = _get(server)
        status, body, headers = _put(server, ROOT_A, True, owner)
        assert status == 200
        assert body["revision"] == before["revision"] + 1
        assert body["hiddenRoots"] == [ROOT_A]
        _private_headers(headers)

        _, snap, _ = _get(server)
        assert snap["hiddenRoots"] == [ROOT_A]

        status, body, _ = _put(server, ROOT_A, False, owner)
        assert status == 200
        assert body["hiddenRoots"] == []
    finally:
        _ensure_visible(server, owner, ROOT_A)


def test_same_value_put_is_idempotent(server):
    owner = _owner(server)
    _ensure_visible(server, owner, ROOT_A)
    try:
        _put(server, ROOT_A, True, owner)
        _, first, _ = _get(server)
        status, body, _ = _put(server, ROOT_A, True, owner)
        assert status == 200
        assert body["revision"] == first["revision"]
    finally:
        _ensure_visible(server, owner, ROOT_A)


def test_different_roots_accumulate_last_write_wins(server):
    owner = _owner(server)
    _ensure_visible(server, owner, ROOT_A, ROOT_B)
    try:
        _, base, _ = _get(server)
        status, body, _ = _put(server, ROOT_A, True, owner)
        assert status == 200
        assert body["revision"] == base["revision"] + 1
        status, body = _put(server, ROOT_B, True, owner)[:2]
        assert status == 200
        assert sorted(body["hiddenRoots"]) == sorted([ROOT_A, ROOT_B])
        assert body["revision"] == base["revision"] + 2
        status, body, _ = _put(server, ROOT_A, False, owner)
        assert status == 200
        assert body["hiddenRoots"] == [ROOT_B]
        assert body["revision"] == base["revision"] + 3
    finally:
        _ensure_visible(server, owner, ROOT_A, ROOT_B)


def test_unhide_never_existed_root(server):
    owner = _owner(server)
    status, body, _ = _put(server, "never-was-xyz", False, owner)
    assert status == 200
    assert "never-was-xyz" not in body["hiddenRoots"]


@pytest.mark.parametrize("root", ["missing-xyz", "a/b", "long/sub"])
def test_unsupported_or_missing_root_is_404(server, root):
    owner = _owner(server)
    try:
        status, _, _ = _put(server, root, True, owner)
        assert status == 404
    finally:
        _ensure_visible(server, owner, root)


# ── owner header ─────────────────────────────────────────────────────


def test_put_missing_owner_header_is_403(server):
    status, _, headers = _req(server, "PUT", VIS_PATH, {"root": ROOT_A, "hidden": True})
    assert status == 403
    _private_headers(headers)


def test_put_owner_mismatch_is_409_and_writes_nothing(server):
    owner = _owner(server)
    _ensure_visible(server, owner, ROOT_A)
    try:
        bad = ("0" if owner[:1] != "0" else "1") + owner[1:]
        status, _, headers = _req(
            server,
            "PUT",
            VIS_PATH,
            {"root": ROOT_A, "hidden": True},
            headers={"x-bookq-visibility-owner": bad},
        )
        assert status == 409
        _private_headers(headers)
        _, snap, _ = _get(server)
        assert ROOT_A not in snap["hiddenRoots"]
    finally:
        _ensure_visible(server, owner, ROOT_A)


# ── input validation ─────────────────────────────────────────────────


@pytest.mark.parametrize("hidden", ["true", "false", 1, 0, None, [True]])
def test_hidden_must_be_strict_bool(server, hidden):
    owner = _owner(server)
    try:
        status, _, _ = _req(
            server,
            "PUT",
            VIS_PATH,
            {"root": ROOT_A, "hidden": hidden},
            headers={"x-bookq-visibility-owner": owner},
        )
        assert status == 400
    finally:
        _ensure_visible(server, owner, ROOT_A)


@pytest.mark.parametrize(
    "root",
    ["", "a\\b", "/abs", "a\x00b", ".", "..", ".hidden", "x" * 2049, 123, None, True,
     "a//b", "a/./b", "a/../b", "/a", "a/", "a/.b", ".h/x"],
)
def test_invalid_roots_are_400(server, root):
    owner = _owner(server)
    try:
        status, _, _ = _req(
            server,
            "PUT",
            VIS_PATH,
            {"root": root, "hidden": True},
            headers={"x-bookq-visibility-owner": owner},
        )
        assert status == 400
    finally:
        if isinstance(root, str) and root:
            _ensure_visible(server, owner, root)


def test_unknown_and_missing_fields_are_400(server):
    owner = _owner(server)
    try:
        for body in (
            {"root": ROOT_A, "hidden": True, "user": "bob@example.com"},
            {"root": ROOT_A},
            {"hidden": True},
            {},
        ):
            status, _, _ = _req(
                server, "PUT", VIS_PATH, body,
                headers={"x-bookq-visibility-owner": owner},
            )
            assert status == 400, body
    finally:
        _ensure_visible(server, owner, ROOT_A)


@pytest.mark.parametrize("body", [[1, 2], "x", 42, True])
def test_non_object_body_is_400(server, body):
    owner = _owner(server)
    try:
        status, _, _ = _req(
            server, "PUT", VIS_PATH, body,
            headers={"x-bookq-visibility-owner": owner},
        )
        assert status == 400
    finally:
        _ensure_visible(server, owner, ROOT_A)


def test_malformed_json_is_400(server):
    owner = _owner(server)
    status, _, _ = _req_raw(
        server, "PUT", VIS_PATH, b"{not json",
        content_type="application/json",
        headers={"x-bookq-visibility-owner": owner},
    )
    assert status == 400


def test_non_json_content_type_is_415(server):
    owner = _owner(server)
    status, _, _ = _req_raw(
        server, "PUT", VIS_PATH, b'{"root": "long", "hidden": true}',
        content_type="text/plain",
        headers={"x-bookq-visibility-owner": owner},
    )
    assert status == 415
    # no content-type at all is also 415
    parsed = urllib.parse.urlparse(server)
    raw_body = b'{"root": "long", "hidden": true}'
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
    conn.putrequest("PUT", VIS_PATH)
    conn.putheader("X-Bookq-Visibility-Owner", owner)
    conn.putheader("Content-Length", str(len(raw_body)))
    conn.endheaders(raw_body)
    resp = conn.getresponse()
    assert resp.status == 415
    conn.close()
