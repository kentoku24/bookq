"""Native auth HTTP contract over live HTTP (both lanes).

Covers what the v1 token bootstrap exposes without seeding server-side
state: grant validation, unknown-code rejection, JSON error shape, the
no-store cache posture, and the native login parameter gate. One-time
code minting, PKCE math, rotation internals and the callback redirect
stay in the sibling py-only module (backed by the native unit tests on
the Rust side), which remains quarantined until it retires.
"""

import base64
import hashlib
import http.client
import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

TOKEN_PATH = "/api/v1/auth/token"


def _s256(verifier):
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _headers(resp):
    try:
        items = resp.headers.items()
    except AttributeError:
        items = resp.getheaders()
    return {k.lower(): v for k, v in items}


def _post(server, body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        server + TOKEN_PATH, data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None), _headers(resp)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null"), _headers(e)
        except Exception:
            return e.code, None, _headers(e)


def _post_raw(server, raw, content_type="application/json"):
    hdrs = {}
    if content_type is not None:
        hdrs["Content-Type"] = content_type
    req = urllib.request.Request(server + TOKEN_PATH, data=raw, headers=hdrs, method="POST")
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


def _get_nofollow(server, path):
    parsed = urllib.parse.urlparse(server)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    headers = {k.lower(): v for k, v in resp.getheaders()}
    status = resp.status
    conn.close()
    return status, headers, body


# ── token exchange validation ────────────────────────────────────────


def test_bad_grant_is_400(server):
    status, body, headers = _post(server, {"grant_type": "bogus"})
    assert status == 400
    assert "detail" in body
    assert "no-store" in headers.get("cache-control", "")


def test_missing_fields_are_400(server):
    cases = [
        {"grant_type": "authorization_code", "code": "x"},
        {"grant_type": "authorization_code", "code_verifier": "v" * 64},
        {"grant_type": "authorization_code"},
        {"grant_type": "refresh_token"},
        {"grant_type": "refresh_token", "refresh_token": ""},
    ]
    for body in cases:
        status, _, _ = _post(server, body)
        assert status == 400, body


def test_unknown_code_is_401(server):
    status, body, headers = _post(
        server,
        {"grant_type": "authorization_code", "code": "nope", "code_verifier": "v" * 64},
    )
    assert status == 401
    assert "detail" in body
    assert "no-store" in headers.get("cache-control", "")


def test_unknown_refresh_token_is_401(server):
    status, body, _ = _post(
        server, {"grant_type": "refresh_token", "refresh_token": "nope"}
    )
    assert status == 401
    assert "detail" in body


@pytest.mark.parametrize("body", [[1, 2], "x", 42, True, None])
def test_non_object_body_is_400(server, body):
    status, _, _ = _post(server, body)
    assert status == 400


def test_invalid_json_is_400(server):
    status, body, _ = _post_raw(server, b"{not json")
    assert status == 400
    assert "detail" in body


def test_pkce_challenge_shape(server):
    # the S256 helper the login gate expects: 64-char verifier digests
    # to a 43-char challenge (documents the contract the 400s below guard)
    assert len(_s256("v" * 64)) == 43
    status, _, _ = _post(
        server,
        {"grant_type": "authorization_code", "code": "nope", "code_verifier": "v" * 64},
    )
    assert status == 401


# ── native login parameter gate ──────────────────────────────────────


def test_login_native_param_validation(server):
    status, _, _ = _get_nofollow(server, "/auth/login?client=native")
    assert status == 400
    status, _, _ = _get_nofollow(
        server,
        "/auth/login?client=native&code_challenge=" + "c" * 50
        + "&code_challenge_method=plain",
    )
    assert status == 400


def test_login_native_valid_redirect(server):
    challenge = _s256("verifier-" + "x" * 50)
    status, headers, _ = _get_nofollow(
        server,
        "/auth/login?client=native&code_challenge=" + challenge
        + "&code_challenge_method=S256&state=st9",
    )
    assert status in (302, 303, 307)
    assert "accounts.google.com" in headers.get("location", "")


def test_login_browser_redirect(server):
    status, headers, _ = _get_nofollow(server, "/auth/login")
    assert status in (302, 303, 307)
    assert "accounts.google.com" in headers.get("location", "")
