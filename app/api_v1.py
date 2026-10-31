"""API v1 endpoints — the normalized shapes of api/openapi.v1.yaml.

v0 routes stay registered for compatibility (M11 retires them); handlers
here either delegate to the shared v0/v1 impl or wrap its payload in the
v1 envelope. The Rust gateway port and generated clients (iOS / TS) code
against these shapes.
"""

import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.auth import require_auth
from app.folder_visibility_routes import get_folder_visibility, put_folder_visibility
from app.routes import (
    _POSITION_MAX_FOLDERS,
    _POSITION_MAX_USERS,
    _POSITION_TS_FUTURE_SLACK_MS,
    _facets_impl,
    _folder_tree,
    _latest_position,
    _list_folder_images,
    _position_user_key,
    _positions,
    _search_impl,
    _slot_max_ts,
    _store_position_v1,
    _trim_position_slot,
    _user_position_slot,
    _validate_folder_param,
    _validate_position_body_v1,
    refresh_index,
    serve_thumb_sprite,
)

router = APIRouter(tags=["v1"])


@router.get("/api/v1/health")
async def v1_health():
    """Health check — no auth required (spec: security [])."""
    return {"status": "ok"}


@router.get("/api/v1/version")
async def v1_version(request: Request, _user: dict = Depends(require_auth)):
    """Version info."""
    settings = request.app.state.settings
    return JSONResponse(
        content={"version": settings.version, "sha": settings.git_sha, "build_date": settings.build_date},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/v1/tree")
async def v1_tree(
    request: Request,
    path: str = Query("", description="Subtree path relative to downloads dir (empty = root)"),
    depth: int | None = Query(None, description="1 = direct child folders only; omitted = full tree"),
    since: int | None = Query(None, description="Tree generation last seen; match = 304"),
    _user: dict = Depends(require_auth),
):
    """Folder tree as ``{kind: tree|children, nodes, generation}``."""
    return await _folder_tree(request, path, depth, since, v1=True)


@router.get("/api/v1/images")
async def v1_images(
    request: Request,
    folder: str = Query(..., description="Folder path relative to downloads dir"),
    _user: dict = Depends(require_auth),
):
    """Direct-child image names as ``{folder, images[]}`` (non-recursive)."""
    downloads_dir = request.app.state.settings.downloads_path
    images = await run_in_threadpool(_list_folder_images, downloads_dir, folder)
    return {"folder": folder, "images": images}


@router.get("/api/v1/search")
async def v1_search(
    request: Request,
    q: str = Query("", description="Free text search"),
    artist: str = Query("", description="Artist filter"),
    tag: str = Query("", description="Tags (comma-separated)"),
    sort: str = Query("relevance", description="Sort: relevance"),
    include_hidden: bool = Query(False, description="全表示 mode: do not exclude the caller's hidden roots"),
    _user: dict = Depends(require_auth),
):
    return await _search_impl(request, q, artist, tag, sort, include_hidden, _user)


@router.get("/api/v1/facets")
async def v1_facets(
    request: Request,
    path: str = Query("", description="Subtree path relative to downloads dir (empty = whole library)"),
    include_hidden: bool = Query(False, description="全表示 mode: do not exclude the caller's hidden roots"),
    _user: dict = Depends(require_auth),
):
    return await _facets_impl(request, path, include_hidden, _user)


@router.post("/api/v1/index/refresh")
async def v1_refresh_index(request: Request, _user: dict = Depends(require_auth)):
    """Synchronous metadata index rebuild — same contract as /api/refresh."""
    return await refresh_index(request, _user)


@router.get("/api/v1/thumb-sprite")
async def v1_thumb_sprite(request: Request, _user: dict = Depends(require_auth)):
    """Contact-sheet sprite — same contract as /api/thumb-sprite."""
    return await serve_thumb_sprite(request, _user)


@router.get("/api/v1/reading-positions")
async def v1_list_reading_positions(
    request: Request,
    folder: str | None = Query(None, description="Restrict to one work's record"),
    user: dict = Depends(require_auth),
):
    """The caller's reading positions — ``{positions: [...]}``, newest first."""
    slot = _user_position_slot(_positions(request), _position_user_key(user), create=False)
    folders = (slot or {}).get("folders", {})
    if folder is not None:
        rec = folders.get(folder)
        recs = [rec] if rec is not None and not rec.get("tombstone") else []
    else:
        recs = sorted(
            (r for r in folders.values() if not r.get("tombstone")),
            key=lambda r: r["ts"],
            reverse=True,
        )
    return JSONResponse(
        content={
            "positions": [
                {k: r[k] for k in ("folder", "page", "total", "ts")} for r in recs
            ]
        },
        headers={"Cache-Control": "no-store"},
    )


@router.put("/api/v1/reading-positions")
async def v1_put_reading_position(
    request: Request,
    folder: str = Query(..., description="Work path the record belongs to"),
    user: dict = Depends(require_auth),
):
    """Record a reading position — per-folder last-writer-wins."""
    _validate_folder_param(folder)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    rec = _validate_position_body_v1(body)
    # Same wrong-clock guard as v0 — see put_reading_position's comment.
    if rec["ts"] > int(time.time() * 1000) + _POSITION_TS_FUTURE_SLACK_MS:
        raise HTTPException(status_code=400, detail="ts too far in the future")
    rec["folder"] = folder
    rec["updated_at"] = time.time()
    positions = _positions(request)
    key = _position_user_key(user)
    if key not in positions and len(positions) >= _POSITION_MAX_USERS:
        stalest = min(positions, key=lambda k: positions[k].get("updated_at", 0))
        del positions[stalest]
    slot = _user_position_slot(positions, key)
    if not _store_position_v1(slot, rec):
        return {"status": "stale"}
    slot["updated_at"] = rec["updated_at"]
    _trim_position_slot(slot)
    return {"status": "ok"}


@router.delete("/api/v1/reading-positions")
async def v1_delete_reading_positions(
    request: Request,
    folder: str | None = Query(None, description="Omit to wipe every record"),
    user: dict = Depends(require_auth),
):
    """Delete one folder's record, or every record when folder is omitted.

    Both leave a tombstone so a stale in-flight PUT can't resurrect the
    wiped record — the same contract as the v0 delete.
    """
    positions = _positions(request)
    key = _position_user_key(user)
    slot = _user_position_slot(positions, key)
    slot["updated_at"] = time.time()
    if folder is not None:
        _validate_folder_param(folder)
        prev = slot["folders"].get(folder)
        slot["folders"][folder] = {
            "tombstone": True,
            "ts": (prev or {}).get("ts", 0),
            "updated_at": time.time(),
        }
    else:
        positions[key] = {
            "folders": {},
            "tombstone_ts": _slot_max_ts(slot),
            "updated_at": slot["updated_at"],
        }
    return {"status": "ok"}


@router.get("/api/v1/folder-visibility")
async def v1_get_folder_visibility(request: Request, user: dict = Depends(require_auth)):
    return await get_folder_visibility(request, user)


@router.put("/api/v1/folder-visibility")
async def v1_put_folder_visibility(request: Request, user: dict = Depends(require_auth)):
    return await put_folder_visibility(request, user)
