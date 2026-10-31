"""bookq — FastAPI image viewer with Google OAuth authentication."""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from app.compression import ApiGZipMiddleware
from app.config import load_settings
from app.auth import router as auth_router, init_oauth
from app.auth_token import token_router
from app.folder_visibility import FolderVisibilityStore
from app.folder_visibility_routes import router as folder_visibility_router
from app.routes import _background_fill, _warm_covers, api_router, warmup_metadata_index
from app.reader_sync.sessions import SessionRegistry
from app.reader_sync.transport import reader_sync_ws, resolve_reader_work
from app.static_files import NoCacheStaticFiles

logger = logging.getLogger(__name__)

# Lifespan startup blocks /api/health (lifespan completes before the server
# serves), so a slow scan eats the compose healthcheck budget: start_period 2s
# + retries 15 x interval 2s ~= 32s. Past that, `restart: unless-stopped` turns
# a merely slow library into a restart loop, which the failure-tolerant
# `except Exception` below cannot prevent because it only covers errors, not
# slowness. Bounding the warmup keeps a slow scan degrading to the pre-warmup
# behaviour (lazy first request) instead of an unhealthy container. Observed
# worst case was ~9s pre-optimization; post-optimization (T4 parallel archive
# enumeration + T5 overlapped heavy imports) the T3 fixed-scale synthetic tree
# (zip x30 / epub x10 / pdf x10 / plain x50, folders=110 images=270) rebuilds
# cold in ~35ms: scan_ms=34 rebuild_ms=1 (2026-10-04, Phase 1 direct warmup);
# lifespan elapsed_ms=32 import_ms=25 (Phase 2, import_ms is the standalone
# reference since T5 overlaps imports with the scan). 20s timeout retained as
# headroom for real libraries without truncating healthy scans.
# Log-grep recipe: `docker compose logs viewer | grep "index rebuilt"` for
# scan_ms/rebuild_ms + archives/epubs/rars/pdfs species counts, and compare
# `内部ウォームアップ完了 ... import_ms=` against elapsed_ms for the import share.
WARMUP_TIMEOUT_SECONDS = 20.0


def _configure_logging() -> None:
    """Make INFO records from the ``app.*`` loggers reach stderr (docker logs).

    Only the ``app`` logger gets a handler and level, deliberately not the root
    logger: hypercorn's ``hypercorn.error`` logger propagates to root, so a root
    ``basicConfig`` prints every hypercorn line twice. Neither hypercorn nor
    uvicorn configures root, so without this INFO records are dropped.
    Level comes from ``BOOKQ_LOG_LEVEL`` (default ``INFO``); an unknown
    value falls back to INFO instead of failing startup.
    """
    app_logger = logging.getLogger("app")
    if not app_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(process)d] [%(levelname)s] %(name)s: %(message)s",
            "[%Y-%m-%d %H:%M:%S %z]",
        ))
        app_logger.addHandler(handler)
    level_name = os.getenv("BOOKQ_LOG_LEVEL", "INFO").upper()
    level = logging.getLevelName(level_name)
    app_logger.setLevel(level if isinstance(level, int) else logging.INFO)


def _import_pypdfium2() -> None:
    import pypdfium2  # noqa: F401


def _import_rarfile() -> None:
    import rarfile  # noqa: F401


def _import_pil() -> None:
    from PIL import Image, ImageOps  # noqa: F401


async def _warmup_imports() -> float:
    """Fault heavy backends into memory, overlapped with the warmup scan.

    Each import is independent (one missing backend must not skip the others)
    and non-fatal (WARNING + continue — never raise; lazy-import fallbacks in
    app/archive.py, app/archive_pdf.py, app/thumbs.py stay intact). Each import
    runs via ``asyncio.to_thread`` so the three fault-ins overlap the scan
    instead of serializing in front of it. Returns the import-only wall time in
    ms, logged by lifespan as the ``import_ms`` reference value (the overlapped
    wall-time contribution itself is not separable, so no numeric assertions).
    On timeout the to_thread workers run to completion with results discarded.
    """
    pid = os.getpid()
    import_start = time.perf_counter()
    try:
        await asyncio.to_thread(_import_pypdfium2)
    except Exception as exc:
        logger.warning("warmup import skip pypdfium2 error=%s pid=%s", exc, pid)
    try:
        await asyncio.to_thread(_import_rarfile)
    except Exception as exc:
        logger.warning("warmup import skip rarfile error=%s pid=%s", exc, pid)
    try:
        await asyncio.to_thread(_import_pil)
    except Exception as exc:
        logger.warning("warmup import skip PIL error=%s pid=%s", exc, pid)
    return (time.perf_counter() - import_start) * 1000


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup warmup: one scan+index pass before serving.

    Populates the same metadata_index_cache the request path uses, so the
    first real request is a cache hit. Failures are non-fatal (WARNING +
    continue) — a wedged warmup must never trap the container in a
    healthcheck-fail loop. Runs under both uvicorn (dev/test) and hypercorn
    (Dockerfile ENTRYPOINT) via the standard ASGI lifespan protocol.
    """
    pid = os.getpid()
    started = time.perf_counter()
    try:
        # Overlap the heavy eager imports with the scan: both run as tasks
        # under one wait_for+gather, so imports no longer serialize behind
        # scan start on real data.
        scan_task = asyncio.create_task(warmup_metadata_index(app.state))
        import_task = asyncio.create_task(_warmup_imports())
        try:
            # wait_for cancels the gather (hence both tasks), not the
            # threadpool worker running the scan; that worker finishes its
            # pass and is discarded (nothing caches its result, so the cache
            # stays empty and the first request simply scans again). The
            # import side shares the same abandon semantics (to_thread workers
            # run to completion, results dropped).
            (folders, images, _), import_ms = await asyncio.wait_for(
                asyncio.gather(scan_task, import_task, return_exceptions=False),
                timeout=WARMUP_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "内部ウォームアップタイムアウト timeout_s=%.0f pid=%s",
                WARMUP_TIMEOUT_SECONDS, pid,
            )
        else:
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.info(
                "内部ウォームアップ完了 elapsed_ms=%.0f folders=%d images=%d pid=%s import_ms=%.0f",
                elapsed_ms, folders, images, pid, import_ms,
            )
    except Exception as exc:
        logger.warning(
            "内部ウォームアップ失敗 error=%s: %s pid=%s",
            type(exc).__name__, exc, pid, exc_info=True,
        )
    # After the warmup attempt resolves, not at lifespan entry: "起動完了" has to
    # mean the server is about to serve, so tailing this line is a valid readiness
    # signal. Still before `yield`, so it precedes hypercorn/uvicorn accepting
    # connections and /api/health answering.
    # reader-sync heartbeat ticker (#78/S03): one task drives every
    # time-based transition (PING cadence, no-response close, code/resume
    # expiry). Started before yield so links can attach immediately.
    # getattr: tests drive lifespan with a bare SimpleNamespace state.
    _rs = getattr(app.state, "reader_sync", None)
    if _rs is not None:
        _rs.start()
    logger.info("起動完了 pid=%s", pid)
    # Shallow-warmup fill (plan shallow-warmup-fill T3): materialize opaque
    # archive nodes in the background, one at a time, pausing under request
    # load. Fire-and-forget past yield — never blocks serving or shutdown
    # semantics; all fill errors are swallowed per item inside the task.
    asyncio.create_task(_background_fill(app.state))
    # Startup cover pre-warming (plan thumb-warmup-stale-serve Todo 3):
    # warm every folder's single cover into the thumb disk cache right
    # away (no idle gating). Same fire-and-forget shape as the fill above;
    # all warm errors are swallowed per job inside the task.
    asyncio.create_task(_warm_covers(app.state))
    yield
    # reader-sync teardown (#78/S03): close every link/socket, cancel the
    # ticker and per-link drain tasks — zero reader-sync tasks or sessions
    # may survive app shutdown.
    _rs = getattr(app.state, "reader_sync", None)
    if _rs is not None:
        try:
            await _rs.aclose()
        except Exception as exc:
            logger.warning("reader-sync shutdown error=%s pid=%s", exc, pid)


def create_app() -> FastAPI:
    _configure_logging()
    settings = load_settings()

    app = FastAPI(title="bookq", version="2.0.0", lifespan=lifespan)
    app.state.settings = settings

    # SessionMiddleware required by authlib for OAuth state storage
    # Local OAuth over plain HTTP won't persist sessions (Secure cookie); use TEST_MODE or HTTPS
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        https_only=True,
        same_site="lax",
        max_age=86400 * 7,
    )

    # API JSON と JS/CSS だけ gzip。画像は素通し（app/compression.py 参照）。
    app.add_middleware(ApiGZipMiddleware, minimum_size=1024, compresslevel=6)

    init_oauth(settings)

    # Mount static files for logo and other assets
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", NoCacheStaticFiles(directory=static_dir), name="static")

    app.include_router(auth_router)
    app.include_router(token_router)
    app.include_router(api_router)
    app.include_router(folder_visibility_router)
    app.state.folder_visibility = FolderVisibilityStore(settings.state_path)

    # dual-reader sync endpoint (#78/S03, #80/S05). Session state lives only
    # on app.state (single worker); torn down by lifespan. work_resolver is
    # the S04 catalog bridge used by OPEN_WORK / SET_OFFSET.
    app.state.reader_sync = SessionRegistry(work_resolver=resolve_reader_work)
    app.add_api_websocket_route("/ws/reader-sync", reader_sync_ws)

    # Server-held "続きから" resume point: one latest record per user email,
    # in-memory only (a container restart loses it — accepted trade-off).
    app.state.reading_positions = {}

    @app.exception_handler(401)
    async def unauthorized_handler(request: Request, exc: Exception):
        return JSONResponse(
            status_code=401,
            content={"error": "Not authenticated"},
            headers={"Cache-Control": "private, no-store", "Vary": "Cookie"},
        )

    return app


app = create_app()
