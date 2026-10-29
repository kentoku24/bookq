"""Native-client bearer tokens (iOS app auth).

The browser path ends at a signed session cookie; a native client cannot
carry that cookie out of ASWebAuthenticationSession (its cookies never
reach the app's URLSession), so the OAuth callback instead mints an
HMAC-signed bearer token and hands it back through the ``bookq://``
custom-scheme redirect. Every authenticated endpoint accepts it via
``Authorization: Bearer`` through the same ``user_from_connection``
chokepoint the session cookie already uses.

Token wire format::

    b64url(payload_json) + "." + b64url(HMAC-SHA256(SECRET_KEY,
                                                  "bookq-native-v1|" + payload_b64))

payload = ``{"v": 1, "email": str, "name": str, "iat": int, "exp": int}``

Revocation story (no server-side token list): expiry does the routine
work, allowlist membership is re-checked at verify time (dropping an
email from ``ALLOWED_EMAILS`` kills its outstanding tokens), and rotating
``SECRET_KEY`` invalidates everything at once.
"""

import base64
import hashlib
import hmac
import json
import os
import time

_TOKEN_VERSION = 1
_DEFAULT_TTL_DAYS = 90
_MAX_TTL_DAYS = 370  # sanity bound on the env override
_MAX_TOKEN_LEN = 8192


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def token_ttl_seconds() -> int:
    """Token lifetime from ``BOOKQ_NATIVE_TOKEN_TTL_DAYS`` (default 90d, clamp 1..370)."""
    raw = os.getenv("BOOKQ_NATIVE_TOKEN_TTL_DAYS", "").strip()
    try:
        days = int(raw) if raw else _DEFAULT_TTL_DAYS
    except ValueError:
        days = _DEFAULT_TTL_DAYS
    days = max(1, min(days, _MAX_TTL_DAYS))
    return days * 86400


def _sign(secret_key: str, payload_b64: str) -> str:
    digest = hmac.new(
        secret_key.encode("utf-8"),
        f"bookq-native-v1|{payload_b64}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    return _b64url(digest)


def mint_native_token(email: str, name: str, secret_key: str, now: float | None = None) -> str:
    """Mint a bearer token for *email*; the OAuth callback hands it to the app."""
    now = time.time() if now is None else now
    payload = {
        "v": _TOKEN_VERSION,
        "email": email,
        "name": name,
        "iat": int(now),
        "exp": int(now) + token_ttl_seconds(),
    }
    payload_b64 = _b64url(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    )
    return f"{payload_b64}.{_sign(secret_key, payload_b64)}"


def verify_native_token(
    token: str,
    secret_key: str,
    allowed_emails,
    now: float | None = None,
) -> dict | None:
    """Return the session-shaped user dict for a valid token, else None.

    Signature is checked first (a forged payload can never reach the parse
    step), then expiry, then allowlist membership — re-checked here so
    removing an email from ``ALLOWED_EMAILS`` revokes outstanding tokens.
    Malformed input always returns None; never raises.
    """
    try:
        if not token or len(token) > _MAX_TOKEN_LEN:
            return None
        payload_b64, sep, sig = token.partition(".")
        if not sep or not payload_b64 or not sig:
            return None
        if not hmac.compare_digest(_sign(secret_key, payload_b64), sig):
            return None
        payload = json.loads(_b64url_decode(payload_b64))
        if not isinstance(payload, dict) or payload.get("v") != _TOKEN_VERSION:
            return None
        email = payload.get("email")
        exp = payload.get("exp")
        if not isinstance(email, str) or not isinstance(exp, (int, float)):
            return None
        now = time.time() if now is None else now
        if exp <= now:
            return None
        email = email.lower()
        if email not in allowed_emails:
            return None
        name = payload.get("name")
        return {"email": email, "name": name if isinstance(name, str) and name else email,
                "picture": ""}
    except Exception:
        return None
