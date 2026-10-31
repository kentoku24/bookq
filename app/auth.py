"""Google OAuth 2.0 authentication and session management.

Uses starlette's SessionMiddleware for session storage (already configured in main.py).
Auth state is stored in request.session, which uses its own signed cookie.

Set BOOKQ_TEST_MODE=true to bypass auth in test environments.
"""

import os

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse
from authlib.integrations.starlette_client import OAuth, OAuthError
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import OctKey
from starlette.requests import HTTPConnection

from app.config import Settings

router = APIRouter(prefix="/auth", tags=["auth"])

oauth = OAuth()

_TEST_MODE = os.getenv("BOOKQ_TEST_MODE", "").lower() == "true"
_BOOKQ_ENV = os.getenv("BOOKQ_ENV", "").strip().lower()

# Test mode bypasses authentication completely. The production deployment
# sets BOOKQ_ENV explicitly so a leaked/misconfigured test flag fails closed
# before the application starts serving requests.
if _TEST_MODE and _BOOKQ_ENV == "production":
    raise RuntimeError(
        "BOOKQ_TEST_MODE=true is forbidden when BOOKQ_ENV=production; refusing to start"
    )


def init_oauth(settings: Settings):
    """Register Google OAuth client. Call once at app startup."""
    oauth.register(
        name="google",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


def user_from_connection(connection: HTTPConnection) -> dict | None:
    """Shared signed-cookie user extraction for HTTP and WebSocket scopes.

    SessionMiddleware populates ``scope["session"]`` for both scope types, so
    Request (HTTP) and WebSocket reach the same session here — the WS endpoint
    uses this instead of the Request-based dependency. The TEST_MODE check
    lives here exactly once (#78: do not duplicate it in transport code).

    Auth order: signed session cookie first, then `Authorization: Bearer`
    (for native clients that can't do the cookie OAuth dance). Both paths
    return the same user dict shape.
    """
    if _TEST_MODE:
        return {"email": "test@example.com", "name": "Test User", "picture": ""}
    user = connection.session.get("user")
    if user is not None:
        return user
    return _user_from_bearer(connection)


def _user_from_bearer(connection: HTTPConnection) -> dict | None:
    """Validate `Authorization: Bearer <jwt>` (HS256, SECRET_KEY-signed).

    Claims: ``sub`` (email) and ``exp`` are required; ``name``/``picture``
    optional. Issued by /api/v1/auth/token for native clients; this side
    only validates. Mirrors the cookie path's allowlist policy: the
    configured set is required, an empty set rejects everyone.
    """
    scheme, _, token = connection.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    settings: Settings = connection.app.state.settings
    if not settings.secret_key:
        return None
    try:
        key = OctKey.import_key(settings.secret_key)
        claims = jwt.decode(token.strip(), key).claims
        jwt.JWTClaimsRegistry().validate(claims)
    except (JoseError, ValueError):
        return None
    if claims.get("exp") is None:  # tokens without expiry are not accepted
        return None
    email = (claims.get("sub") or "").lower()
    if not email:
        return None
    # Same allowlist policy as the OAuth callback: empty ALLOWED_EMAILS
    # denies everyone (fail-closed), never fail-open.
    if email not in settings.allowed_emails_set:
        return None
    return {
        "email": email,
        "name": claims.get("name") or email,
        "picture": claims.get("picture") or "",
    }


async def get_current_user(request: Request) -> dict | None:
    """FastAPI dependency: get current user from session. Returns None if not authenticated.
    In test mode, returns a mock user.
    """
    return user_from_connection(request)


async def require_auth(request: Request) -> dict:
    """FastAPI dependency: require authentication. Raises 401 if not authenticated."""
    user = user_from_connection(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


# ── Auth routes ────────────────────────────────────────────────────────


def _native_login_params(request: Request) -> dict | None:
    """Parse + validate the `?client=native` PKCE login parameters.

    Returns ``{code_challenge, state}`` for a native request, ``None`` for a
    browser login. RFC 8252 requires S256 — `plain` is rejected. Validation
    failures raise 400 before the OAuth redirect is attempted.
    """
    q = request.query_params
    if q.get("client") != "native":
        return None
    challenge = q.get("code_challenge", "")
    method = q.get("code_challenge_method", "S256")
    state = q.get("state", "")
    # RFC 7636: unencoded verifier is 43..128 chars; the S256 challenge is
    # its base64url digest (43 chars). Bound, don't normalize.
    if not (43 <= len(challenge) <= 128):
        raise HTTPException(status_code=400, detail="invalid code_challenge")
    if method != "S256":
        raise HTTPException(status_code=400, detail="code_challenge_method must be S256")
    if len(state) > 256:
        raise HTTPException(status_code=400, detail="invalid state")
    return {"code_challenge": challenge, "state": state}


@router.get("/login")
async def login(request: Request):
    """Redirect to Google OAuth consent screen.

    ``?client=native`` runs the RFC 8252 flow: the PKCE challenge (and the
    client's CSRF ``state``) rides in the signed session cookie through the
    OAuth round-trip, and the callback redirects to ``bookq://auth`` with a
    one-time exchange code instead of setting the session user.
    """
    settings: Settings = request.app.state.settings
    native = _native_login_params(request)
    if native is not None:
        request.session["bookq_native_pkce"] = native
    redirect_uri = settings.base_url.rstrip("/") + "/auth/callback" if settings.base_url else str(request.url_for("auth_callback"))
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/callback", name="auth_callback")
async def auth_callback(request: Request):
    """Handle Google OAuth callback: exchange code for token, verify user, store in session."""
    settings: Settings = request.app.state.settings

    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError:
        return RedirectResponse(url="/?error=access_denied", status_code=303)

    userinfo = token.get("userinfo")
    if not userinfo:
        raise HTTPException(status_code=502, detail="Auth provider error: no userinfo")

    email = (userinfo.get("email") or "").lower()
    if email not in settings.allowed_emails_set:
        request.session.clear()
        raise HTTPException(status_code=403, detail="Email not allowed")

    pending_native = request.session.pop("bookq_native_pkce", None)
    if pending_native is not None:
        # Native flow: no session cookie — hand the app a one-time code
        # bound to its PKCE challenge; it exchanges it at /api/v1/auth/token.
        from app.auth_token import issue_auth_code

        code = issue_auth_code(
            request.app.state,
            email=email,
            name=userinfo.get("name", email),
            picture=userinfo.get("picture", ""),
            code_challenge=pending_native["code_challenge"],
        )
        params = {"code": code}
        if pending_native["state"]:
            params["state"] = pending_native["state"]
        from urllib.parse import urlencode

        return RedirectResponse(url=f"bookq://auth?{urlencode(params)}", status_code=303)

    request.session["user"] = {
        "email": email,
        "name": userinfo.get("name", email),
        "picture": userinfo.get("picture", ""),
    }

    return RedirectResponse(url="/", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    """Clear session and redirect to home."""
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)
