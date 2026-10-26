"""Per-user folder visibility API: GET/PUT /api/folder-visibility.

The DB user is always derived from the authenticated session email — request
bodies, query params and the owner header never select a user. The owner
header (a purpose-scoped HMAC token) is only compared against the value
computed for the session user, so a stale tab cannot write into an account
that was switched in another tab.
"""

import hmac
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.auth import require_auth
from app.folder_visibility import (
    VisibilitySnapshot,
    user_key_for_email,
    validate_root,
    visibility_owner,
)
from app.routes import _ensure_tree_and_index

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


async def _top_level_folder_names(request: Request) -> set[str]:
    """``name``s of the current tree's top-level ``type == "folder"`` nodes.

    Hidden keys are top-level node identities, so a flattened scanner node
    like ``wrap/work`` validates by tree membership — never by probing the
    filesystem (``wrap`` alone is not a tree node and must not validate).
    """
    try:
        tree, _index = await _ensure_tree_and_index(request)
    except Exception as exc:
        logger.warning("folder-visibility tree unavailable type=%s", type(exc).__name__)
        raise _err(503, "library tree unavailable")
    return {
        n.get("name")
        for n in tree
        if isinstance(n, dict) and n.get("type") == "folder"
    }


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
    # compare_digest on a non-ASCII str raises TypeError — compare bytes:
    # header bytes decode as latin-1 per RFC 7230; the computed owner is
    # always ASCII hex. Anything unencodable simply mismatches.
    try:
        header_bytes = header_owner.encode("latin-1")
    except UnicodeEncodeError:
        header_bytes = b"\x00"
    if not hmac.compare_digest(header_bytes, owner.encode("ascii")):
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
            # Must equal a current top-level folder node's name — tree
            # membership already subsumes containment and archive/PDF roots
            # (both scan as folder nodes). Unhide skips this so a root
            # deleted from disk can still be cleared.
            names = await _top_level_folder_names(request)
            if root not in names:
                raise _err(404, "no such top-level folder or archive")
    snap = await _store_call(store.set_hidden, user_key, root, hidden)
    return _respond(owner, snap)
