"""Native PKCE token flow — /api/v1/auth/token + ?client=native login (#190).

Covers the auth-code -> Bearer exchange (one-time use, PKCE S256 verify,
expiry), refresh-token rotation (old token dies), the JWT claims shape the
#187 bearer validator consumes, and the login->callback native redirect
with the OAuth client mocked out.
"""

import base64
import hashlib
import time
from types import SimpleNamespace

import pytest
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

SECRET = "token-test-secret-key-32-chars-long!!"
ALICE = "alice@example.com"


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DOWNLOADS_DIR", str(tmp_path / "dl"))
    (tmp_path / "dl").mkdir()
    monkeypatch.setenv("BOOKQ_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SECRET_KEY", SECRET)
    monkeypatch.setenv("ALLOWED_EMAILS", ALICE)
    monkeypatch.delenv("BOOKQ_TEST_MODE", raising=False)
    from app.main import create_app

    # https base_url: SessionMiddleware emits Secure cookies, which httpx
    # only sends back over https.
    return TestClient(create_app(), base_url="https://testserver")


def _seed_code(app, email=ALICE, challenge="chall", exp=None):
    from app.auth_token import issue_auth_code

    code = issue_auth_code(
        app.state, email=email, name="Alice", picture="",
        code_challenge=challenge,
    )
    if exp is not None:
        app.state.auth_codes[code]["exp"] = exp
    return code


def _exchange(client, **body):
    return client.post("/api/v1/auth/token", json=body)


def test_code_exchange_issues_tokens(client):
    code = _seed_code(client.app, challenge=_s256("v" * 64))
    resp = _exchange(
        client, grant_type="authorization_code",
        code=code, code_verifier="v" * 64,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 3600
    assert body["access_token"] and body["refresh_token"]


def test_access_token_accepted_by_bearer_validator(client):
    """The issued JWT must satisfy _user_from_bearer (sub/email/exp + allowlist)."""
    code = _seed_code(client.app, challenge=_s256("v" * 64))
    body = _exchange(
        client, grant_type="authorization_code",
        code=code, code_verifier="v" * 64,
    ).json()

    from app.auth import _user_from_bearer

    conn = SimpleNamespace(
        headers={"authorization": f"Bearer {body['access_token']}"},
        app=SimpleNamespace(
            state=SimpleNamespace(settings=client.app.state.settings)
        ),
    )
    user = _user_from_bearer(conn)
    assert user["email"] == ALICE


def test_code_is_one_time(client):
    code = _seed_code(client.app, challenge=_s256("v" * 64))
    ok = _exchange(client, grant_type="authorization_code",
                   code=code, code_verifier="v" * 64)
    assert ok.status_code == 200
    again = _exchange(client, grant_type="authorization_code",
                      code=code, code_verifier="v" * 64)
    assert again.status_code == 401


def test_wrong_verifier_rejected_and_code_consumed(client):
    code = _seed_code(client.app, challenge=_s256("right"))
    resp = _exchange(client, grant_type="authorization_code",
                     code=code, code_verifier="wrong")
    assert resp.status_code == 401
    # even the correct verifier can't retry — the code was consumed.
    resp = _exchange(client, grant_type="authorization_code",
                     code=code, code_verifier="right")
    assert resp.status_code == 401


def test_unknown_and_expired_code_rejected(client):
    resp = _exchange(client, grant_type="authorization_code",
                     code="nope", code_verifier="v" * 64)
    assert resp.status_code == 401
    code = _seed_code(client.app, challenge=_s256("v" * 64),
                      exp=time.time() - 1)
    resp = _exchange(client, grant_type="authorization_code",
                     code=code, code_verifier="v" * 64)
    assert resp.status_code == 401


def test_refresh_rotation(client):
    code = _seed_code(client.app, challenge=_s256("v" * 64))
    first = _exchange(client, grant_type="authorization_code",
                      code=code, code_verifier="v" * 64).json()
    second = _exchange(client, grant_type="refresh_token",
                       refresh_token=first["refresh_token"])
    assert second.status_code == 200
    assert second.json()["refresh_token"] != first["refresh_token"]
    # the presented (now-rotated-out) refresh token is dead
    replay = _exchange(client, grant_type="refresh_token",
                       refresh_token=first["refresh_token"])
    assert replay.status_code == 401


def test_bad_grant_and_missing_fields(client):
    resp = _exchange(client, grant_type="bogus")
    assert resp.status_code == 400
    resp = _exchange(client, grant_type="authorization_code", code="x")
    assert resp.status_code == 400
    resp = _exchange(client, grant_type="refresh_token")
    assert resp.status_code == 400


def test_login_native_param_validation(client):
    bad = client.get("/auth/login?client=native", follow_redirects=False)
    assert bad.status_code == 400
    bad = client.get(
        "/auth/login?client=native&code_challenge="
        + "c" * 50
        + "&code_challenge_method=plain",
        follow_redirects=False,
    )
    assert bad.status_code == 400


class _FakeGoogle:
    """Stands in for authlib's OAuth client — no Google round-trip."""

    async def authorize_redirect(self, request, redirect_uri=None, **kwargs):
        return RedirectResponse("https://accounts.google.com/fake-consent")

    async def authorize_access_token(self, request):
        return {"userinfo": {"email": ALICE, "name": "Alice", "picture": ""}}


def test_native_login_callback_code_flow(client, monkeypatch):
    """login?client=native stashes the PKCE challenge; the callback redirects
    to bookq://auth with a code that exchanges for real tokens."""
    import app.auth as auth_module

    monkeypatch.setattr(auth_module.oauth, "google", _FakeGoogle())

    challenge = _s256("verifier-" + "x" * 50)
    resp = client.get(
        "/auth/login?client=native"
        f"&code_challenge={challenge}&code_challenge_method=S256&state=st9",
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303, 307)

    resp = client.get("/auth/callback", follow_redirects=False)
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("bookq://auth?")
    assert "state=st9" in location
    code = location.split("code=")[1].split("&")[0]

    resp = _exchange(client, grant_type="authorization_code",
                     code=code, code_verifier="verifier-" + "x" * 50)
    assert resp.status_code == 200
    assert resp.json()["access_token"]


def test_browser_login_unaffected(client, monkeypatch):
    """Plain /auth/login (no client=native) must not set the native stash."""
    import app.auth as auth_module

    monkeypatch.setattr(auth_module.oauth, "google", _FakeGoogle())
    client.get("/auth/login", follow_redirects=False)
    resp = client.get("/auth/callback", follow_redirects=False)
    # browser flow completes as before: session user + redirect to /
    assert resp.headers["location"] == "/"
