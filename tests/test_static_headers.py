"""静的アセットは ETag 再検証前提の no-cache。ヒューリスティックキャッシュで
古い app.js が残ることを防ぐ（ハッシュ付きファイル名を持たない現行構成での正解）。"""
import httpx

SHELL_FILES = [
    "/static/app.js",
    "/static/style.css",
    "/static/offline-core.js",
    "/static/viewer/spread.js",
    "/static/manifest.json",
]


def test_static_assets_send_no_cache_and_etag(server):
    with httpx.Client(base_url=server, timeout=10) as c:
        for path in SHELL_FILES:
            r = c.get(path)
            assert r.status_code == 200, path
            assert r.headers.get("cache-control") == "no-cache", (path, dict(r.headers))
            assert r.headers.get("etag"), path


def test_static_asset_conditional_304(server):
    with httpx.Client(base_url=server, timeout=10) as c:
        r1 = c.get("/static/app.js")
        r2 = c.get("/static/app.js", headers={"If-None-Match": r1.headers["etag"]})
    assert r2.status_code == 304
    assert r2.headers.get("cache-control") == "no-cache"


def test_sw_js_keeps_no_cache_and_images_keep_public_max_age(server):
    """既存の方針が崩れていないこと（回帰防止）。"""
    with httpx.Client(base_url=server, timeout=10) as c:
        sw = c.get("/sw.js")
        img = c.get("/image/simple_manga/1.png")
        thumb = c.get("/thumb/simple_manga/1.png?w=200")
    assert sw.headers.get("cache-control") == "no-cache"
    assert img.headers.get("cache-control") == "public, max-age=3600"
    assert thumb.headers.get("cache-control", "").startswith("public, max-age=86400")
