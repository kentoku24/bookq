import re
import urllib.request
import urllib.error
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SW_PATH = REPO_ROOT / "app" / "static" / "sw.js"

def _get(server: str, path: str):
    url = server + path
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read(), {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as e:
        return e.code, e.read(), {k.lower(): v for k, v in (e.headers or {}).items()}

def test_shell_precache_v2_assets_and_install(server=None):
    text = SW_PATH.read_text(encoding="utf-8")
    assert 'bookq-shell-v2' in text
    assert 'bookq-shell-v1' not in text, "stale v1 cache name must not linger"
    # Block match (re.S): the asset list may legally wrap across lines.
    m = re.search(r'SHELL_ASSETS\s*=\s*\[(.*?)\]', text, re.S)
    assert m, "SHELL_ASSETS block not found"
    block = m.group(1)
    for asset in ('"/static/style.css"', '"/static/app.js"', '"/static/offline-core.js"',
                  '"/static/icons/icon-192.png"', '"/static/icons/icon-512.png"',
                  '"/static/icons/icon-512-maskable.png"'):
        assert asset in block, f"SHELL_ASSETS missing {asset}"
    assert "new Request" in text and "cache:'reload'" in text or 'cache:\"reload\"' in text or "cache: 'reload'" in text
    assert "SHEL" in text

def test_shell_precache_v2_count():
    text = SW_PATH.read_text(encoding="utf-8")
    assert 'bookq-shell-v2' in text, "SHELL_CACHE must be v2"
    m = re.search(r'SHELL_ASSETS\s*=\s*\[(.*?)\]', text, re.S)
    assert m, "SHELL_ASSETS not found"
    entries = re.findall(r'"[^"]+"', m.group(1))
    # #77 added /static/viewer/navigation.js; #81 added viewer/pairModel.js +
    # viewer/pairedView.js to the shell precache (offline parity for pairing);
    # #84 adds pairTransport.js + pairClient.js + pairController.js;
    # #106 adds vendor/qrcode.min.js + vendor/jsQR.min.js (QR pairing);
    # browse/pairSync.js (dual-reader browse scroll mirror) joins the shell
    # precache for offline parity with the rest of the paired-reader chain.
    # progress/sync.js (server-held "続きから" resume point) is precached too —
    # the boot pull must exist in the same shell as app.js that calls it.
    # browse/visibility.js (per-user hidden roots) joins the shell — the
    # visibility module must exist in the same shell as the app that wires it.
    assert len(entries) == 33, f"expected 33 SHELL_ASSETS, got {len(entries)}: {entries}"
    assert '"/static/viewer/navigation.js"' in m.group(1), "SHELL_ASSETS must precache viewer/navigation.js"
    assert '"/static/viewer/pairController.js"' in m.group(1), "SHELL_ASSETS must precache viewer/pairController.js"
    assert '"/static/browse/visibility.js"' in m.group(1), "SHELL_ASSETS must precache browse/visibility.js"
    assert "new Request" in text and "reload" in text, "install must use new Request with cache:reload"

def test_static_swr_branch_order_and_guard():
    text = SW_PATH.read_text(encoding="utf-8")
    assert "staticSWR" in text or "StaleWhileRevalidate" in text or "caches.match" in text
    health_idx = text.find("/api/health")
    # static branch is in fetch handler: startsWith('/static/') && ['script','style'...
    fetch_section = text[text.find('self.addEventListener("fetch"'):]
    static_idx = fetch_section.find("startsWith('/static/')")
    # image branch in fetch handler is the second startsWith('/image/') after health
    # find image after static
    image_idx = fetch_section.find("startsWith('/image/')", static_idx + 1) if static_idx != -1 else -1
    if image_idx == -1:
        image_idx = fetch_section.find("startsWith('/image/')")
    api_idx = fetch_section.find("startsWith('/api/')", static_idx + 1) if static_idx != -1 else -1
    assert health_idx != -1
    assert static_idx != -1, "static SWR branch missing"
    assert image_idx != -1
    assert static_idx < image_idx, f"branch order wrong: static {static_idx} image {image_idx} in fetch handler"
    # global order health before static
    assert text.find("/api/health") < text.find("staticSWR") or text.find("/api/health") < fetch_section.find("startsWith('/static/')") + text.find('self.addEventListener("fetch"')
    assert "!url.pathname.startsWith('/static/')" in text, "image branch must guard !/static/"
    assert "['script','style','image','font','css']" in text or '["script"' in text or "request.destination" in text

def test_static_swr_semantics():
    text = SW_PATH.read_text(encoding="utf-8")
    assert "caches.match" in text
    assert "caches.open" in text
    assert "fetch(" in text
    assert ".put(" in text

def test_visibility_api_is_network_only():
    """/api/folder-visibility must be a dedicated network-only branch BEFORE
    the generic /api/ NetworkFirst branch — a per-user hidden set can never be
    served from the shared API cache, and offline failure is Response.error()
    (the module's owner-scoped localStorage snapshot is the fallback, not
    Cache API)."""
    text = SW_PATH.read_text(encoding="utf-8")
    fetch_section = text[text.find('self.addEventListener("fetch"'):]
    vis_idx = fetch_section.find("'/api/folder-visibility'")
    api_idx = fetch_section.find("startsWith('/api/')")
    assert vis_idx != -1, "dedicated /api/folder-visibility branch missing"
    assert api_idx != -1, "generic /api/ NetworkFirst branch missing"
    assert vis_idx < api_idx, (
        f"visibility branch must precede the shared API cache branch: "
        f"vis {vis_idx} api {api_idx}"
    )
    branch = fetch_section[vis_idx:api_idx]
    assert "fetch(request)" in branch, "visibility branch must go to network"
    assert ".put(" not in branch and "caches.open" not in branch, \
        "visibility responses must never enter a Cache API store"

def test_sw_headers(server):
    status, body, headers = _get(server, "/sw.js")
    assert status == 200
    assert headers.get("service-worker-allowed") == "/"
    cc = headers.get("cache-control", "")
    assert "no-cache" in cc.lower()
    status2, body2, headers2 = _get(server, "/static/sw.js")
    assert status2 == 200

def test_static_assets_all_200(server):
    for path in ["/static/style.css", "/static/app.js", "/static/offline-core.js", "/static/icons/icon-192.png", "/static/icons/icon-512.png", "/static/icons/icon-512-maskable.png"]:
        status, body, _ = _get(server, path)
        assert status == 200, f"{path} status {status}"
        assert len(body) > 0, f"{path} empty"


def test_dockerignore_excludes_no_app_static():
    text = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "app/static" not in text, ".dockerignore must not exclude app/static (offline-harness + sw.js must be in image)"


def test_dockerfile_copies_app():
    text = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY app/ ./app/" in text, "Dockerfile must COPY app/ ./app/ so app/static/offline-harness.html is included"
    assert text.find("COPY app/") == text.rfind("COPY app/") or text.count("COPY app/") >= 1


def test_install_best_effort_per_asset_does_not_reject():
    text = SW_PATH.read_text(encoding="utf-8")
    install_start = text.find('self.addEventListener("install"')
    assert install_start != -1, "install handler not found"
    activate_start = text.find('self.addEventListener("activate"', install_start + 1)
    install_block = text[install_start:activate_start] if activate_start != -1 else text[install_start:]
    assert "SHELL_ASSETS" in install_block, "install must reference SHELL_ASSETS"
    assert "reload" in install_block, "install must use cache:'reload'"
    assert "skipWaiting" in install_block, "install must still call skipWaiting"
    assert "cache.addAll(SHELL_ASSETS" not in install_block and "cache.addAll( SHELL_ASSETS" not in install_block, \
        "install must not use atomic cache.addAll(SHELL_ASSETS) — single asset failure must not reject install"
    assert not re.search(r"cache\.addAll\s*\(\s*SHELL_ASSETS", install_block), \
        "install must not use cache.addAll with SHELL_ASSETS"
    has_all_settled = "allSettled" in install_block
    has_all_with_catch = "Promise.all" in install_block and ".catch" in install_block
    has_loop_best_effort = ("for" in install_block and ".catch" in install_block)
    assert has_all_settled or has_all_with_catch or has_loop_best_effort, \
        "install must attempt each SHELL_ASSETS entry independently with best-effort handling (Promise.allSettled or Promise.all with per-asset .catch)"
    promise_pos = install_block.find("allSettled") if has_all_settled else install_block.find("Promise.all")
    if promise_pos == -1:
        promise_pos = install_block.find("for")
    skip_pos = install_block.find("skipWaiting")
    assert promise_pos != -1 and skip_pos != -1 and skip_pos > promise_pos, \
        "skipWaiting must be chained after per-asset attempts"
    assert has_all_settled or re.search(r"\.catch\s*\(\s*\(\s*\)\s*=>\s*\{\s*\}\s*\)", install_block) or re.search(r"\.catch\s*\(\s*\(\s*\)\s*=>", install_block) or "catch" in install_block, \
        "install best-effort handling must swallow per-asset rejections"


def test_conftest_flatten_chain_only_leaf(tmp_path):
    import struct, zlib, sys
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from app.scanner import scan_tree

    def _png(p):
        def chunk(tag, data):
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        ihdr = struct.pack(">IIBBBBB", 40, 60, 8, 2, 0, 0, 0)
        row = b"\x00" + bytes((200, 60, 60)) * 40
        idat = zlib.compress(row * 60)
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b""))

    leaf = tmp_path / "chain" / "only" / "leaf"
    leaf.mkdir(parents=True)
    _png(leaf / "1.png")
    tree = scan_tree(tmp_path)
    assert len(tree) == 1
    assert tree[0]["name"] == "chain/only/leaf/1.png"
    assert tree[0]["type"] == "image"


def test_uiux_tree_fixture_sync():
    ft = (REPO_ROOT / "tests" / "fixtures" / "uiux_tree.py").read_text(encoding="utf-8")
    assert "chain" in ft and "only" in ft and "leaf" in ft, "uiux_tree.py must build chain/only/leaf flatten fixture"
    assert "thumb_grid" in ft or "THUMB_GRID" in ft
    conftest_text = (REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert "chain/only/leaf" in conftest_text, "conftest downloads_tree must build chain/only/leaf"
    assert "wrap/inner" in conftest_text
    assert "odd_wrap/inner" in conftest_text
