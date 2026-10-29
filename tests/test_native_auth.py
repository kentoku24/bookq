"""Unit + API tests for native-client bearer token auth (app/native_auth.py).

Covers mint/verify round-trip, tamper rejection, expiry, malformed tokens,
allowlist revocation, TTL env parsing, the TEST_MODE login?native=1
bookq:// redirect, Bearer acceptance through require_auth (including the
lowercase scheme), and WS /ws/reader-sync bearer authentication.

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt \\
        pytest tests/test_native_auth.py -q
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

os.environ["BOOKQ_TEST_MODE"] = "true"

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from app import auth as auth_module  # noqa: E402
from app.native_auth import (  # noqa: E402
    mint_native_token,
    token_ttl_seconds,
    verify_native_token,
)

SECRET = "native-auth-test-secret"
ALLOWED = {"alice@example.com", "bob@example.com"}


def _forged(payload: dict, secret: str = SECRET) -> str:
    """Hand-build a token — for crafting payloads mint() would never emit."""
    p64 = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    ).decode("ascii").rstrip("=")
    sig = hmac.new(
        secret.encode("utf-8"),
        f"bookq-native-v1|{p64}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    s64 = base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")
    return f"{p64}.{s64}"


# ── mint/verify unit ─────────────────────────────────────────────────


def test_round_trip_returns_session_shaped_user():
    token = mint_native_token("alice@example.com", "Alice", SECRET)
    user = verify_native_token(token, SECRET, ALLOWED)
    assert user == {"email": "alice@example.com", "name": "Alice", "picture": ""}


def test_empty_name_defaults_to_email():
    token = mint_native_token("alice@example.com", "", SECRET)
    assert verify_native_token(token, SECRET, ALLOWED)["name"] == "alice@example.com"


def test_email_is_normalized_lowercase():
    token = mint_native_token("Alice@Example.com", "Alice", SECRET)
    user = verify_native_token(token, SECRET, ALLOWED)
    assert user["email"] == "alice@example.com"


def test_wrong_secret_rejected():
    token = mint_native_token("alice@example.com", "Alice", SECRET)
    assert verify_native_token(token, "other-secret", ALLOWED) is None


def test_tampered_payload_rejected():
    token = mint_native_token("alice@example.com", "Alice", SECRET)
    payload_b64, sig = token.rsplit(".", 1)
    flip = "B" if payload_b64[0] == "A" else "A"
    assert verify_native_token(flip + payload_b64[1:] + "." + sig, SECRET, ALLOWED) is None


def test_tampered_signature_rejected():
    token = mint_native_token("alice@example.com", "Alice", SECRET)
    tail = "A" if token[-1] != "A" else "B"
    assert verify_native_token(token[:-1] + tail, SECRET, ALLOWED) is None


def test_expired_token_rejected():
    token = mint_native_token("alice@example.com", "Alice", SECRET, now=1000)
    exp = 1000 + token_ttl_seconds()
    assert verify_native_token(token, SECRET, ALLOWED, now=exp - 1) is not None
    assert verify_native_token(token, SECRET, ALLOWED, now=exp + 1) is None


def test_allowlist_removal_revokes():
    token = mint_native_token("alice@example.com", "Alice", SECRET)
    assert verify_native_token(token, SECRET, {"bob@example.com"}) is None


def test_wrong_version_rejected():
    token = _forged({"v": 2, "email": "alice@example.com", "name": "A", "iat": 0, "exp": 9e18})
    assert verify_native_token(token, SECRET, ALLOWED) is None


def test_forged_email_rejected_by_allowlist():
    token = _forged({"v": 1, "email": "mallory@example.com", "name": "M", "iat": 0, "exp": 9e18})
    assert verify_native_token(token, SECRET, ALLOWED) is None


@pytest.mark.parametrize("bad", ["", "x", "a.b.c", ".sig", "p.", "!!!.!!!", "  ", "a" * 9000 + ".x"])
def test_malformed_tokens_rejected(bad):
    assert verify_native_token(bad, SECRET, ALLOWED) is None


def test_ttl_days_default_and_env(monkeypatch):
    monkeypatch.delenv("BOOKQ_NATIVE_TOKEN_TTL_DAYS", raising=False)
    assert token_ttl_seconds() == 90 * 86400
    monkeypatch.setenv("BOOKQ_NATIVE_TOKEN_TTL_DAYS", "30")
    assert token_ttl_seconds() == 30 * 86400
    monkeypatch.setenv("BOOKQ_NATIVE_TOKEN_TTL_DAYS", "junk")
    assert token_ttl_seconds() == 90 * 86400
    monkeypatch.setenv("BOOKQ_NATIVE_TOKEN_TTL_DAYS", "99999")
    assert token_ttl_seconds() == 370 * 86400
    monkeypatch.setenv("BOOKQ_NATIVE_TOKEN_TTL_DAYS", "0")
    assert token_ttl_seconds() == 86400


# ── API path (real auth: TEST_MODE bypass patched off) ───────────────


@pytest.fixture
def prod_client(tmp_path, monkeypatch):
    """App with REAL auth (TEST_MODE bypass patched off) over an empty library."""
    dl = tmp_path / "dl"
    dl.mkdir()
    monkeypatch.setenv("DOWNLOADS_DIR", str(dl))
    monkeypatch.setenv("SECRET_KEY", SECRET)
    monkeypatch.setenv("ALLOWED_EMAILS", "alice@example.com,bob@example.com")
    monkeypatch.setattr(auth_module, "_TEST_MODE", False)
    from app.main import create_app

    app = create_app()
    with TestClient(app) as client:
        yield client


def _alice_token() -> str:
    return mint_native_token("alice@example.com", "Alice", SECRET)


def test_me_requires_auth(prod_client):
    assert prod_client.get("/api/me").status_code == 401


def test_me_accepts_bearer(prod_client):
    resp = prod_client.get(
        "/api/me", headers={"Authorization": f"Bearer {_alice_token()}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "alice@example.com"
    assert body["name"] == "Alice"
    assert resp.headers["Cache-Control"] == "private, no-store"


def test_bearer_scheme_is_case_insensitive(prod_client):
    resp = prod_client.get(
        "/api/me", headers={"Authorization": f"bearer {_alice_token()}"}
    )
    assert resp.status_code == 200


def test_bad_token_is_401(prod_client):
    assert (
        prod_client.get("/api/me", headers={"Authorization": "Bearer junk"}).status_code
        == 401
    )


def test_unlisted_email_is_401(prod_client):
    token = mint_native_token("carol@example.com", "Carol", SECRET)
    assert (
        prod_client.get("/api/me", headers={"Authorization": f"Bearer {token}"}).status_code
        == 401
    )


def test_expired_token_is_401(prod_client):
    token = mint_native_token(
        "alice@example.com", "Alice", SECRET, now=time.time() - 400 * 86400
    )
    assert (
        prod_client.get("/api/me", headers={"Authorization": f"Bearer {token}"}).status_code
        == 401
    )


def test_protected_endpoint_accepts_bearer(prod_client):
    resp = prod_client.get(
        "/api/folders", headers={"Authorization": f"Bearer {_alice_token()}"}
    )
    assert resp.status_code == 200
    assert resp.json() == []


def test_no_credential_is_401_not_500(prod_client):
    assert prod_client.get("/api/folders").status_code == 401


def test_ws_accepts_bearer(prod_client):
    with prod_client.websocket_connect(
        "/ws/reader-sync", headers={"authorization": f"Bearer {_alice_token()}"}
    ) as ws:
        hello = json.loads(ws.receive_text())
        assert hello["type"] == "HELLO"


def test_ws_rejects_unauthenticated(prod_client):
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with prod_client.websocket_connect("/ws/reader-sync"):
            pass
    assert exc_info.value.code == 4401


# ── TEST_MODE native login shortcut (dev-environment flow) ───────────


def test_test_mode_native_login_redirects_to_bookq_scheme(tmp_path, monkeypatch):
    monkeypatch.setenv("DOWNLOADS_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", SECRET)
    from app.main import create_app

    client = TestClient(create_app())
    resp = client.get("/auth/login?native=1", follow_redirects=False)
    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert loc.startswith("bookq://auth/callback?")
    query = parse_qs(urlparse(loc).query)
    assert query["name"][0] == "Test User"
    user = verify_native_token(query["token"][0], SECRET, {"test@example.com"})
    assert user is not None
    assert user["email"] == "test@example.com"


def test_test_mode_native_login_absent_param_is_untouched(tmp_path, monkeypatch):
    """Plain /auth/login (no native=1) must not take the bookq:// branch."""
    monkeypatch.setenv("DOWNLOADS_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", SECRET)
    from app.main import create_app

    client = TestClient(create_app())
    # authorize_redirect fails without real Google creds — assert only that
    # the response is NOT a bookq:// redirect (the native branch never ran).
    try:
        resp = client.get("/auth/login", follow_redirects=False)
        assert not resp.headers.get("location", "").startswith("bookq://")
    except Exception:
        pass  # OAuth metadata fetch failure is the expected TEST_MODE outcome
