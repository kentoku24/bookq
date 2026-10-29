"""Google OAuth 2.0 authentication and session management.

Uses starlette's SessionMiddleware for session storage (already configured in main.py).
Auth state is stored in request.session, which uses its own signed cookie.

Set BOOKQ_TEST_MODE=true to bypass auth in test environments.
"""

import os
from urllib.parse import quote

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse
from authlib.integrations.starlette_client import OAuth, OAuthError
from starlette.requests import HTTPConnection

from app.config import Settings
from app.native_auth import mint_native_token, verify_native_token

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


def _bearer_user(connection: HTTPConnection) -> dict | None:
    """Verify ``Authorization: Bearer <native-token>`` (native clients only).

    The signed-cookie session is unreachable from a native app — its OAuth
    completes inside ASWebAuthenticationSession, whose cookies never reach
    the app's URLSession — so the OAuth callback mints an HMAC token that
    every API + the WS endpoint accept here through the same chokepoint.
    Same fail-closed posture as the cookie path: any problem -> None and
    the session cookie gets its usual say.
    """
    authz = connection.headers.get("authorization", "")
    scheme, _, token = authz.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    try:
        settings = connection.app.state.settings
    except Exception:
        return None
    return verify_native_token(
        token.strip(), settings.secret_key, settings.allowed_emails_set
    )


def user_from_connection(connection: HTTPConnection) -> dict | None:
    """Shared signed-cookie user extraction for HTTP and WebSocket scopes.

    SessionMiddleware populates ``scope["session"]`` for both scope types, so
    Request (HTTP) and WebSocket reach the same session here — the WS endpoint
    uses this instead of the Request-based dependency. The TEST_MODE check
    lives here exactly once (#78: do not duplicate it in transport code).
    """
    if _TEST_MODE:
        return {"email": "test@example.com", "name": "Test User", "picture": ""}
    bearer = _bearer_user(connection)
    if bearer is not None:
        return bearer
    return connection.session.get("user")


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


def _native_redirect_url(token: str, name: str) -> str:
    """``bookq://`` callback URL carrying the minted bearer token.

    ASWebAuthenticationSession intercepts this redirect in-process — the
    token never transits an app WebView or a server log. The token is
    b64url already; the display name may carry unicode/spaces.
    """
    return (
        f"bookq://auth/callback?token={quote(token, safe='')}"
        f"&name={quote(name, safe='')}"
    )


@router.get("/login")
async def login(request: Request):
    """Redirect to Google OAuth consent screen.

    ``?native=1`` marks the flow for a native client: the callback then
    answers with a Bearer token through the ``bookq://`` custom scheme
    instead of a session cookie. TEST_MODE short-circuits Google entirely
    so dev environments can still exercise the app's token-receive path.
    """
    settings: Settings = request.app.state.settings
    redirect_uri = settings.base_url.rstrip("/") + "/auth/callback" if settings.base_url else str(request.url_for("auth_callback"))
    if request.query_params.get("native") == "1":
        if _TEST_MODE:
            email = "test@example.com"
            name = "Test User"
            token = mint_native_token(email, name, settings.secret_key)
            return RedirectResponse(
                url=_native_redirect_url(token, name), status_code=303
            )
        request.session["native_client"] = True
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

    if request.session.pop("native_client", False):
        # Native-client flow: hand the credential back as a bearer token via
        # the custom scheme rather than a session cookie the app can't carry.
        request.session.clear()
        name = userinfo.get("name", email)
        token = mint_native_token(email, name, settings.secret_key)
        return RedirectResponse(
            url=_native_redirect_url(token, name), status_code=303
        )

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
