"""Todo 8 — Pure unit tests for OfflineCore helpers with deps injection.

Covers fmt, cacheNameFor, dedupedBytes/rawBytes, getLeafPathsFor without Playwright or SW.

Refs: app/static/offline-core.js (single source), app/static/app.js deps injection.
"""
import urllib.parse as up
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ── Mirrors of offline-core.js pure helpers ───────────────────────────────

def fmt(bytes_val):
    """Mirror OfflineCore.fmt: <1e9 -> MB, else GB, 1 decimal."""
    try:
        n = float(bytes_val)
    except Exception:
        n = 0
    if n != n:  # NaN
        n = 0
    if n < 1e9:
        return f"{n / 1e6:.1f} MB"
    return f"{n / 1e9:.1f} GB"


def cacheNameFor(folder: str) -> str:
    """Mirror OfflineCore.cacheNameFor: 'bookq-work-'+encodeURIComponent(folder)+'-v1'"""
    return "bookq-work-" + up.quote(folder, safe="") + "-v1"


def dedupedBytes(all_records):
    folder_set = {w["folder"] for w in all_records}
    s = 0
    for w in all_records:
        is_parent = any(f != w["folder"] and f.startswith(w["folder"] + "/") for f in folder_set)
        if not is_parent:
            s += w.get("bytes") or 0
    return s


def rawBytes(all_records):
    return sum((w.get("bytes") or 0) for w in all_records)


def _folder_has_only_images(node):
    children = node.get("children") or []
    return bool(children) and all(c.get("type") == "image" for c in children)


def collect_leaf_paths(node, out):
    if _folder_has_only_images(node):
        out.append(node["_fullPath"])
        return
    for c in (node.get("children") or []):
        if c.get("type") == "folder":
            collect_leaf_paths(c, out)


def _find_node(nodes, target):
    for n in nodes:
        if n.get("_fullPath") == target:
            return n
        if n.get("children") and target.startswith(n["_fullPath"] + "/"):
            f = _find_node(n["children"], target)
            if f:
                return f
    return None


def compute_leaf_paths_for(folder, tree, get_node_at=None):
    """Mirror app.js computeLeafPathsFor with optional deps injection."""
    get = get_node_at or (lambda p: _find_node(tree, p))
    node = get(folder)
    if node:
        if _folder_has_only_images(node):
            return [folder]
        tmp = []
        for c in (node.get("children") or []):
            if c.get("type") == "folder":
                collect_leaf_paths(c, tmp)
        has_direct_images = any(c.get("type") == "image" for c in (node.get("children") or []))
        if not tmp:
            return [folder]
        dedup = list(dict.fromkeys(tmp))
        if has_direct_images:
            dedup.insert(0, folder)
        return dedup
    # fallback walk for flattened parents
    leaves = []

    def walk(nodes):
        for n in nodes:
            if n.get("_fullPath") == folder or n.get("_fullPath", "").startswith(folder + "/"):
                if _folder_has_only_images(n):
                    leaves.append(n["_fullPath"])
            if n.get("children"):
                walk(n["children"])

    walk(tree)
    if leaves:
        return list(dict.fromkeys(leaves))
    return [folder]


def get_leaf_paths_for(folder, tree, deps=None):
    """Mirror getLeafPathsFor with deps={tree, getNodeAt} injection."""
    if deps and "tree" in deps:
        tree = deps["tree"]
    get_node = None
    if deps and "getNodeAt" in deps:
        get_node = deps["getNodeAt"]
    return compute_leaf_paths_for(folder, tree, get_node_at=get_node)


# ── Tests ─────────────────────────────────────────────────────────────────

def test_fmt():
    assert fmt(50_000_000) == "50.0 MB"
    assert fmt(1_500_000_000) == "1.5 GB"
    assert fmt(1_000_000_000) == "1.0 GB"
    assert fmt(999_999_999) == "1000.0 MB"  # <1e9 -> MB
    assert fmt(0) == "0.0 MB"
    assert fmt(None) == "0.0 MB"
    # boundary
    assert fmt(1_000_000_000 - 1) == "1000.0 MB"
    assert fmt(1_000_000_000) == "1.0 GB"


def test_cacheNameFor():
    assert cacheNameFor("a/b") == "bookq-work-a%2Fb-v1"
    assert cacheNameFor("夏 の 本") == "bookq-work-" + up.quote("夏 の 本", safe="") + "-v1"
    assert cacheNameFor("") == "bookq-work--v1"
    # slash must be %2F (encodeURIComponent)
    assert "%2F" in cacheNameFor("x/y")
    # roundtrip via decode should recover
    assert up.unquote(cacheNameFor("a/b").removeprefix("bookq-work-").removesuffix("-v1")) == "a/b"


def test_deduped_and_raw_bytes():
    records = [
        {"folder": "a", "bytes": 100},
        {"folder": "a/b", "bytes": 60},
        {"folder": "c", "bytes": 30},
    ]
    # a is parent of a/b -> dedup skips a
    assert rawBytes(records) == 190
    assert dedupedBytes(records) == 90  # 60+30
    # no parent
    records2 = [{"folder": "x", "bytes": 10}, {"folder": "y", "bytes": 20}]
    assert dedupedBytes(records2) == 30
    assert rawBytes(records2) == 30
    # empty
    assert dedupedBytes([]) == 0
    assert rawBytes([]) == 0


def test_getLeafPathsFor_with_mocked_tree():
    tree = [
        {
            "_fullPath": "series",
            "name": "series",
            "type": "folder",
            "children": [
                {"_fullPath": "series/vol1", "name": "vol1", "type": "folder", "children": [{"name": "a.png", "type": "image"}]},
                {"_fullPath": "series/vol2", "name": "vol2", "type": "folder", "children": [{"name": "b.png", "type": "image"}]},
            ],
        },
        {"_fullPath": "single", "name": "single", "type": "folder", "children": [{"name": "c.png", "type": "image"}]},
    ]
    # series -> leaves under it
    assert get_leaf_paths_for("series", tree) == ["series/vol1", "series/vol2"]
    # leaf folder itself
    assert get_leaf_paths_for("series/vol1", tree) == ["series/vol1"]
    assert get_leaf_paths_for("single", tree) == ["single"]
    # unknown -> fallback returns itself
    assert get_leaf_paths_for("nope", tree) == ["nope"]
    # with injected tree override via deps
    alt_tree = [
        {"_fullPath": "alt", "name": "alt", "type": "folder", "children": [{"name": "x.png", "type": "image"}]}
    ]
    assert get_leaf_paths_for("alt", tree, deps={"tree": alt_tree}) == ["alt"]
    # with mocked getNodeAt that handles flattened case
    def mock_get(p):
        if p == "group/episodes":
            return {"_fullPath": "group/episodes", "name": "episodes", "type": "folder", "children": [
                {"_fullPath": "group/episodes/ep1", "name": "ep1", "type": "folder", "children": [{"name": "p.png", "type": "image"}]},
                {"_fullPath": "group/episodes/ep2", "name": "ep2", "type": "folder", "children": [{"name": "q.png", "type": "image"}]},
            ]}
        return _find_node(tree, p)
    assert get_leaf_paths_for("group/episodes", tree, deps={"getNodeAt": mock_get}) == ["group/episodes/ep1", "group/episodes/ep2"]


def test_feature_test_replaces_ua_sniff():
    """Ensure storage feature test is used, not UA string."""
    app_text = (REPO_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "isSafari =" not in app_text and "isSafari=" not in app_text
    assert "navigator.userAgent" not in app_text
    # quota fallback still present: 0/0 check
    assert "est.usage===0 && est.quota===0" in app_text or "est.usage === 0 && est.quota === 0" in app_text
    # window exposures kept for Playwright
    assert "window.downloadWork" in app_text
    assert "window.updateStorageBar" in app_text


def test_offline_core_single_source():
    """OfflineCore pure helpers still single-source and exported."""
    core_text = (REPO_ROOT / "app" / "static" / "offline-core.js").read_text(encoding="utf-8")
    assert "function fmt(bytes)" in core_text
    assert "function cacheNameFor(folder)" in core_text
    assert "function dedupedBytes(all)" in core_text
    assert "function rawBytes(all)" in core_text
    # escapeHtml / folderHasOnlyImages moved here from app.js + 4 copies (#107)
    assert "function escapeHtml(s)" in core_text
    assert "function folderHasOnlyImages(node)" in core_text
    # published on globalThis, so sw.js can importScripts the same file
    assert "globalThis.OfflineCore" in core_text
    # pin escaping semantics, not just existence: & first, all five chars
    amp_i = core_text.index('replace(/&/g, "&amp;")')
    lt_i = core_text.index('replace(/</g, "&lt;")')
    gt_i = core_text.index('replace(/>/g, "&gt;")')
    quot_i = core_text.index('replace(/"/g, "&quot;")')
    apos_i = core_text.index("replace(/'/g, \"&#39;\")")
    assert amp_i < lt_i < gt_i < quot_i < apos_i
    # ESM shim removed (#110, kept): no .mjs re-export exists anymore.
    assert not (REPO_ROOT / "app" / "static" / "offline-core.mjs").exists()

    # sw.js shares cacheNameFor instead of redefining it
    sw_text = (REPO_ROOT / "app" / "static" / "sw.js").read_text(encoding="utf-8")
    # sw.js importScripts both files in one call (#236: offline-core.js for
    # cacheNameFor + api/endpoints.js for API_PATHS) — prefix match accepts
    # the multi-arg form (see test_sw_offline_contract.py verbatim assert).
    assert "importScripts('/static/offline-core.js'" in sw_text
    assert "'/static/api/endpoints.js'" in sw_text
    assert "self.OfflineCore" in sw_text
    assert "function cacheNameFor" not in sw_text, "sw.js must not keep its own cacheNameFor"
    assert '"/static/offline-core.js"' in sw_text, "offline-core.js must stay precached for offline importScripts"

    # no second copy of either helper anywhere in the frontend
    for rel in ("app/static/app.js", "app/static/browse/render.js",
                "app/static/search/facets.js", "app/static/offline/storeAdapter.js",
                "app/static/offline-harness.html"):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "function escapeHtml" not in text, f"{rel} must not define escapeHtml"
        assert "function folderHasOnlyImages" not in text, f"{rel} must not define folderHasOnlyImages"
        assert "escapeHtml(" in text, f"{rel} must call the shared escapeHtml"


def test_app_js_has_no_module_fallback_bodies():
    """app.js delegates to the modules; no `window.X && window.X.fn` fallbacks.

    A missing module must throw at startup (wrong defer order in index.html)
    instead of silently running a second, drifting implementation. Refs #107.
    """
    app_text = (REPO_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    import re
    hits = re.findall(r"window\.[A-Za-z]+ && window\.", app_text)
    assert hits == [], f"module fallback bodies are back in app.js: {hits}"
