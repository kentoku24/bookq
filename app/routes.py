"""Application routes: viewer page, API, and image serving."""

import asyncio
import datetime
import hashlib
import inspect
import json
import logging
import math
import mimetypes
import os
import re
import time
from email import utils
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BeforeValidator
from starlette.concurrency import run_in_threadpool

from app import auth as auth_module
from app.archive import ARCHIVE_EXTENSIONS, is_archive_file, list_archive_entries
from app.auth import get_current_user, require_auth
from app.folder_visibility import (
    DB_FILENAME,
    GLOBAL_USER_KEY,
    user_key_for_email,
    visibility_owner,
)
from app.metadata import MetadataIndex
from app.reading_positions import (
    REJECTED,
    STALE,
    ReadingPositionStore,
    position_user_key,
)
from app.scanner import PDF_EXTENSIONS, _scan_archive_node, _scan_pdf_node, folder_cover_fields, is_pdf_file, list_images, natural_key, scan_tree
from app.tree_signature import compute_signature

logger = logging.getLogger(__name__)

api_router = APIRouter()

_TEMPLATE_DIR = str(Path(__file__).parent / "templates")
INDEX_CACHE_TTL = 300  # 5 minutes in seconds

_jinja_env = Environment(
    loader=FileSystemLoader(_TEMPLATE_DIR),
    autoescape=select_autoescape(["html", "xml"]),
    auto_reload=True,
    cache_size=0,
)

_index_lock = asyncio.Lock()


class _IndexBuilding(Exception):
    """The metadata index cache is missing and its cold build is still running."""


def _index_disabled() -> bool:
    """True when the metadata index must not be built or held (rs-native mode).

    ``BOOKQ_DISABLE_INDEX`` opts the Python viewer into "no index" mode for
    the viewer-rs full rollout (#320): once every tree/index consumer is
    served natively upstream, the Python worker must stop building and
    holding the tree+index (the largest heap block — ~150-250MB on a
    ~150k-image library). Read at call time so tests can flip it via
    monkeypatch.setenv.

    Index-needing handlers fail fast with 503 via ``_IndexDisabled`` — a
    lazy build would silently regrow the heap on misconfiguration.
    Endpoints that serve without the index (shallow /api/folders live-FS
    fallback, /api/images, /image, /thumb, /view, /api/v1/health) keep
    working, so a disabled viewer still functions as the plain-file
    fallback backend behind viewer-rs.
    """
    return os.getenv("BOOKQ_DISABLE_INDEX", "").strip().lower() in (
        "1", "true", "yes",
    )


class _IndexDisabled(HTTPException):
    """Raised by the ``_ensure_*`` path under ``BOOKQ_DISABLE_INDEX`` → 503."""

    def __init__(self) -> None:
        super().__init__(
            status_code=503,
            detail="metadata index disabled (BOOKQ_DISABLE_INDEX)",
        )


# How long a request may wait on an in-flight cold build before answering
# 202. Small libraries finish inside the budget and keep the synchronous
# 200 contract; a large-library cold build never parks a request.
_INDEX_BUILD_AWAIT_SEC = 0.8


async def _rebuild_into_cache(app) -> None:
    """Single-flight cold build: scan outside _index_lock, swap inside it.

    Runs as ``app.state.index_build_task`` so every cache-miss caller shares
    one build instead of queueing on a 5-10s scan under the lock. The handle
    is cleared in finally; scan failures propagate to the awaiters and cache
    nothing (a partial tree is never kept).
    """
    state = getattr(app, "state", app)
    try:
        tree, index, _folders, _images, _archives, _epubs, _rars, _pdfs = await _scan_and_rebuild(
            state.settings.downloads_path
        )
        async with _index_lock:
            cache_data = getattr(state, "metadata_index_cache", None)
            if (
                cache_data is not None
                and (time.time() - cache_data["timestamp"]) <= INDEX_CACHE_TTL
                and "tree" in cache_data
            ):
                # A warmup or manual /api/refresh landed a fresh cache while
                # this scan ran — keep that one, it is fresher by definition.
                return
            prev_gen = cache_data.get("generation", 1) if isinstance(cache_data, dict) else 1
            # Same null-signature contract as the previous inline path: the
            # next background refresh walks the tree and self-heals it.
            state.metadata_index_cache = {
                "tree": tree, "index": index, "timestamp": time.time(),
                "generation": prev_gen, "signature": None}
    finally:
        try:
            state.index_build_task = None
        except Exception:
            pass

# Shallow-warmup background fill pacing (plan shallow-warmup-fill T3): one
# opaque archive node is materialized at a time, with this pause between
# items, and the fill pauses entirely while a request arrived within
# _FILL_IDLE_SECONDS (app.state.last_request_at, stamped by /api/folders,
# /thumb/ and /api/thumb-sprite).
_FILL_IDLE_SECONDS = 10.0
_FILL_STEP_SECONDS = 0.2

# Generation-bump coalescing (plan fill-pacing): the fill used to bump the
# cache generation on EVERY materialized node — each bump answers every
# ?since-polling client with a full tree re-fetch (~12MB on the live
# library), so a long fill turned into a storm of identical downloads. The
# bump now lands at most once per _FILL_BUMP_INTERVAL_SECONDS, once per
# _FILL_BUMP_EVERY_ITEMS materialized nodes, and always once after the
# final item — the generation stays monotonic and ?since semantics are
# unchanged, pollers just observe fewer, larger steps.
_FILL_BUMP_INTERVAL_SECONDS = 2.0
_FILL_BUMP_EVERY_ITEMS = 10


async def _wait_requests_idle(app_state) -> None:
    """Sleep until no request has arrived for ``_FILL_IDLE_SECONDS``.

    Shared pacing gate for the startup background tasks (_background_fill,
    _warm_covers). ``app_state.last_request_at`` is stamped by /api/folders,
    /thumb/, /view/ and /api/thumb-sprite, so any recent client activity
    holds background CPU/disk work back instead of competing with
    interactive use. A missing/zero stamp means no request has ever arrived
    — proceed immediately.
    """
    last = getattr(app_state, "last_request_at", 0.0) or 0.0
    while time.time() - float(last) < _FILL_IDLE_SECONDS:
        await asyncio.sleep(1.0)
        last = getattr(app_state, "last_request_at", 0.0) or 0.0


def _require_int_literal(v):
    """Reject non-integer-literal query values ahead of pydantic's lax coercion.

    FastAPI's default int coercion accepts ``since=0.0`` / ``depth=1.0``,
    which the spec's ``type: integer`` forbids — schemathesis flags the
    acceptance as ST201 (#326). ``[0-9]`` rather than ``\\d`` — the latter
    matches Unicode digits (``１２``) which ``int()`` also parses. Values
    already non-str (direct unit calls, ``None``) pass through untouched.
    """
    if isinstance(v, str) and re.fullmatch(r"-?[0-9]+", v) is None:
        raise ValueError("expected an integer literal")
    return v


_QueryInt = Annotated[int, BeforeValidator(_require_int_literal)]


def _is_within(base: Path, target: Path) -> bool:
    """Return True if *target* is inside *base* (resolves symlinks, guards prefix bypass)."""
    try:
        return target.resolve().is_relative_to(base.resolve())
    except (OSError, RuntimeError, ValueError):
        # Symlink loop, dangling symlink, or unreadable path — treat as outside, not 500.
        return False


def render_template(request: Request, template_name: str, context: dict) -> HTMLResponse:
    """Render a Jinja2 template with the request in context."""
    template = _jinja_env.get_template(template_name)
    return HTMLResponse(template.render({"request": request, **context}))


def _count_tree_nodes(tree: list[dict]) -> tuple[int, int, int, int, int, int]:
    """Count folder/image nodes by their ``type`` key, plus archive breakdown.

    Both the request path and startup warmup log these counts, so the walk
    lives here rather than being hand-rolled twice. Archive-type folder
    nodes (``archive`` flag from app/scanner.py) still count as folders;
    the extra counters break them down: ``pdf`` flag set counts as pdfs,
    otherwise the filename suffix decides — ``.epub`` counts as epubs,
    ``.rar``/``.cbr`` as rars, anything else (zip/cbz) as archives.
    """
    folders = 0
    images = 0
    archives = 0
    epubs = 0
    rars = 0
    pdfs = 0
    stack = list(tree)
    while stack:
        node = stack.pop()
        if node.get("type") == "folder":
            folders += 1
            if node.get("pdf"):
                pdfs += 1
            elif node.get("archive"):
                suffix = Path(node.get("name") or "").suffix.lower()
                if suffix == ".epub":
                    epubs += 1
                elif suffix in (".rar", ".cbr"):
                    rars += 1
                else:
                    archives += 1
            children = node.get("children") or []
            stack.extend(children)
        elif node.get("type") == "image":
            images += 1
    return folders, images, archives, epubs, rars, pdfs


def _scan_accepts_phases(fn) -> bool:
    """True when *fn* takes the ``scan_tree`` ``_phases`` accumulator.

    ``scan_tree`` is monkeypatched by single-argument stubs in lifespan
    abandonment tests, so the accumulator is only passed when the
    (possibly patched) callable actually declares it — otherwise the
    phase fields simply stay 0.0 for that rebuild.
    """
    try:
        return "_phases" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


async def _scan_and_rebuild(
    downloads_path: Path,
    list_archives: bool = True,
) -> tuple[list[dict], MetadataIndex, int, int, int, int, int, int]:
    """Run one scan_tree + MetadataIndex rebuild pass, log it, return it.

    Shared by the request path (_ensure_tree_and_index) and startup warmup
    (warmup_metadata_index) so the node counts, the timings and the single
    "metadata index rebuilt" log line cannot drift between the two callers.
    Raises on failure — callers decide whether to cache. Only the warmup
    passes ``list_archives=False`` (opaque archive nodes); the request path
    keeps the default True.
    """
    started = time.perf_counter()
    scan_start = time.perf_counter()
    phases: dict[str, float] = {"walk_ms": 0.0, "archive_ms": 0.0, "pdf_ms": 0.0}
    if _scan_accepts_phases(scan_tree):
        tree = await run_in_threadpool(
            scan_tree, downloads_path, None, phases, list_archives=list_archives
        )
    else:
        tree = await run_in_threadpool(scan_tree, downloads_path)
    scan_ms = (time.perf_counter() - scan_start) * 1000
    index = MetadataIndex()
    rebuild_start = time.perf_counter()
    await run_in_threadpool(index.rebuild_from_tree, tree, downloads_path)
    rebuild_ms = (time.perf_counter() - rebuild_start) * 1000
    folders, images, archives, epubs, rars, pdfs = _count_tree_nodes(tree)
    # One line per rebuild so `docker compose logs viewer | grep "index rebuilt"` counts
    # how many workers rebuilt independently (#113: cache is per process).
    logger.info(
        "metadata index rebuilt pid=%s comics=%d elapsed_ms=%.0f scan_ms=%.0f rebuild_ms=%.0f folders=%d images=%d archives=%d epubs=%d rars=%d pdfs=%d walk_ms=%.0f archive_ms=%.0f pdf_ms=%.0f",
        os.getpid(), len(index.comics), (time.perf_counter() - started) * 1000,
        scan_ms, rebuild_ms, folders, images, archives, epubs, rars, pdfs,
        phases["walk_ms"], phases["archive_ms"], phases["pdf_ms"],
    )
    return tree, index, folders, images, archives, epubs, rars, pdfs


def _schedule_index_refresh(app) -> None:
    """Schedule a single-flight background index rebuild on *app*.

    A second caller arriving while a refresh is still running is a no-op —
    the in-flight task's swap covers it. The handle lives on
    ``app.state.index_refresh_task``; it is read with getattr and assigned
    as a plain attribute so SimpleNamespace stub states work the same as
    the real Starlette State. Never raises: the caller already served
    stale, so a missed refresh only delays freshness, never breaks serving.
    """
    state = getattr(app, "state", app)
    try:
        existing = getattr(state, "index_refresh_task", None)
    except Exception:
        existing = None
    if existing is not None and not existing.done():
        return
    try:
        state.index_refresh_task = asyncio.create_task(_background_index_refresh(app))
    except Exception:
        # No running loop (or a read-only state): skip the refresh. Stale
        # was already served, so this only delays freshness.
        pass


async def _background_index_refresh(app) -> None:
    """Rebuild the index off the request path and atomically swap it in.

    The stale-serve caller already served the old cache, so this task owns
    freshness. First a cheap filesystem-signature gate runs OUTSIDE
    _index_lock: a signature walk over an unchanged tree refreshes the
    timestamp IN PLACE (never rebinding the dict — generation/ETag stay
    stable) with zero scans; a replaced (concurrent rebuild / manual
    refresh) or freshly re-stamped cache is left untouched, and a
    mismatched, missing, or failed walk falls through to the full rebuild.

    The full scan runs OUTSIDE _index_lock (a 5-10s walk must never hold
    it); only the swap takes the lock. Inside the lock the cache is
    re-read: a cache that turned fresh while the scan ran (manual
    /api/refresh or another rebuild) is left untouched, and the swap
    advances the generation when the gate's signature proved the tree
    changed (so ``?since=`` pollers re-fetch) while keeping it when the
    change could not be verified. The swap stores the pre-scan signature
    the gate walked (None when no walk ran or it failed) — a rebuilt cache
    still carries a gateable signature for the next expiry; a tree that
    changes mid-scan self-heals at the next expiry via the mismatch. Failures keep the old cache and log a
    warning — a partial tree is never kept. CancelledError is re-raised;
    the task handle is always cleared.
    """
    state = getattr(app, "state", app)
    sig: str | None = None
    sig_walked = False
    try:
        try:
            downloads_path = state.settings.downloads_path
        except Exception:
            return
        cur = getattr(state, "metadata_index_cache", None)
        if isinstance(cur, dict) and "tree" in cur:
            try:
                sig = await run_in_threadpool(compute_signature, downloads_path)
                sig_walked = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "background index refresh signature walk skip error=%s: %s",
                    type(exc).__name__, exc,
                )
                sig, sig_walked = None, False
            if sig_walked:
                try:
                    # Re-read after the walk: a concurrent rebuild or manual
                    # refresh may have replaced the dict while it ran — that
                    # winner is fresher by construction, so leave it be.
                    cur2 = getattr(state, "metadata_index_cache", None)
                    if (
                        not isinstance(cur2, dict)
                        or cur2 is not cur
                        or "tree" not in cur2
                    ):
                        return
                    if (time.time() - cur2["timestamp"]) <= INDEX_CACHE_TTL:
                        # Turned fresh while the walk ran — nothing to do.
                        return
                    if sig is not None and sig == cur2.get("signature"):
                        # Unchanged tree: refresh the timestamp IN PLACE and
                        # skip the scan entirely. The identity re-check under
                        # the lock keeps a racing swap from losing the bump.
                        async with _index_lock:
                            if getattr(state, "metadata_index_cache", None) is cur:
                                cur["timestamp"] = time.time()
                        return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "background index refresh signature gate skip error=%s: %s",
                        type(exc).__name__, exc,
                    )
        try:
            tree, index, _folders, _images, _archives, _epubs, _rars, _pdfs = await _scan_and_rebuild(downloads_path)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "background index refresh skip error=%s: %s",
                type(exc).__name__, exc,
            )
            return
        try:
            async with _index_lock:
                cache_data = getattr(state, "metadata_index_cache", None)
                if (
                    cache_data is not None
                    and (time.time() - cache_data["timestamp"]) <= INDEX_CACHE_TTL
                    and "tree" in cache_data
                ):
                    return
                prev_gen = cache_data.get("generation", 1) if isinstance(cache_data, dict) else 1
                prev_sig = cache_data.get("signature") if isinstance(cache_data, dict) else None
                # The gate only falls through to a rebuild on a signature
                # mismatch, so reaching this swap means the tree CHANGED:
                # advance the generation, or ``/api/folders?since=<gen>``
                # would answer 304 forever and the poller would never pick the
                # new tree up. Two real signatures must be comparable to claim
                # the change — a null previous signature (cold build, warmup
                # failure) or an unverifiable walk keeps the generation.
                changed = (
                    sig_walked and sig is not None
                    and prev_sig is not None and sig != prev_sig
                )
                state.metadata_index_cache = {
                    "tree": tree, "index": index,
                    "timestamp": time.time(),
                    "generation": prev_gen + 1 if changed else prev_gen,
                    "signature": sig if sig_walked else None,
                }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "background index refresh swap skip error=%s: %s",
                type(exc).__name__, exc,
            )
    finally:
        try:
            state.index_refresh_task = None
        except Exception:
            pass


async def _ensure_tree_and_index(request: Request, wait: bool = False) -> tuple[list[dict], MetadataIndex]:
    """Return a (tree, index) pair, never blocking the request on a scan.

    A single scan_tree walk feeds both the folders response and the index
    rebuild, so one filesystem walk serves the whole /api/folders request.
    The lock itself is check-and-swap only (double-checked, never held
    across the build beyond run_in_threadpool dispatch). On rebuild
    failure nothing is cached — a partial tree is never kept — and the
    error propagates.

    Stale-serve: a TTL-expired cache is returned IMMEDIATELY and refresh
    moves to a single-flight background task (_schedule_index_refresh —
    see _background_index_refresh, which gates the rebuild on the
    filesystem signature so an unchanged tree costs zero scans), so the
    first request past expiry never blocks on a 5-10s full scan.

    Cold-miss (startup before warmup lands, warmup failure) also never
    blocks: the build runs as the shared ``app.state.index_build_task``
    (_rebuild_into_cache), callers await it only for
    ``_INDEX_BUILD_AWAIT_SEC``, and a still-running build raises
    ``_IndexBuilding`` — the handlers answer 202 + Retry-After instead of
    parking the request for the whole scan. Small libraries finish inside
    the budget, so their 200 contract is unchanged. ``wait=True`` restores
    the old block-until-built semantics for callers that cannot answer
    202 (the reader-sync WS path has no retry channel to offer).
    """
    if _index_disabled():
        raise _IndexDisabled()
    cache_data = getattr(request.app.state, "metadata_index_cache", None)
    if cache_data is not None and "tree" in cache_data:
        if (time.time() - cache_data["timestamp"]) <= INDEX_CACHE_TTL:
            return cache_data["tree"], cache_data["index"]
        # Stale: serve now, refresh in the background (single-flight).
        _schedule_index_refresh(request.app)
        return cache_data["tree"], cache_data["index"]

    # Bound the LOCK WAIT itself: /api/refresh holds _index_lock across its
    # whole synchronous rebuild, which would otherwise park a cold-miss
    # caller for the same 10s+ this path exists to avoid. An in-flight
    # refresh IS a build — its swap lands a fresh cache, so answering 202
    # and letting the client retry is the same contract as the task path.
    # No shield here (unlike the task await below): a timed-out acquire must
    # be cancelled outright, or the leaked waiter would grab the lock later
    # with nobody left to release it. wait=True callers keep the old
    # unbounded acquire — they cannot express "come back later".
    if wait:
        await _index_lock.acquire()
    else:
        try:
            await asyncio.wait_for(_index_lock.acquire(), _INDEX_BUILD_AWAIT_SEC)
        except asyncio.TimeoutError:
            raise _IndexBuilding()
    try:
        # Double-check after acquiring lock — another coroutine may have rebuilt.
        cache_data = getattr(request.app.state, "metadata_index_cache", None)
        if (
            cache_data is not None
            and (time.time() - cache_data["timestamp"]) <= INDEX_CACHE_TTL
            and "tree" in cache_data
        ):
            return cache_data["tree"], cache_data["index"]
        task = getattr(request.app.state, "index_build_task", None)
        if task is None:
            task = asyncio.create_task(_rebuild_into_cache(request.app))
            request.app.state.index_build_task = task
    finally:
        _index_lock.release()
    # Await OUTSIDE the lock and bounded: the lock only guards
    # check-and-swap, so a 10s+ cold scan never parks a queue of requests
    # under it. Shield keeps a timed-out caller from cancelling the shared
    # build; its exception (build failure) still propagates to every awaiter.
    if wait:
        await asyncio.shield(task)
    else:
        try:
            await asyncio.wait_for(asyncio.shield(task), _INDEX_BUILD_AWAIT_SEC)
        except asyncio.TimeoutError:
            raise _IndexBuilding()
    cache_data = getattr(request.app.state, "metadata_index_cache", None)
    if cache_data is None or "tree" not in cache_data:
        # The task returned without caching (a fresher cache landed mid-run
        # and was then invalidated, or the task failed in a non-raising way).
        raise _IndexBuilding()
    return cache_data["tree"], cache_data["index"]


async def _ensure_index(request: Request) -> MetadataIndex:
    """Return a ready MetadataIndex, rebuilding it if stale or missing."""
    _, index = await _ensure_tree_and_index(request)
    return index


async def warmup_metadata_index(app_state) -> tuple[int, int, float]:
    """Request-independent warmup for lifespan startup.

    Runs the same cache-miss path as _ensure_tree_and_index via
    _scan_and_rebuild and populates the exact same
    ``app_state.metadata_index_cache`` structure ({tree, index,
    timestamp, generation, signature} — generation starts at 1 and the background
    fill bumps it per materialized node; signature is the post-scan
    compute_signature so the first TTL miss gates without rebuilding), so the first real request hits cache. No lock: lifespan
    completes before the server accepts requests, so nothing contends.
    Eager imports (pypdfium2/rarfile/PIL) stay in the caller
    (app/main.py lifespan), not here.

    Raises on failure — the caller logs WARNING and continues startup.
    Returns (folders, images, elapsed_ms).

    Under ``BOOKQ_DISABLE_INDEX`` (#320) the warmup is a no-op: rs-native
    mode must not build the tree at all, not even once (a one-shot build
    would still leave it in the cache for its lifetime).
    """
    if _index_disabled():
        logger.info("index disabled (BOOKQ_DISABLE_INDEX): warmup skipped pid=%s", os.getpid())
        return 0, 0, 0.0
    started = time.perf_counter()
    tree, index, folders, images, _archives, _epubs, _rars, _pdfs = await _scan_and_rebuild(
        app_state.settings.downloads_path, list_archives=False
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    sig = await run_in_threadpool(compute_signature, app_state.settings.downloads_path)
    app_state.metadata_index_cache = {"tree": tree, "index": index, "timestamp": time.time(),
                                      "generation": 1, "signature": sig}
    return folders, images, elapsed_ms


def _find_first_opaque(nodes: list[dict], parent_path: str, skip: set[str],
                       _chain: tuple = ()):
    """Return the first fillable opaque node as ``(chain, full_path, is_pdf)``.

    Opaque = shallow-scan placeholder (``archive`` flag with ``children == []``).
    *skip* holds already-filled full paths: a genuinely empty archive
    materializes to the identical shape, so without *skip* the fill would
    loop on it forever. Paths compose as parent/name (never split names —
    flattened names contain "/").

    *chain* is the root-down hop list to the node — ``((nodes_list, idx),
    ...)`` where the last hop is the node itself and every earlier hop's node
    is one of its ancestors. The fill walks it back to front after replacing
    the node so each ancestor's cover fields catch up in the same pass.
    """
    for idx, node in enumerate(nodes):
        if node.get("type") != "folder":
            continue
        full = f"{parent_path}/{node['name']}" if parent_path else node["name"]
        chain = _chain + ((nodes, idx),)
        if node.get("archive") and not node.get("children") and full not in skip:
            return chain, full, bool(node.get("pdf"))
        children = node.get("children")
        if children:
            found = _find_first_opaque(children, full, skip, chain)
            if found is not None:
                return found
    return None


def _refresh_ancestor_covers(chain: tuple) -> None:
    """Recompute ``thumb``/``thumbs`` bottom-up for a materialized node's ancestors.

    A shallow-scan tree carries no cover for a folder whose images live only
    inside archives (``thumb is None`` — its archive children were opaque).
    Materializing those children never re-derived the folder's own cover, so
    the tile stayed a placeholder for the process lifetime (the TTL/signature
    refresh skips the rescan of an unchanged filesystem). Each hop's node is
    recomputed from its (now richer) children with the scan's shared cover
    rule, deepest ancestor first so the level above sees the updated value —
    the shallow tree converges to the full-scan shape without a rescan.
    """
    for nodes, idx in reversed(chain[:-1]):
        node = nodes[idx]
        thumb, sub_thumbs = folder_cover_fields(node.get("children") or [])
        node["thumb"] = thumb
        if sub_thumbs is None:
            node.pop("thumbs", None)
        else:
            node["thumbs"] = sub_thumbs


async def _background_fill(app_state) -> None:
    """Materialize shallow opaque archive nodes one by one (plan T3, fail-open).

    Spawned once by lifespan after 起動完了; lives past yield. Each item
    runs three phases: under _index_lock it picks the next opaque node and
    captures its splice coordinates (a cheap tree walk — the lock keeps
    that read consistent with the splice the same task performs), then the
    archive/PDF open runs OUTSIDE the lock via run_in_threadpool
    (_scan_archive_node/_scan_pdf_node are pure functions of their
    arguments — a multi-second RAR/EPUB listing must never park the
    tree-mutating paths the lock serializes), and a second locked section
    splices the built node in place only after re-checking the slot is
    still the same opaque placeholder — a rebuild may have swapped the
    tree, or another actor may have filled the slot while the scan ran, in
    which case the stale result is discarded. The cache generation
    advances coalesced to at most one bump per
    _FILL_BUMP_INTERVAL_SECONDS, per _FILL_BUMP_EVERY_ITEMS materialized
    nodes, and always once after the final item, so ?since pollers see
    fewer, larger steps instead of a full-tree re-fetch per node. Between
    items the task sleeps
    _FILL_STEP_SECONDS and pauses while requests are fresh (idle detection
    via app_state.last_request_at). Per-item exceptions are logged and
    skipped; index/facets are untouched (name-only registration already
    covers archives). The swapped node keeps the tree's own ``name`` (a
    flattened name carries a parent/child path that the built node, made
    from the file's real name, does not) and every ancestor's cover fields
    are refreshed from the enriched children, so a folder whose covers live
    only inside archives stops being a placeholder. Never raises.
    """
    try:
        cache = getattr(app_state, "metadata_index_cache", None)
        if not isinstance(cache, dict) or "tree" not in cache:
            return
        downloads_path = app_state.settings.downloads_path
        tree = cache["tree"]
        done: set[str] = set()
        # Coalesced generation accounting: *pending* counts the splices this
        # tree has taken since its last bump. A tree swap resets it — a
        # rebuilt cache carries its own generation and unreachable splices
        # must not advance it.
        pending = 0
        last_bump = time.monotonic()
        while True:
            await _wait_requests_idle(app_state)
            full: str | None = None
            try:
                # Phase 1 (locked, cheap): pick the next opaque node and
                # capture the splice coordinates. The walk stays under the
                # lock so the chosen node is read consistently with the
                # splice this task will perform — the expensive part is
                # what moves out.
                async with _index_lock:
                    cache = getattr(app_state, "metadata_index_cache", None)
                    if not isinstance(cache, dict) or "tree" not in cache:
                        return
                    if cache["tree"] is not tree:
                        tree, done = cache["tree"], set()
                        pending, last_bump = 0, time.monotonic()
                    found = _find_first_opaque(tree, "", done)
                    if found is None:
                        if pending:
                            # Final flush: the completed tail of the work
                            # must always advance the generation, or ?since
                            # pollers would never see the last materialized
                            # nodes.
                            cache["generation"] = int(cache.get("generation", 1) or 1) + 1
                        return
                    chain, full, is_pdf = found
                    nodes, idx = chain[-1]
                    node = nodes[idx]
                    tree_name = node.get("name")

                # Phase 2 (UNLOCKED): the archive/PDF open. Holding
                # _index_lock across this threadpool await used to park
                # every tree-mutating path behind a seconds-long listing.
                archive_file = downloads_path / full
                if is_pdf:
                    filled = await run_in_threadpool(
                        _scan_pdf_node, archive_file.parent, archive_file.name,
                        downloads_path,
                    )
                else:
                    filled = await run_in_threadpool(
                        _scan_archive_node, archive_file.parent, archive_file.name,
                        downloads_path,
                    )

                # Phase 3 (locked): splice only if the slot is still the
                # same opaque placeholder captured in phase 1 — the scan
                # ran unlocked, so a rebuild or a racing fill may have
                # beaten this result to it.
                async with _index_lock:
                    cache = getattr(app_state, "metadata_index_cache", None)
                    if not isinstance(cache, dict) or "tree" not in cache:
                        return
                    same_tree = cache["tree"] is tree
                    cur = nodes[idx] if same_tree and 0 <= idx < len(nodes) else None
                    if (
                        cur is node
                        and node.get("archive") and not node.get("children")
                    ):
                        if tree_name is not None:
                            # Flattened nodes carry the parent/child name the
                            # scan hoisted ("wrap/v1.zip"); the built node only
                            # knows the file's own name. Keeping the tree's
                            # name is what makes the composed path resolve on
                            # disk for the client.
                            filled["name"] = tree_name
                        nodes[idx] = filled
                        _refresh_ancestor_covers(chain)
                        done.add(full)
                        pending += 1
                        now = time.monotonic()
                        if (
                            pending >= _FILL_BUMP_EVERY_ITEMS
                            or now - last_bump >= _FILL_BUMP_INTERVAL_SECONDS
                        ):
                            cache["generation"] = int(cache.get("generation", 1) or 1) + 1
                            pending, last_bump = 0, now
                    elif same_tree and cur is not None and not (
                        cur.get("archive") and not cur.get("children")
                    ):
                        # The slot was already materialized while the scan
                        # ran — drop the duplicate result and never re-scan
                        # the path.
                        done.add(full)
                    # Otherwise the tree was swapped mid-scan (chain/nodes
                    # point into the old tree — splicing there would be
                    # invisible) or the slot was replaced by a fresh opaque
                    # node; either way the stale result is dropped and a
                    # later pass re-scans the node in the current tree.
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "background fill skip %s error=%s: %s",
                    full if full is not None else "?", type(exc).__name__, exc,
                )
                if full is not None:
                    done.add(full)
            await asyncio.sleep(_FILL_STEP_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "background fill abort error=%s: %s", type(exc).__name__, exc,
        )


# Single-cover warm widths: 200 (tile first paint) and 400 (list/card).
_COVER_WARM_WIDTHS = (200, 400)


def _enumerate_cover_jobs(tree: list[dict], parent: str = "") -> list[tuple[str, int]]:
    """List ``(cover_path, width)`` warm jobs for every folder's single thumb.

    Each folder node carrying a ``thumb`` (folder-relative first image)
    yields the job at w=200 and w=400; ``thumb is None`` nodes are skipped.
    Full paths compose as parent/name — flattened names contain "/", so
    neither side is ever split (same rule as _find_tree_node). Multi-thumb
    ``thumbs[]`` lists and sprites are deliberately out of scope: only the
    single cover behind the first paint is warmed.
    """
    jobs: list[tuple[str, int]] = []
    for node in tree:
        if node.get("type") != "folder":
            continue
        full = f"{parent}/{node['name']}" if parent else node["name"]
        thumb = node.get("thumb")
        if thumb:
            for width in _COVER_WARM_WIDTHS:
                jobs.append((f"{full}/{thumb}", width))
        children = node.get("children") or []
        if children:
            jobs.extend(_enumerate_cover_jobs(children, full))
    return jobs


def _count_opaque_nodes(nodes: list[dict]) -> int:
    """Count shallow-scan placeholders (archive/PDF folder nodes with no children).

    The startup warm uses this to decide when to stop: a count of zero means
    the background fill materialized every archive, and a count that stops
    decreasing across passes means the fill is finished or stalled (a library
    holding a genuinely empty archive keeps that archive opaque forever, so
    the count can never reach zero there).
    """
    total = 0
    stack = list(nodes)
    while stack:
        node = stack.pop()
        if node.get("type") != "folder":
            continue
        children = node.get("children") or []
        if node.get("archive") and not children:
            total += 1
            continue
        stack.extend(children)
    return total


# Startup cover pre-warming (plan thumb-warmup-stale-serve Todo 3): the
# first pass runs IMMEDIATELY once the tree exists (waiting until serving
# went idle first would leave cold covers paying a live decode on first
# view), but the pass is deliberately gentle: ONE job at a time with a
# small yield after each, and every job idle-gates on
# app_state.last_request_at through the same _FILL_IDLE_SECONDS window the
# background fill uses (plan fill-pacing) — while requests are fresh the
# pass parks itself instead of competing with interactive use. A pillow
# decode/resize is CPU- and memory-heavy (a full
# scanned page decodes to tens of MB), so several in flight at once starve
# the serving event loop (observed ~10s /api/folders stalls at concurrency
# 4 on a memory-pressured box) — the opposite of this plan's first-paint
# goal. Later passes pick up trees the background fill materialized,
# sleeping between passes so the fill can advance. Stops once no opaque
# nodes remain, or once the generation AND the opaque count have been
# stable for _WARM_COVERS_QUIET_PASSES.
_WARM_COVERS_QUIET_PASSES = 3
_WARM_COVERS_MAX_SECONDS = 20 * 60
_WARM_COVERS_CONCURRENCY = 1
_WARM_COVERS_JOB_SLEEP_SECONDS = 0.02
_WARM_COVERS_POLL_SECONDS = 2.0
_WARM_COVERS_PASS_SLEEP_SECONDS = 5.0
_WARM_COVERS_WAIT_SECONDS = 120.0


async def _warm_covers(app_state) -> None:
    """Warm every folder's single cover into the thumb disk cache (fail-open).

    Spawned once by lifespan after 起動完了; lives past yield. Waits for
    the index cache to hold a tree (polls, gives up after the wait cap),
    then warms each enumerated job with bounded concurrency — each job is
    independently fail-open, so one corrupt source never aborts the pass.
    A pass snapshots (tree, generation) up front, so a concurrent index
    swap is safe (the next pass picks it up). Every job idle-gates on
    ``app_state.last_request_at`` (the same _FILL_IDLE_SECONDS window the
    background fill uses), so a pass parks itself while requests are fresh
    instead of competing with interactive use. Each pass then sleeps one
    poll interval before re-enumerating, so the startup background fill can
    materialize archives instead of racing a back-to-back tree re-walk, and
    prunes each thumb namespace once per pass (stores pass ``prune=False``).
    Stops once no opaque nodes remain, or once both the generation and the
    opaque count have been stable for _WARM_COVERS_QUIET_PASSES, or at the
    total time cap. Never raises except on cancellation.
    """
    from app.thumbs import prune_cache_dir, thumb_cache_namespace, warm_thumb_cache

    try:
        waited = 0.0
        while True:
            cache = getattr(app_state, "metadata_index_cache", None)
            if isinstance(cache, dict) and "tree" in cache:
                break
            if waited >= _WARM_COVERS_WAIT_SECONDS:
                return
            await asyncio.sleep(_WARM_COVERS_POLL_SECONDS)
            waited += _WARM_COVERS_POLL_SECONDS
        try:
            downloads_path = app_state.settings.downloads_path
        except Exception:
            return
        sem = asyncio.Semaphore(_WARM_COVERS_CONCURRENCY)
        start = time.monotonic()
        quiet = 0
        while True:
            if time.monotonic() - start > _WARM_COVERS_MAX_SECONDS:
                return
            cache = getattr(app_state, "metadata_index_cache", None)
            if not isinstance(cache, dict) or "tree" not in cache:
                return
            tree, gen = cache["tree"], cache.get("generation", 1)
            opaque = _count_opaque_nodes(tree)
            jobs = _enumerate_cover_jobs(tree)

            async def _one(job_path: str, job_width: int) -> None:
                async with sem:
                    # Idle-gate INSIDE the semaphore: at most one job parks
                    # here, so a burst of requests pauses the whole pass
                    # without a herd of sleeping coroutines.
                    await _wait_requests_idle(app_state)
                    try:
                        await warm_thumb_cache(downloads_path, job_path, job_width)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning(
                            "cover warm skip %s w=%d error=%s: %s",
                            job_path, job_width, type(exc).__name__, exc,
                        )
                    # Yield the loop after every job: the pillow work runs in
                    # the threadpool but holds the GIL in chunks, so without a
                    # breather a long pass starves request handling.
                    await asyncio.sleep(_WARM_COVERS_JOB_SLEEP_SECONDS)

            try:
                await asyncio.gather(*(_one(job_path, job_width) for job_path, job_width in jobs))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "cover warm pass skip error=%s: %s", type(exc).__name__, exc,
                )
            for width in _COVER_WARM_WIDTHS:
                await run_in_threadpool(prune_cache_dir, thumb_cache_namespace(width))
            await asyncio.sleep(_WARM_COVERS_PASS_SLEEP_SECONDS)
            cur = getattr(app_state, "metadata_index_cache", None)
            cur_gen = cur.get("generation", None) if isinstance(cur, dict) else None
            cur_tree = cur.get("tree") if isinstance(cur, dict) else None
            cur_opaque = _count_opaque_nodes(cur_tree) if isinstance(cur_tree, list) else 0
            if cur_gen == gen and cur_opaque == opaque:
                quiet += 1
            else:
                quiet = 0
            if cur_opaque == 0 or quiet >= _WARM_COVERS_QUIET_PASSES:
                return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "cover warm abort error=%s: %s", type(exc).__name__, exc,
        )


def _attach_metadata(nodes: list[dict], lookup: dict, parent_path: str) -> None:
    """Recursively attach ComicMetadata to folder nodes in the tree.

    Nodes without metadata carry no ``metadata`` key at all (only
    ``has_metadata: False``) — the client reads ``f.metadata||{}`` behind a
    ``has_metadata`` check, so absence and an empty object behave the same.
    """
    for node in nodes:
        if node["type"] == "folder":
            path = f"{parent_path}/{node['name']}" if parent_path else node["name"]
            comic = lookup.get(path)
            if comic is not None and comic.has_metadata:
                node["has_metadata"] = True
                node["metadata"] = comic.to_dict()
            else:
                node["has_metadata"] = False

            if node.get("children"):
                _attach_metadata(node["children"], lookup, path)


def _attach_subtree_facets(nodes: list[dict], subtree: dict, parent_path: str) -> None:
    """Attach per-folder aggregate facets (artists/tags/parodies of every descendant comic).

    Folders with no facets in their subtree carry no ``subtree_facets`` key —
    the client checks ``node && node.subtree_facets`` before filtering, so
    absence and an empty object behave the same.
    """
    for node in nodes:
        if node["type"] == "folder":
            path = f"{parent_path}/{node['name']}" if parent_path else node["name"]
            facets = subtree.get(path)
            if facets and any(facets.values()):
                node["subtree_facets"] = facets
            if node.get("children"):
                _attach_subtree_facets(node["children"], subtree, path)


@api_router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Viewer home page. Shows login prompt if unauthenticated, full viewer if authenticated."""
    user = await get_current_user(request)
    # Dev-only thumbnail timing mode: ?thumb_perf=1 inlines the shared probe and
    # result panel. Never enabled in production (_TEST_MODE is import-time false).
    thumb_perf = auth_module._TEST_MODE and request.query_params.get("thumb_perf") == "1"
    # Android re-blurs backdrop-filter every composited frame (measured on
    # CP08-J1: fps 4 vs 22 Firefox, 45 vs 83 Chrome). No client-side feature
    # test exists for the platform and navigator.userAgent is banned in app.js
    # (test_feature_test_replaces_ua_sniff), so tag <html> from the request's
    # User-Agent header; style.css drops the frost under .ua-android.
    # No Vary: User-Agent — the SW precaches '/' via a header-less Request, so a
    # varying response would never match a real navigation's cache lookup.
    ua_android = "android" in request.headers.get("user-agent", "").lower()
    visibility_user_key = (
        user_key_for_email(user.get("email")) if isinstance(user, dict) else None
    )
    visibility_owner_value = (
        visibility_owner(visibility_user_key, request.app.state.settings.secret_key)
        if visibility_user_key
        else None
    )
    # Embed the library-global visibility snapshot so the first paint is
    # already filtered — the client still runs its GET afterwards
    # (re-sync only). Store failure -> None embed -> the client's normal
    # loading gate.
    visibility_snapshot = None
    if visibility_owner_value is not None:
        _vis_store = getattr(request.app.state, "folder_visibility", None)
        if _vis_store is not None:
            try:
                _vis_snap = await run_in_threadpool(
                    _vis_store.read, GLOBAL_USER_KEY
                )
                visibility_snapshot = {
                    "owner": visibility_owner_value,
                    "revision": _vis_snap.revision,
                    "hiddenRoots": list(_vis_snap.hidden_roots),
                }
            except Exception:
                visibility_snapshot = None
    context = {
        "user": user,
        "test_mode": auth_module._TEST_MODE,
        "ua_android": ua_android,
        "visibility_owner": visibility_owner_value,
        "visibility_snapshot": visibility_snapshot,
        # Same normalized user-state key as folder visibility — scopes the
        # client's localStorage progress per account (#375).
        "progress_user_key": visibility_user_key,
    }
    if thumb_perf:
        context["thumbnail_perf"] = True
        context["thumbnail_probe"] = (
            Path(__file__).parent / "perf" / "thumbnail-probe.js"
        ).read_text(encoding="utf-8")
    response = render_template(request, "index.html", context)
    # The viewer page embeds per-user data (owner + snapshot) — never let a
    # shared/heuristic cache serve it to another account. Not "no-store":
    # the SW precaches "/" via cache.put(), which rejects no-store responses,
    # and the offline navigate fallback depends on that precache entry.
    response.headers["Cache-Control"] = "private, no-cache"
    if thumb_perf:
        response.headers["Cache-Control"] = "no-store"
    return response


@api_router.api_route("/sw.js", methods=["GET", "HEAD"], include_in_schema=False)
async def serve_sw():
    """Serve Service Worker at scope root — required so scope '/' is allowed."""
    sw_path = Path(__file__).parent / "static" / "sw.js"
    if not sw_path.is_file():
        raise HTTPException(status_code=404, detail="sw.js not found")
    return FileResponse(
        path=str(sw_path),
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@api_router.api_route("/favicon.ico", methods=["GET", "HEAD"], include_in_schema=False)
async def serve_favicon_ico():
    """Serve favicon at root — browsers probe /favicon.ico; alias to /static/favicon.ico.

    Icons remain cacheFirst via SW fetch handler, not precached in SHELL_ASSETS —
    lazy cacheFirst is sufficient and avoids bloating the shell precache.
    """
    ico_path = Path(__file__).parent / "static" / "favicon.ico"
    if not ico_path.is_file():
        raise HTTPException(status_code=404, detail="favicon.ico not found")
    return FileResponse(
        path=str(ico_path),
        media_type="image/x-icon",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@api_router.get("/api/health")
async def health():
    """Health check — no auth required."""
    return {"status": "ok"}


@api_router.get("/api/version")
async def version(request: Request):
    """Version info — no auth required."""
    settings = request.app.state.settings
    return JSONResponse(
        content={"version": settings.version, "sha": settings.git_sha, "build_date": settings.build_date},
        headers={"Cache-Control": "no-store"},
    )


def _find_tree_node(nodes: list[dict], target: str, parent_path: str = "") -> dict | None:
    """Find the folder node whose downloads-relative path equals *target*.

    Tree walk mirroring findNodeByPath (app.js) — NEVER split names on "/":
    flattened folder names like "wrap/inner/vol1" contain "/" and splitting
    either side would miss them. Match on the composed full path instead.
    """
    for node in nodes:
        full = f"{parent_path}/{node['name']}" if parent_path else node["name"]
        if full == target:
            return node
        children = node.get("children")
        if children and target.startswith(full + "/"):
            found = _find_tree_node(children, target, full)
            if found is not None:
                return found
    return None


_FOLDERS_CACHE_CONTROL = "private, max-age=60"


def _folders_response(
    request: Request,
    payload,
    path: str,
    depth: int | None,
    gen,
    gen_int: int = 1,
    key_ns: str = "v0",
):
    """Attach ETag + Cache-Control, or return 304 on If-None-Match hit.

    The serialized body and its ETag are cached in
    ``request.app.state.folders_body_cache``, keyed by *gen* (the index-cache
    generation the caller fetched *payload* under — ``cache_data["timestamp"]``,
    a float) plus (path, depth). *gen* is passed in by the caller, captured
    right after the ``_ensure_tree_and_index`` await that produced *tree*,
    rather than re-read from ``request.app.state`` here — a future ``await``
    between the tree fetch and this call could otherwise race a concurrent
    rebuild and desync the two. Using the timestamp (not ``id(tree)``) also
    avoids a stale hit if a freed tree's list address gets reused by a later
    allocation once ``metadata_index_cache`` has been cleared (e.g. test
    teardown setting it to ``None``). A generation change discards the whole
    cache so it never grows unbounded. Within a generation, ``json.dumps`` +
    sha1 run at most once per (path, depth) — a 304 never re-serializes either.

    ETag input = stable payload hash + path + depth only (no cache
    timestamp), so it stays the same across an index-cache TTL refresh that
    left the tree unchanged, and changes only when the tree itself changes.
    The fill generation (*gen_int*) is appended as a suffix, so a
    background-filled tree revalidates even when the digest prefix matches.
    """
    body_cache = getattr(request.app.state, "folders_body_cache", None)
    if (
        body_cache is None
        or body_cache.get("gen") != gen
        or body_cache.get("gen_int") != gen_int
    ):
        body_cache = {"gen": gen, "gen_int": gen_int, "entries": {}}
        request.app.state.folders_body_cache = body_cache

    cache_key = (key_ns, path, depth)
    entry = body_cache["entries"].get(cache_key)
    if entry is None:
        body_str = json.dumps(
            payload, ensure_ascii=False, allow_nan=False, indent=None,
            separators=(",", ":"), default=str,
        )
        body = body_str.encode("utf-8")
        digest = hashlib.sha1(f"{key_ns}:{path}:{depth}:{body_str}".encode("utf-8")).hexdigest()
        etag = f'"{digest[:24]}-g{gen_int}"'
        entry = (body, etag)
        body_cache["entries"][cache_key] = entry
    body, etag = entry

    headers = {"ETag": etag, "Cache-Control": _FOLDERS_CACHE_CONTROL}
    if getattr(request, "headers", None) is not None and request.headers.get(
        "if-none-match"
    ) == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


@api_router.get("/api/folders")
async def api_folders(
    request: Request,
    response: Response = None,  # type: ignore[assignment] — injected per request; None on direct unit calls
    path: str = Query("", description="Subtree path relative to downloads dir (empty = root)"),
    depth: _QueryInt | None = Query(None, description="1 = direct child folders only; omitted = full tree"),
    since: _QueryInt | None = Query(None, description="Tree generation last seen; match = 304, stale = tree+generation"),
    _user: dict = Depends(require_auth),
):
    """List folders as a recursive tree with metadata. Requires authentication.

    Shallow mode (``depth=1``) returns only the direct ``type == folder``
    children of *path*, each with ``children == []`` (never None) and WITHOUT
    ``metadata``/``subtree_facets`` keys — facet filters apply after a
    full-tree fetch. The shallow shape is deliberately NOT a full tree, so the
    frontend must only OfflineTree.putTree() full-tree snapshots. Flattened
    names (containing "/") are returned verbatim.
    """
    return await _folder_tree(request, path, depth, since, v1=False)


async def _folder_tree(
    request: Request,
    path: str,
    depth: int | None,
    since: int | None,
    *,
    v1: bool = False,
):
    """Shared core of ``/api/folders`` (v0) and ``/api/v1/tree`` (v1).

    v1 wraps every payload in the spec envelope
    ``{kind: tree|children, nodes, generation}``; v0 keeps the bare list
    (or ``{tree, generation}`` on a ``since`` mismatch). ``key_ns`` keeps
    the shared body/ETag cache from serving one version's shape at the
    other's URL.
    """
    # Direct unit calls pass the raw Query defaults; treat them as omitted.
    if not isinstance(path, str):
        path = ""
    if not isinstance(depth, int):
        depth = None
    if not isinstance(since, int) or isinstance(since, bool):
        since = None
    if depth is not None and depth < 1:
        raise HTTPException(status_code=400, detail="depth must be >= 1")
    request.app.state.last_request_at = time.time()
    key_ns = "v1" if v1 else "v0"

    def _wrap(nodes: list[dict], kind: str, gen_int: int):
        if not v1:
            return nodes
        return {"kind": kind, "nodes": nodes, "generation": gen_int}

    if depth is None:
        # Legacy full tree — byte-compatible default (search/offline/tests).
        # Tree/index come from the shared TTL cache; Todo 6 (HTTP cache) can
        # key its ETag off the same cache timestamp.
        try:
            tree, index = await _ensure_tree_and_index(request)
        except _IndexBuilding:
            # no-store, never _FOLDERS_CACHE_CONTROL: a private 60s-cached
            # 202 would feed the client's retry loop stale bodies long after
            # the build landed (and stay out of the SW api cache too).
            return JSONResponse(
                {"detail": "metadata index building"},
                status_code=202,
                headers={"Retry-After": "2", "Cache-Control": "no-store"},
            )
        # Captured immediately after the await, with no intervening await, so
        # it cannot desync from the *tree* a concurrent rebuild may replace.
        cache_data = request.app.state.metadata_index_cache
        gen = cache_data["timestamp"]
        gen_int = int(cache_data.get("generation", 1) or 1)
        if since is not None and since == gen_int:
            return Response(status_code=304, headers={"Cache-Control": _FOLDERS_CACHE_CONTROL})
        lookup = {c.folder_path: c for c in index.comics}
        _attach_metadata(tree, lookup, "")
        subtree = index.subtree_facets()
        _attach_subtree_facets(tree, subtree, "")
        if since is not None:
            if v1:
                return JSONResponse({"kind": "tree", "nodes": tree, "generation": gen_int})
            return JSONResponse({"tree": tree, "generation": gen_int})
        return _folders_response(request, _wrap(tree, "tree", gen_int), "", None, gen, gen_int, key_ns)
    norm = path.rstrip("/")
    downloads_path = request.app.state.settings.downloads_path
    target_fs = downloads_path if not norm else downloads_path / norm
    if not _is_within(downloads_path, target_fs):
        raise HTTPException(status_code=403, detail="path outside downloads dir")
    # Shallow mode NEVER waits on the full scan: a fresh index cache serves
    # it from the tree, otherwise a single-level live-FS read serves it.
    # Neither path calls _ensure_tree_and_index / _ensure_index /
    # _scan_and_rebuild, and an expired cache is never served.
    # Transient degradations while the full scan has not run yet: dirs carry
    # no thumb (the client renders its placeholder — render.js / resolveCover.js
    # already handle thumb == null, no client change needed), and drilling
    # into an archive yields [] until a full scan materializes its children.
    cache_data = getattr(request.app.state, "metadata_index_cache", None)
    if (
        cache_data is not None
        and (time.time() - cache_data["timestamp"]) <= INDEX_CACHE_TTL
        and "tree" in cache_data
    ):
        tree = cache_data["tree"]
        gen = cache_data["timestamp"]
        gen_int = int(cache_data.get("generation", 1) or 1)
        if not norm:
            target_children = tree
        else:
            node = _find_tree_node(tree, norm)
            if node is None or node.get("type") != "folder":
                raise HTTPException(status_code=404, detail="folder not found")
            target_children = node.get("children") or []
        # Copies, not views into the cached tree (children must stay intact there).
        shallow = [{**c, "children": []} for c in target_children if c.get("type") == "folder"]
        return _folders_response(request, _wrap(shallow, "children", gen_int), norm, depth, gen, gen_int, key_ns)
    # Live-FS fallback: list one directory level directly. Archives/PDFs are
    # classified by suffix membership only — never opened, images never
    # stat'ed. Deliberately bypasses folders_body_cache: that cache is keyed
    # by the index-cache generation (a float timestamp), and storing this
    # payload under a stale/None generation would let a later request hit a
    # body built from a different tree.
    try:
        on_disk = target_fs.is_dir()
    except OSError:
        on_disk = False
    if not on_disk:
        if norm and not target_fs.exists():
            # Same 404 rule as the tree path: a nested path that resolves to
            # nothing has no listing. An existing non-directory (archive/pdf
            # file) falls through to [] — nothing lives beneath it.
            raise HTTPException(status_code=404, detail="folder not found")
        shallow = []
    else:
        try:
            fs_children = list(target_fs.iterdir())
        except OSError:
            fs_children = None
        if fs_children is None:
            shallow = []
        else:
            archives: list[str] = []
            pdfs: list[str] = []
            dirs: list[str] = []
            for entry in fs_children:
                name = entry.name
                if name.startswith("."):
                    continue
                if entry.is_dir():
                    dirs.append(name)
                elif entry.is_file():
                    suffix = entry.suffix.lower()
                    if suffix in ARCHIVE_EXTENSIONS:
                        archives.append(name)
                    elif suffix in PDF_EXTENSIONS:
                        pdfs.append(name)
            archives.sort(key=lambda n: (natural_key(n), n))
            pdfs.sort(key=lambda n: (natural_key(n), n))
            dirs.sort(key=lambda n: (natural_key(n), n))
            shallow = (
                [{"name": n, "type": "folder", "children": [], "thumb": None, "archive": True}
                 for n in archives]
                + [{"name": n, "type": "folder", "children": [], "thumb": None,
                     "archive": True, "pdf": True} for n in pdfs]
                + [{"name": n, "type": "folder", "children": []} for n in dirs]
            )
    fallback_gen = (
        int(cache_data.get("generation", 1) or 1) if isinstance(cache_data, dict) else 0
    )
    body_str = json.dumps(
        _wrap(shallow, "children", fallback_gen), ensure_ascii=False, allow_nan=False, indent=None,
        separators=(",", ":"), default=str,
    )
    body = body_str.encode("utf-8")
    digest = hashlib.sha1(f"{key_ns}:{norm}:{depth}:{body_str}".encode("utf-8")).hexdigest()
    etag = f'"{digest[:24]}"'
    headers = {"ETag": etag, "Cache-Control": _FOLDERS_CACHE_CONTROL}
    if getattr(request, "headers", None) is not None and request.headers.get(
        "if-none-match"
    ) == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


async def _visibility_hidden_roots(request: Request, user: dict) -> tuple[str, ...]:
    """Caller's hidden roots for metadata exclusion.

    Shares the visibility route's own user-key/store helpers (same 403/503
    posture) — a failed read fails CLOSED, never serving an unfiltered list.
    Lazy import: folder_visibility_routes already depends on this module's
    tree/index helpers.
    """
    from app.folder_visibility_routes import hidden_roots_for_request
    return await hidden_roots_for_request(request, user)


async def _search_impl(request: Request, q: str, artist: str, tag: str, sort: str, include_hidden: bool, user: dict):
    """Shared ``/api/search`` + ``/api/v1/search`` core.

    Comics under the caller's hidden roots are excluded server-side —
    shipping them and filtering client-side would still leak them in the
    response body. ``include_hidden`` opts out for the owner's own 全表示
    mode (the caller's own hidden set — never another user's).
    """
    try:
        index = await _ensure_index(request)
    except _IndexBuilding:
        return JSONResponse(
            {"detail": "metadata index building"},
            status_code=202,
            headers={"Retry-After": "2", "Cache-Control": "no-store"},
        )
    hidden = await _visibility_hidden_roots(request, user)
    if include_hidden:
        hidden = ()

    filters: dict[str, list[str]] = {}
    if artist:
        filters["artist"] = [artist]
    if tag:
        filters["tags"] = [t.strip() for t in tag.split(",") if t.strip()]

    results = index.search(q=q, filters=filters, sort=sort, exclude_roots=hidden)
    return {"query": q, "total": len(results), "results": results}


@api_router.get("/api/search")
async def search(
    request: Request,
    q: str = Query("", description="Free text search"),
    artist: str = Query("", description="Artist filter"),
    tag: str = Query("", description="Tags (comma-separated)"),
    sort: str = Query("relevance", description="Sort: relevance"),
    include_hidden: bool = Query(False, description="全表示 mode: do not exclude the caller's hidden roots"),
    _user: dict = Depends(require_auth),
):
    """Search comics by text and facet filters. Requires authentication."""
    return await _search_impl(request, q, artist, tag, sort, include_hidden, _user)


async def _facets_impl(request: Request, path: str, include_hidden: bool, user: dict):
    """Shared ``/api/metadata/facets`` + ``/api/v1/facets`` core.

    Hidden roots are excluded server-side — a facet value existing only on
    hidden works must never surface. ``include_hidden`` opts out for the
    owner's own 全表示 mode (the caller's own hidden set — never another's).
    """
    try:
        index = await _ensure_index(request)
    except _IndexBuilding:
        return JSONResponse(
            {"detail": "metadata index building"},
            status_code=202,
            headers={"Retry-After": "2", "Cache-Control": "no-store"},
        )
    hidden = await _visibility_hidden_roots(request, user)
    if include_hidden:
        hidden = ()
    return index.facets_for(path, exclude_roots=hidden)


@api_router.get("/api/metadata/facets")
async def get_facets(
    request: Request,
    path: str = Query("", description="Subtree path relative to downloads dir (empty = whole library)"),
    include_hidden: bool = Query(False, description="全表示 mode: do not exclude the caller's hidden roots"),
    _user: dict = Depends(require_auth),
):
    """Return available facet filter options. Requires authentication.

    Contract: returns {artists, tags} only (no parodies key).
    """
    return await _facets_impl(request, path, include_hidden, _user)


@api_router.post("/api/refresh")
async def refresh_index(request: Request, _user: dict = Depends(require_auth)):
    """Manually rebuild the metadata index. Call after adding new content.

    Synchronous rebuild under the shared index lock: the response is sent
    only after the fresh tree+index are cached, so the very next
    /api/folders read sees the new content. Stamps a fresh timestamp, so
    the TTL auto-refresh path keeps working unchanged afterwards (the
    folders ETag/body cache keys off the timestamp generation and rolls
    over automatically).
    """
    if _index_disabled():
        raise _IndexDisabled()
    async with _index_lock:
        downloads_path = request.app.state.settings.downloads_path
        tree, index, _folders, _images, _archives, _epubs, _rars, _pdfs = await _scan_and_rebuild(downloads_path)
        sig = await run_in_threadpool(compute_signature, downloads_path)
        # Cache only after both succeed (cache-nothing on failure).
        old = getattr(request.app.state, "metadata_index_cache", None)
        prev_gen = old.get("generation", 1) if isinstance(old, dict) else 1
        request.app.state.metadata_index_cache = {
            "tree": tree,
            "index": index,
            "timestamp": time.time(),
            "generation": prev_gen,
            "signature": sig,
        }
    return {"status": "ok", "folders": len(index.comics)}


def _list_folder_images(downloads_dir: Path, folder: str) -> list[str]:
    """Synchronous enumeration core of ``/api/images`` — call off the event loop.

    Shared with the dual-reader manifest builder (``app.reader_sync.catalog``)
    so the manifest's fetch-path listing is the route's listing, not a second
    implementation. Identical order and identical failures: containment first
    (403), then archive-file / pdf-file / archive-inner-prefix / plain-dir
    resolution, with 404 for a missing folder or an absent inner prefix.
    """
    folder_path = downloads_dir / folder

    if not _is_within(downloads_dir, folder_path):
        raise HTTPException(status_code=403, detail="Forbidden")

    folder_path = folder_path.resolve()
    if is_archive_file(folder_path):
        return list_archive_entries(folder_path)
    if is_pdf_file(folder_path):
        from app.archive_pdf import pdf_page_count, pdf_page_name

        count = pdf_page_count(folder_path)
        return [pdf_page_name(i) for i in range(count)]
    resolved = _resolve_archive(downloads_dir, folder)
    if resolved is not None:
        archive, inner = resolved
        if not _is_within(downloads_dir, archive):
            raise HTTPException(status_code=403, detail="Forbidden")
        scoped = _list_archive_prefix(archive.resolve(), inner)
        if not scoped:
            raise HTTPException(status_code=404, detail=f"Folder not found: {folder}")
        return scoped
    if not folder_path.is_dir():
        raise HTTPException(status_code=404, detail=f"Folder not found: {folder}")

    return list_images(folder_path)


@api_router.get("/api/images")
async def api_images(
    request: Request,
    folder: str = Query(..., description="Folder path relative to downloads dir"),
    _user: dict = Depends(require_auth),
):
    """List images in a folder — or inside a ZIP/CBZ/PDF archive. Requires authentication."""
    downloads_dir = request.app.state.settings.downloads_path
    return await run_in_threadpool(_list_folder_images, downloads_dir, folder)


def _resolve_archive(downloads_dir: Path, path: str) -> tuple[Path, str] | None:
    """Split *path* into (archive_file, inner_entry) if a leading prefix is an archive/PDF.

    Longest-prefix match so nested names (``sub/work.zip``) resolve before
    shorter ones. Returns None when no prefix is an archive file.
    """
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    for i in range(len(parts) - 1, 0, -1):
        candidate = downloads_dir.joinpath(*parts[:i])
        try:
            if is_archive_file(candidate) or is_pdf_file(candidate):
                return candidate, "/".join(parts[i:])
        except (OSError, ValueError):
            # ValueError: embedded null byte makes stat() raise instead of
            # returning False — treat like a missing candidate, never a 500.
            continue
    return None


def _list_archive_prefix(archive: Path, inner: str) -> list[str]:
    """Return archive entries under the inner-directory *prefix*, relative to it.

    Names stay relative (``v01/a.png`` under prefix ``v01`` → ``a.png``) so
    the viewer/offline ``/image/<folder>/<name>`` construction keeps working
    with ``folder=<archive>/<prefix>``. A file entry or unknown prefix
    yields ``[]`` (the caller turns that into 404).
    """
    prefix = inner.strip("/")
    if not prefix or prefix in (".", "..") or ".." in prefix.split("/"):
        return []
    return [e[len(prefix) + 1:] for e in list_archive_entries(archive)
            if e.startswith(prefix + "/")]


def _parse_range(range_header: str, size: int) -> tuple[int, int] | None:
    """Parse a simple ``bytes=start-end`` Range header. Returns (start, end) inclusive."""
    m = re.match(r"bytes=(\d*)-(\d*)$", range_header.strip())
    if not m:
        return None
    s, e = m.group(1), m.group(2)
    if s == "" and e == "":
        return None
    if s == "":
        # Suffix range: last N bytes.
        n = int(e)
        if n <= 0:
            return None
        start = max(0, size - n)
        return start, size - 1
    start = int(s)
    end = int(e) if e != "" else size - 1
    if start >= size or end < start:
        return None
    return start, min(end, size - 1)


def _not_modified(request: Request, etag: str, mtime: float) -> bool:
    """Evaluate If-None-Match / If-Modified-Since preconditions."""
    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match == etag:
        return True
    if_modified_since = request.headers.get("if-modified-since")
    if if_modified_since:
        try:
            mod_since = utils.parsedate_to_datetime(if_modified_since)
            if mod_since.tzinfo is None:
                mod_since = mod_since.replace(tzinfo=datetime.timezone.utc)
            # Last-Modified is second-precision: truncate mtime so a client
            # echoing the exact header revalidates instead of refetching.
            if mod_since.tzinfo is not None and int(mtime) <= mod_since.timestamp():
                return True
        except (TypeError, ValueError):
            pass
    return False


def _archive_bytes_response(
    request: Request, data: bytes, media_type: str, etag: str, last_modified: str, mtime: float
) -> Response:
    """Return bytes with ETag/Last-Modified, conditional 304, and Range 206 support."""
    if _not_modified(request, etag, mtime):
        return Response(status_code=304)
    # `private`, not `public`: shared caches (CDN edge) must not store
    # authenticated image bytes — they would re-serve them without auth.
    headers = {
        "Cache-Control": "private, max-age=3600",
        "ETag": etag,
        "Last-Modified": last_modified,
        "Accept-Ranges": "bytes",
    }
    range_header = request.headers.get("range")
    if range_header:
        parsed = _parse_range(range_header, len(data))
        if parsed is not None:
            start, end = parsed
            headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
            return Response(
                content=data[start : end + 1],
                status_code=206,
                media_type=media_type,
                headers=headers,
            )
    return Response(content=data, media_type=media_type, headers=headers)


async def _serve_archive_entry(
    archive: Path, inner: str, request: Request
) -> Response:
    """Serve one entry from a ZIP/CBZ archive or one rendered PDF page.

    Entries carry ETag/Last-Modified with conditional (304) and Range (206)
    support so the viewer and offline cache revalidate instead of refetching.
    """
    from app.archive import ArchiveToolMissing, _is_image_entry, read_archive_entry
    from app.archive_pdf import parse_pdf_page_name, pdf_page_count, render_pdf_page

    if not inner or inner.startswith("/") or ".." in inner.split("/"):
        raise HTTPException(status_code=400, detail="Invalid image path")

    try:
        stat = archive.stat()
    except (OSError, ValueError):
        # ValueError: embedded null byte makes stat() raise instead of
        # failing with OSError — map to 404 like any other stat failure.
        raise HTTPException(status_code=404, detail="File not found")
    last_modified = utils.formatdate(stat.st_mtime, usegmt=True)

    if is_pdf_file(archive):
        count = await run_in_threadpool(pdf_page_count, archive)
        idx = parse_pdf_page_name(inner, count)
        if idx is None:
            raise HTTPException(status_code=404, detail="File not found")
        etag = f'"{stat.st_mtime:.0f}-{stat.st_size}-pdf{idx}"'
        if _not_modified(request, etag, stat.st_mtime):
            return Response(status_code=304)
        data = await run_in_threadpool(render_pdf_page, archive, idx)
        if data is None:
            raise HTTPException(
                status_code=501,
                detail="PDF rendering requires pypdfium2 (pip install pypdfium2)",
            )
        return _archive_bytes_response(
            request, data, "image/png", etag, last_modified, stat.st_mtime
        )

    # ZIP/CBZ/RAR/EPUB: read the stored bytes on demand — DOWNLOADS_DIR is untouched.
    if not _is_image_entry(inner):
        raise HTTPException(status_code=404, detail="File not found")
    # ETag must stay ASCII: Starlette encodes headers as latin-1 and a raw
    # non-ASCII inner name (e.g. Japanese) 500s. Hash the inner name like the
    # /api/folders ETag does; mtime+size+name-hash keeps the same semantics.
    inner_digest = hashlib.sha1(inner.encode("utf-8")).hexdigest()[:16]
    etag = f'"{stat.st_mtime:.0f}-{stat.st_size}-{inner_digest}"'
    if _not_modified(request, etag, stat.st_mtime):
        return Response(status_code=304)

    try:
        data = await run_in_threadpool(read_archive_entry, archive, inner)
    except ArchiveToolMissing as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    if data is None:
        raise HTTPException(status_code=404, detail="File not found")
    ctype, _ = mimetypes.guess_type(inner)
    return _archive_bytes_response(
        request, data, ctype or "application/octet-stream",
        etag, last_modified, stat.st_mtime,
    )


@api_router.get("/image/{path:path}")
async def serve_image(
    path: str,
    request: Request,
    _user: dict = Depends(require_auth),
):
    """Serve an image file. Requires authentication. Path is folder/subfolder/.../filename."""
    downloads_dir = request.app.state.settings.downloads_path

    resolved = _resolve_archive(downloads_dir, path)
    if resolved is not None:
        archive, inner = resolved
        if not _is_within(downloads_dir, archive):
            raise HTTPException(status_code=403, detail="Forbidden")
        return await _serve_archive_entry(archive.resolve(), inner, request)

    # Root-level loose files arrive as a bare filename (no folder segment);
    # let them flow into the guards below. Traversal stays blocked:
    # ".." resolves outside downloads_dir (-> 403 via _is_within).
    if path.startswith("/") or path.endswith("/"):
        raise HTTPException(status_code=400, detail="Invalid image path")

    file_path = downloads_dir / path

    if not _is_within(downloads_dir, file_path):
        raise HTTPException(status_code=403, detail="Forbidden")

    file_path = file_path.resolve()
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    stat = file_path.stat()
    ctype, _ = mimetypes.guess_type(str(file_path))
    etag = f'"{stat.st_mtime:.0f}-{stat.st_size}"'
    last_modified = utils.formatdate(stat.st_mtime, usegmt=True)

    # Conditional request handling (If-None-Match / If-Modified-Since)
    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match == etag:
        return Response(status_code=304)

    if_modified_since = request.headers.get("if-modified-since")
    if if_modified_since:
        try:
            mod_since = utils.parsedate_to_datetime(if_modified_since)
            if mod_since.tzinfo is None:
                mod_since = mod_since.replace(tzinfo=datetime.timezone.utc)
            if mod_since.tzinfo is not None and stat.st_mtime <= mod_since.timestamp():
                return Response(status_code=304)
        except (TypeError, ValueError):
            pass  # Invalid date header — proceed normally

    return FileResponse(
        path=str(file_path),
        stat_result=stat,
        media_type=ctype or "application/octet-stream",
        headers={
            "Cache-Control": "private, max-age=3600",
            "ETag": etag,
            "Last-Modified": last_modified,
        },
    )


@api_router.get("/thumb/{path:path}")
async def serve_thumb(
    path: str,
    request: Request,
    w: str = Query(default="400"),
    _user: dict = Depends(require_auth),
):
    """Serve a resized JPEG thumbnail. Requires authentication. Path mirrors /image/*."""
    from app.thumbs import handle_thumb

    request.app.state.last_request_at = time.time()
    return await handle_thumb(request, path, w)


@api_router.get("/view/{path:path}")
async def serve_view(
    path: str,
    request: Request,
    w: str | None = Query(default=None),
    _user: dict = Depends(require_auth),
):
    """Serve a reader page normalized for fast decode. Requires authentication.

    ``?w=<px>`` requests a smaller rendition (clamped) for flip-through
    fast tiers; absent keeps passthrough/2560-cap behaviour.
    """
    from app.thumbs import handle_view

    request.app.state.last_request_at = time.time()
    return await handle_view(request, path, w)


@api_router.get("/api/thumb-sprite")
async def serve_thumb_sprite(
    request: Request,
    _user: dict = Depends(require_auth),
):
    """Serve a contact-sheet sprite for same-archive multi-thumb grids. Requires authentication."""
    from app.thumbs import handle_thumb_sprite

    request.app.state.last_request_at = time.time()
    return await handle_thumb_sprite(request)


# ── Reading position (server-held; v1 per-folder, v0 single-latest) ────
#
# The "続きから" pill mirrors its newest localStorage entry here so a fresh
# device (or a browser restart) can resume where another device left off.
# Records live in the shared user-state SQLite DB (api/user-state-schema.sql
# v2) via app.reading_positions — per-user slots survive restarts and are
# shared with viewer-rs when BOOKQ_RS_NATIVE=positions is enabled (#375).
# The stored folder path is opaque data: it is never resolved against the
# filesystem here, only echoed back, so no containment check applies.
#
# Slot rules (LWW, tombstones, future-ts guard, bounds) live in
# app.reading_positions.PositionBook — the same rules viewer-rs ports,
# pinned by tests/fixtures/positions_cases.json. Handlers below only do
# HTTP validation and adapt store results to status codes.


def _position_store(request: Request) -> ReadingPositionStore:
    """Return the SQLite position store, lazily created on app.state.

    getattr-first so SimpleNamespace stub states (tests) work the same as
    the real Starlette State; construction performs no I/O.
    """
    state = request.app.state
    store = getattr(state, "reading_position_store", None)
    if not isinstance(store, ReadingPositionStore):
        settings = getattr(state, "settings", None)
        path = getattr(settings, "state_path", None)
        if path is None:
            state_dir = os.environ.get("BOOKQ_STATE_DIR", "").strip()
            base = (
                Path(state_dir).expanduser()
                if state_dir
                else Path.home() / ".local" / "share" / "bookq"
            )
            path = base / DB_FILENAME
        store = ReadingPositionStore(Path(path))
        try:
            state.reading_position_store = store
        except Exception:
            pass
    return store


async def _position_store_call(fn, *args):
    """Run one store operation off the event loop; failures -> generic 503.

    Only the exception type is logged: DB messages can carry SQL, local
    paths or user data, none of which belong in logs or the response.
    """
    try:
        return await run_in_threadpool(fn, *args)
    except Exception as exc:
        logger.warning("reading-position store failure type=%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="reading-position store unavailable")


def _position_user_key(user: dict) -> str:
    """The caller's position-store key — 403 when the session carries no
    usable email (never a shared "unknown" bucket: that would let one
    malformed session read or wipe another account's positions)."""
    key = position_user_key(user)
    if key is None:
        raise HTTPException(status_code=403, detail="session has no usable identity")
    return key


def _validate_folder_param(folder) -> str:
    """Validate a folder query/path param against the v0 body rules."""
    if (
        not isinstance(folder, str)
        or not folder
        or len(folder) > 2048
        or folder.startswith("/")
        or "\x00" in folder
    ):
        raise HTTPException(status_code=400, detail="invalid folder")
    return folder


# SQLite INTEGER / Rust i64 bound — viewer-rs's as_int rejects anything
# outside i64, and an out-of-range Python int would OverflowError inside
# sqlite3 and surface as a bogus 503 instead of a validation 400.
_INT64_MIN = -(1 << 63)
_INT64_MAX = (1 << 63) - 1


def _as_int(v) -> int | None:
    """JSON Schema `integer` admits integral floats (2716.0) — coerce them.

    bools, non-integral/non-finite values and ints outside the i64 range
    the store can hold stay rejected.
    """
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        n = v
    elif isinstance(v, float) and math.isfinite(v) and v.is_integer():
        n = int(v)
    else:
        return None
    return n if _INT64_MIN <= n <= _INT64_MAX else None


def _validate_position_body_v1(body) -> dict:
    """Validate a v1 PUT body ``{page,total,ts}`` — folder comes from the query."""
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected JSON object")
    page = _as_int(body.get("page"))
    if page is None or page < 0:
        raise HTTPException(status_code=400, detail="invalid page")
    total = _as_int(body.get("total"))
    if total is None or total < 1:
        raise HTTPException(status_code=400, detail="invalid total")
    ts = _as_int(body.get("ts"))
    if ts is None or ts < 0:
        raise HTTPException(status_code=400, detail="invalid ts")
    return {"page": page, "total": total, "ts": ts}


def _validate_position_payload(body) -> dict:
    """Validate a PUT body, returning the normalized record. Raises 400."""
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected JSON object")
    folder = body.get("folder")
    if (
        not isinstance(folder, str)
        or not folder
        or len(folder) > 2048
        or folder.startswith("/")
        or "\x00" in folder
    ):
        raise HTTPException(status_code=400, detail="invalid folder")
    page = _as_int(body.get("page"))
    if page is None or page < 0:
        raise HTTPException(status_code=400, detail="invalid page")
    total = _as_int(body.get("total"))
    if total is None or total < 1:
        raise HTTPException(status_code=400, detail="invalid total")
    ts = body.get("ts")
    if ts is None:
        ts = int(time.time() * 1000)
    else:
        ts = _as_int(ts)
        if ts is None or ts < 0:
            raise HTTPException(status_code=400, detail="invalid ts")
    return {"folder": folder, "page": page, "total": total, "ts": ts}


@api_router.get("/api/reading-position")
async def get_reading_position(request: Request, user: dict = Depends(require_auth)):
    """Return the caller's single latest reading position (null when unset).

    v0 single-slot view over the per-folder store: the newest non-tombstone
    record across all folders.
    """
    key = _position_user_key(user)
    position = await _position_store_call(_position_store(request).latest, key)
    return JSONResponse(
        content={"position": position},
        headers={"Cache-Control": "no-store"},
    )


@api_router.put("/api/reading-position")
async def put_reading_position(request: Request, user: dict = Depends(require_auth)):
    """Store the caller's latest reading position — newest client ts wins.

    Arrival order alone can't express "this PUT left its device before the
    stored record was written" (retry backlogs, keepalive retransmits,
    cross-device writes), so the newest stored record rejects
    strictly-older PUTs. A tombstone (left by DELETE) also rejects the
    wiped record itself — the wiped ts is still "equal", not newer — so
    another device's self-heal can't resurrect a wiped position.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    rec = _validate_position_payload(body)
    key = _position_user_key(user)
    result = await _position_store_call(_position_store(request).put_v0, key, rec)
    # REJECTED = far-future ts: one wrong-clock device must not poison the
    # slot — an unchecked future ts would reject every honest PUT as
    # stale, and even the DELETE tombstone would inherit it. Clamping is
    # NOT a fix: a continuously-writing skewed device would keep its
    # record ~slack ahead of real time and block honest writes forever.
    # Dropping (400) leaves the honest record in place; the client treats
    # non-retryable 4xx as a drop, so a wrong-clock device simply doesn't
    # sync until its clock is fixed.
    if result == REJECTED:
        raise HTTPException(status_code=400, detail="ts too far in the future")
    if result == STALE:
        return {"status": "stale"}
    return {"status": "ok"}


@api_router.delete("/api/reading-position")
async def delete_reading_position(
    request: Request, hard: bool = False, user: dict = Depends(require_auth)
):
    """Clear the caller's stored reading positions (harness 全消去).

    Wipes every folder record and leaves a tombstone carrying the newest
    wiped ts: PUTs at-or-older than it are rejected, so a stale in-flight
    write or another device's boot self-heal of the same records can't
    resurrect them. A genuinely newer read always lands.

    ``?hard=1`` (test mode only) removes the slot entirely — the conftest
    reset fixture needs a truly clean slate, since a tombstone would
    legitimately reject the next test's freshly-written record.
    """
    key = _position_user_key(user)
    store = _position_store(request)
    if hard and auth_module._TEST_MODE:
        await _position_store_call(store.delete_user, key)
        return {"status": "ok"}
    await _position_store_call(store.delete_all, key)
    return {"status": "ok"}
