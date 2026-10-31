"""Bearer (JWT) auth for native clients — validation side only (#185).

`user_from_connection` is the single extraction point shared by HTTP and
WebSocket scopes: signed session cookie first, then `Authorization: Bearer`.
These tests pin that ordering and the token rules (HS256, SECRET_KEY-signed,
`sub`+`exp` required, allowlist mirrored from the cookie path).
"""

import time

import pytest
from joserfc import jwt
from joserfc.jwk import OctKey
from starlette.requests import HTTPConnection

import app.auth as auth
from app.config import load_settings

KEY = "unit-test-bearer-secret" + "x" * 40  # HS256 keys must be >= 32 bytes


def _token(*, key=KEY, exp_offset=3600, sub="reader@example.com", **claims):
    if exp_offset is not None:
        claims["exp"] = int(time.time()) + exp_offset
    if sub is not None:
        claims["sub"] = sub
    return jwt.encode({"alg": "HS256"}, claims, OctKey.import_key(key))


@pytest.fixture()
def app_scope(monkeypatch):
    """App with test-mode auth bypass off and a known signing key.

    The imported app object is process-global, so mutated settings are
    restored on teardown to avoid leaking state into other tests in the
    same pytest worker.
    """
    monkeypatch.setattr(auth, "_TEST_MODE", False)
    from app.main import app

    if not hasattr(app.state, "settings"):
        app.state.settings = load_settings()
    settings = app.state.settings
    saved = (settings.secret_key, list(settings.allowed_emails), settings.downloads_dir)
    settings.secret_key = KEY
    settings.allowed_emails = []
    yield app
    settings.secret_key, settings.allowed_emails, settings.downloads_dir = saved


def _conn(app, token=None, session=None):
    headers = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    return HTTPConnection(
        {"type": "http", "headers": headers, "session": session or {}, "app": app}
    )


def test_valid_bearer_token_returns_user(app_scope):
    user = auth.user_from_connection(_conn(app_scope, _token(name="Ken")))
    assert user == {"email": "reader@example.com", "name": "Ken", "picture": ""}


def test_wrong_signature_rejected(app_scope):
    assert auth.user_from_connection(_conn(app_scope, _token(key="other"))) is None


def test_expired_token_rejected(app_scope):
    assert auth.user_from_connection(_conn(app_scope, _token(exp_offset=-10))) is None


def test_missing_exp_rejected(app_scope):
    assert auth.user_from_connection(_conn(app_scope, _token(exp_offset=None))) is None


def test_missing_sub_rejected(app_scope):
    assert auth.user_from_connection(_conn(app_scope, _token(sub=None))) is None


def test_no_bearer_falls_back_to_session(app_scope):
    conn = _conn(app_scope, session={"user": {"email": "cookie@example.com"}})
    assert auth.user_from_connection(conn) == {"email": "cookie@example.com"}


def test_cookie_session_wins_over_bearer(app_scope):
    conn = _conn(app_scope, _token(), session={"user": {"email": "cookie@example.com"}})
    assert auth.user_from_connection(conn) == {"email": "cookie@example.com"}


def test_no_credentials_returns_none(app_scope):
    assert auth.user_from_connection(_conn(app_scope)) is None


def test_non_bearer_scheme_ignored(app_scope):
    conn = HTTPConnection(
        {
            "type": "http",
            "headers": [(b"authorization", b"Basic dXNlcjpwYXNz")],
            "session": {},
            "app": app_scope,
        }
    )
    assert auth.user_from_connection(conn) is None


def test_allowlist_enforced(app_scope):
    app_scope.state.settings.allowed_emails = ["other@example.com"]
    assert auth.user_from_connection(_conn(app_scope, _token())) is None

    app_scope.state.settings.allowed_emails = ["reader@example.com"]
    assert auth.user_from_connection(_conn(app_scope, _token())) is not None


def test_empty_secret_key_disables_bearer(app_scope):
    app_scope.state.settings.secret_key = ""
    assert auth.user_from_connection(_conn(app_scope, _token())) is None


def test_protected_endpoint_accepts_bearer(app_scope, tmp_path):
    from fastapi.testclient import TestClient

    with TestClient(app_scope) as client:
        app_scope.state.settings.secret_key = KEY  # lifespan reloads settings
        app_scope.state.settings.downloads_dir = str(tmp_path)
        r = client.get("/api/folders", headers={"Authorization": f"Bearer {_token()}"})
        assert r.status_code == 200
        assert client.get("/api/folders").status_code == 401
