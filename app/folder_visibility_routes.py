"""Per-user folder visibility API: GET/PUT /api/folder-visibility.

The DB user is always derived from the authenticated session email — request
bodies, query params and the owner header never select a user. The owner
header (a purpose-scoped HMAC token) is only compared against the value
computed for the session user, so a stale tab cannot write into an account
that was switched in another tab.
"""

import hmac
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.archive import is_archive_file
from app.auth import require_auth
from app.folder_visibility import (
    VisibilitySnapshot,
    user_key_for_email,
    validate_root,
    visibility_owner,
)
from app.routes import _is_within
from app.scanner import is_pdf_file

logger = logging.getLogger(__name__)

router = APIRouter(tags=["folder-visibility"])

OWNER_HEADER = "x-bookq-visibility-owner"
_PRIVATE_HEADERS = {"Cache-Control": "private, no-store", "Vary": "Cookie"}


def _err(status: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail=detail, headers=_PRIVATE_HEADERS)


def _user_key(user: dict) -> str:
    email = user.get("email") if isinstance(user, dict) else None
    key = user_key_for_email(email)
    if key is None:
        raise _err(403, "session has no usable identity")
    return key


def _store(request: Request):
    store = getattr(request.app.state, "folder_visibility", None)
    if store is None:
        raise _err(503, "visibility store unavailable")
    return store


async def _store_call(fn, *args):
    """Run one store operation off the event loop; failures -> generic 503.

    Only the exception type is logged: DB messages can carry SQL, local
    paths or user data, none of which belong in logs or the response.
    """
    try:
        return await run_in_threadpool(fn, *args)
    except Exception as exc:
        logger.warning("folder-visibility store failure type=%s", type(exc).__name__)
        raise _err(503, "visibility store unavailable")


def _classify_root(downloads_path: Path, root: str) -> str:
    """'dir'/'archive'/'pdf' for a supported top-level entry, else 'missing'/'outside'."""
    target = downloads_path / root
    if not _is_within(downloads_path, target):
        return "outside"
    if target.is_dir():
        return "dir"
    if is_archive_file(target):
        return "archive"
    if is_pdf_file(target):
        return "pdf"
    return "missing"


def _respond(owner: str, snap: VisibilitySnapshot) -> JSONResponse:
    return JSONResponse(
        content={
            "owner": owner,
            "revision": snap.revision,
            "hiddenRoots": list(snap.hidden_roots),
        },
        headers=_PRIVATE_HEADERS,
    )


@router.get("/api/folder-visibility")
async def get_folder_visibility(request: Request, user: dict = Depends(require_auth)):
    user_key = _user_key(user)
    owner = visibility_owner(user_key, request.app.state.settings.secret_key)
    snap = await _store_call(_store(request).read, user_key)
    return _respond(owner, snap)


@router.put("/api/folder-visibility")
async def put_folder_visibility(request: Request, user: dict = Depends(require_auth)):
    settings = request.app.state.settings
    user_key = _user_key(user)
    owner = visibility_owner(user_key, settings.secret_key)

    header_owner = request.headers.get(OWNER_HEADER)
    if header_owner is None:
        raise _err(403, "visibility owner header required")
    if not hmac.compare_digest(header_owner, owner):
        raise _err(409, "visibility owner mismatch")

    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise _err(415, "expected application/json")
    try:
        body = await request.json()
    except Exception:
        raise _err(400, "invalid JSON body")
    if not isinstance(body, dict):
        raise _err(400, "expected JSON object")
    if set(body) != {"root", "hidden"}:
        raise _err(400, "expected exactly 'root' and 'hidden'")
    hidden = body["hidden"]
    if not isinstance(hidden, bool):
        raise _err(400, "hidden must be a boolean")
    try:
        root = validate_root(body["root"])
    except ValueError:
        raise _err(400, "invalid root")

    store = _store(request)
    if hidden:
        snap = await _store_call(store.read, user_key)
        if root not in snap.hidden_roots:
            kind = await run_in_threadpool(_classify_root, settings.downloads_path, root)
            if kind == "outside":
                raise _err(403, "root is outside the library")
            if kind == "missing":
                raise _err(404, "no such top-level folder or archive")
    snap = await _store_call(store.set_hidden, user_key, root, hidden)
    return _respond(owner, snap)
