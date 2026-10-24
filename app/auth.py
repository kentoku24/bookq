"""Google OAuth 2.0 authentication and session management.

Uses starlette's SessionMiddleware for session storage (already configured in main.py).
Auth state is stored in request.session, which uses its own signed cookie.

Set BOOKQ_TEST_MODE=true to bypass auth in test environments.
"""

import os

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse
from authlib.integrations.starlette_client import OAuth, OAuthError

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


async def get_current_user(request: Request) -> dict | None:
    """FastAPI dependency: get current user from session. Returns None if not authenticated.
    In test mode, returns a mock user.
    """
    if _TEST_MODE:
        return {"email": "test@example.com", "name": "Test User", "picture": ""}
    return request.session.get("user")


async def require_auth(request: Request) -> dict:
    """FastAPI dependency: require authentication. Raises 401 if not authenticated."""
    if _TEST_MODE:
        return {"email": "test@example.com", "name": "Test User", "picture": ""}
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


# ── Auth routes ────────────────────────────────────────────────────────


@router.get("/login")
async def login(request: Request):
    """Redirect to Google OAuth consent screen."""
    settings: Settings = request.app.state.settings
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
