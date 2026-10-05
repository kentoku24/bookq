"""gzip はテキスト系（/api, /static, /sw.js）だけ。画像は絶対に圧縮しない。"""
import gzip
import json

import httpx


def _get(server: str, path: str, accept_encoding: str | None = "gzip"):
    headers = {}
    if accept_encoding is not None:
        headers["Accept-Encoding"] = accept_encoding
    # httpx は既定で自動デコードするので raw を見るため stream を使う
    with httpx.Client(base_url=server, timeout=10) as c:
        with c.stream("GET", path, headers=headers) as r:
            raw = b"".join(r.iter_raw())
            return r, raw


def test_api_folders_is_gzipped_when_accepted(server):
    r, raw = _get(server, "/api/folders")
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"
    body = json.loads(gzip.decompress(raw))
    assert isinstance(body, list) and len(body) > 0
    # 圧縮が効いている（少なくとも 3 倍）
    assert len(raw) * 3 < len(json.dumps(body).encode("utf-8"))


def test_api_folders_identity_without_accept_encoding(server):
    r, raw = _get(server, "/api/folders", accept_encoding="identity")
    assert r.status_code == 200
    assert "content-encoding" not in r.headers
    assert isinstance(json.loads(raw), list)


def test_api_folders_etag_survives_gzip(server):
    r1, _ = _get(server, "/api/folders")
    etag = r1.headers.get("etag")
    assert etag
    with httpx.Client(base_url=server, timeout=10) as c:
        r2 = c.get("/api/folders", headers={"Accept-Encoding": "gzip", "If-None-Match": etag})
    assert r2.status_code == 304


def test_static_js_is_gzipped(server):
    r, raw = _get(server, "/static/app.js")
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"


def test_images_are_never_gzipped(server):
    # conftest の fixture ツリーに simple_manga/1.png がある
    r, raw = _get(server, "/image/simple_manga/1.png")
    assert r.status_code == 200
    assert "content-encoding" not in r.headers
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    r2, _ = _get(server, "/thumb/simple_manga/1.png?w=200")
    assert r2.status_code == 200
    assert "content-encoding" not in r2.headers
