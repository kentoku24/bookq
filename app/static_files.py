"""StaticFiles with an explicit Cache-Control.

Starlette's StaticFiles sends ETag/Last-Modified but no Cache-Control, so
browsers fall back to heuristic freshness and may serve a stale app.js after a
deploy. Shell files have no content hash in their names, so a long max-age is
unsafe (the service worker's stale-while-revalidate fetch would hit the HTTP
cache too). `no-cache` = always revalidate with If-None-Match; a 304 costs a
few hundred bytes.
"""
from starlette.staticfiles import StaticFiles
from starlette.responses import Response


class NoCacheStaticFiles(StaticFiles):
    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response
