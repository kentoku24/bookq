"""Path-scoped gzip.

Starlette's GZipMiddleware compresses every response regardless of type; that
would waste CPU on JPEG/PNG under /image and /thumb (already compressed) and
interfere with Range (206) responses there. So compression is opted in by path:
API JSON (3.8 MB trees shrink ~10x) and the JS/CSS shell. Everything else is
passed through untouched.

Relies on Starlette >= 1.6 (requirements.txt): large bodies are gzipped on a
worker thread and image/* responses are excluded by content type.
"""
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import Receive, Scope, Send

COMPRESS_PREFIXES = ("/api/", "/static/", "/sw.js")


class ApiGZipMiddleware(GZipMiddleware):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path", "").startswith(COMPRESS_PREFIXES):
            await super().__call__(scope, receive, send)
            return
        await self.app(scope, receive, send)
