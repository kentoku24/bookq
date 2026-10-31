"""Native-client token issuance — RFC 8252 PKCE flow -> Bearer JWT.

Implements the `/api/v1/auth/token` contract from api/openapi.v1.yaml:
`/auth/login?client=native` rides the normal Google OAuth redirect but,
instead of a session cookie, the callback mints a one-time `code` bound to
the caller's PKCE `code_challenge` and redirects to `bookq://auth?code=…`.
The native app exchanges it here for a short-lived HS256 access token (the
same JWT shape `_user_from_bearer` validates) plus a rotating refresh
token. Both stores live on app.state — single worker assumed, restart
invalidates everything (same posture as reading_positions).
"""

import base64
import hashlib
import hmac
import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from joserfc import jwt
from joserfc.jwk import OctKey

token_router = APIRouter(tags=["auth"])

ACCESS_TTL_S = 3600
CODE_TTL_S = 120
REFRESH_TTL_S = 30 * 86400
# Upper bound on each store — a leak/bug must not grow them unboundedly.
_MAX_ENTRIES = 10_000


def _codes(app_state) -> dict:
    codes = getattr(app_state, "auth_codes", None)
    if not isinstance(codes, dict):
        codes = {}
        app_state.auth_codes = codes
    return codes


def _refresh_tokens(app_state) -> dict:
    tokens = getattr(app_state, "refresh_tokens", None)
    if not isinstance(tokens, dict):
        tokens = {}
        app_state.refresh_tokens = tokens
    return tokens


def _evict_expired(store: dict) -> None:
    now = time.time()
    for k in [k for k, v in store.items() if v["exp"] <= now]:
        del store[k]
    if len(store) > _MAX_ENTRIES:
        oldest = min(store, key=lambda k: store[k]["exp"])
        del store[oldest]


def issue_auth_code(app_state, *, email: str, name: str, picture: str,
                    code_challenge: str) -> str:
    """Mint a one-time auth code bound to a PKCE S256 challenge."""
    codes = _codes(app_state)
    _evict_expired(codes)
    code = secrets.token_urlsafe(32)
    codes[code] = {
        "email": email,
        "name": name,
        "picture": picture,
        "challenge": code_challenge,
        "exp": time.time() + CODE_TTL_S,
    }
    return code


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _mint_access(secret_key: str, email: str, name: str, picture: str) -> str:
    now = int(time.time())
    claims = {
        "sub": email,
        "name": name,
        "picture": picture,
        "iat": now,
        "exp": now + ACCESS_TTL_S,
    }
    return jwt.encode({"alg": "HS256"}, claims, OctKey.import_key(secret_key))


def _issue_pair(app_state, settings, principal: dict) -> dict:
    tokens = _refresh_tokens(app_state)
    _evict_expired(tokens)
    refresh = secrets.token_urlsafe(48)
    tokens[refresh] = {
        "email": principal["email"],
        "name": principal["name"],
        "picture": principal["picture"],
        "exp": time.time() + REFRESH_TTL_S,
    }
    return {
        "access_token": _mint_access(
            settings.secret_key,
            principal["email"],
            principal["name"],
            principal["picture"],
        ),
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL_S,
        "refresh_token": refresh,
    }


def exchange_token(app_state, settings, body) -> tuple[int, dict]:
    """Consume an auth code (PKCE-verified) or refresh token; return (status, payload)."""
    if not isinstance(body, dict):
        return 400, {"detail": "expected JSON object"}
    grant = body.get("grant_type")
    if grant == "authorization_code":
        code, verifier = body.get("code"), body.get("code_verifier")
        if not isinstance(code, str) or not code or not isinstance(verifier, str) or not verifier:
            return 400, {"detail": "code and code_verifier required"}
        # Pop first: a code is one-time even when the verifier is wrong —
        # guessing attempts must not get a second shot at the same code.
        entry = _codes(app_state).pop(code, None)
        if entry is None or entry["exp"] <= time.time():
            return 401, {"detail": "invalid or expired code"}
        try:
            ok = hmac.compare_digest(_s256(verifier), entry["challenge"])
        except (UnicodeEncodeError, ValueError):
            ok = False
        if not ok:
            return 401, {"detail": "PKCE verification failed"}
    elif grant == "refresh_token":
        token = body.get("refresh_token")
        if not isinstance(token, str) or not token:
            return 400, {"detail": "refresh_token required"}
        # Rotation: the presented token is consumed whether or not it was
        # still valid — a replayed old token dies, never revives.
        entry = _refresh_tokens(app_state).pop(token, None)
        if entry is None or entry["exp"] <= time.time():
            return 401, {"detail": "invalid or expired refresh_token"}
    else:
        return 400, {"detail": "unsupported grant_type"}
    return 200, _issue_pair(app_state, settings, entry)


@token_router.post("/api/v1/auth/token")
async def post_auth_token(request: Request):
    """Exchange an auth code (PKCE) or refresh token for a Bearer token.

    Public endpoint (no session auth — it *is* the auth bootstrap).
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    status, payload = exchange_token(request.app.state, request.app.state.settings, body)
    return JSONResponse(
        content=payload,
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )
