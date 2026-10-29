"""Regression contract for clearing authenticated API cache across sessions."""

from pathlib import Path


SW_PATH = Path(__file__).resolve().parent.parent / "app" / "static" / "sw.js"


def test_api_cache_version_bump_drops_legacy_cache():
    text = SW_PATH.read_text(encoding="utf-8")

    assert 'const API_CACHE = "bookq-api-v2"' in text
    assert 'const API_CACHE = "bookq-api-v1"' not in text
    assert "const whitelist = [SHELL_CACHE, API_CACHE, FALLBACK_CACHE]" in text


def test_logout_clears_api_cache_before_and_after_network_request():
    text = SW_PATH.read_text(encoding="utf-8")
    start = text.find("if (url.pathname === '/auth/logout')")
    end = text.find("// Online-check probe", start)
    assert start != -1 and end != -1, "logout must have a dedicated fetch branch"
    block = text[start:end]

    assert block.count("await clearApiCache()") >= 3
    assert "await fetch(request)" in block
    assert "return Response.error()" in block


def test_unauthorized_api_response_clears_private_cache():
    text = SW_PATH.read_text(encoding="utf-8")
    assert "res.status === 401 || res.status === 403" in text
    assert "await clearApiCache()" in text
    assert "let apiCacheEpoch = 0" in text
    assert "cacheEpoch !== apiCacheEpoch" in text
