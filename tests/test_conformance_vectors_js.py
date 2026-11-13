"""Conformance vectors (#280) — JS side (via Playwright + production sources).

Reads api/vectors/*.json and drives the real frontend code — never a
re-implementation:

* encode-path: the shipped app/static/offline-core.js (OfflineCore).
* solo-spreads: the shipped app/static/viewer/spread.js (rebuildSpreads).
* work-chapters / path-lookup: findNodeByPath, findParentPath/getParentPath,
  findParentOf and getSiblingLeafFolders extracted verbatim from the shipped
  app/static/app.js (brace-matched, so a source drift breaks the extraction
  instead of silently testing a copy), run against the vector trees.
  This stands in for the catalog/rules.js extraction (#271): once the pure
  functions move, this harness points at the module instead.
"""
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
VECTORS_DIR = REPO_ROOT / "api" / "vectors"
APP_JS = REPO_ROOT / "app" / "static" / "app.js"
OFFLINE_CORE_JS = REPO_ROOT / "app" / "static" / "offline-core.js"
SPREAD_JS = REPO_ROOT / "app" / "static" / "viewer" / "spread.js"


def _load(name):
    return json.loads((VECTORS_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture
def any_browser(request):
    if os.environ.get("BROWSER") == "webkit":
        return request.getfixturevalue("webkit_browser")
    return request.getfixturevalue("browser")


@contextmanager
def _page(any_browser):
    context = any_browser.new_context()
    pg = context.new_page()
    try:
        pg.goto("about:blank")
        yield pg
    finally:
        context.close()


def test_encode_path_js(any_browser):
    """Evaluate the shipped OfflineCore against the encode-path vectors."""
    data = _load("encode-path.json")
    assert "js" in data["languages"]
    with _page(any_browser) as pg:
        pg.add_script_tag(path=str(OFFLINE_CORE_JS))
        for case in data["cases"]:
            expr = (
                "() => ({path: window.OfflineCore.encodePath("
                + json.dumps(case["path"])
                + ")"
                + (
                    ", url: window.OfflineCore.imageUrl("
                    + json.dumps(case["folder"])
                    + ", "
                    + json.dumps(case["file"])
                    + ")"
                    if "folder" in case
                    else ""
                )
                + "})"
            )
            got = pg.evaluate(expr)
            assert got["path"] == case["expect"], case["name"]
            if "folder" in case:
                assert got["url"] == case["expectImageUrl"], case["name"]


def test_solo_spreads_js(any_browser):
    """Evaluate the shipped spread.js rebuildSpreads against the vectors."""
    data = _load("solo-spreads.json")
    assert "js" in data["languages"]
    with _page(any_browser) as pg:
        pg.add_script_tag(path=str(SPREAD_JS))
        for case in data["cases"]:
            got = pg.evaluate(
                """([images, boundaries, mode, offset]) => {
                     window.images = images;
                     window.chapterBoundaries = boundaries;
                     window.pageMode = mode;
                     window.spreadOffset = offset;
                     window.anchorSpread = 0;
                     window.spreadSizeCache = {};
                     window.rebuildSpreads();
                     return {
                       spreads: window.spreads.map(s => ({pages: s.pages, chapterStart: !!s.chapterStart})),
                       // Spacer slots stay holes (undefined) in JS while Swift
                       // uses -1: normalize to the vector's -1 convention.
                       idxToSpread: Array.from(window.idxToSpread, v => (v === undefined ? -1 : v)),
                     };
                   }""",
                [case["images"], case["chapterBoundaries"], case["pageMode"], case["spreadOffset"]],
            )
            assert [s["pages"] for s in got["spreads"]] == case["expectSpreads"], case["name"]
            assert [s["chapterStart"] for s in got["spreads"]] == case["expectChapterStart"], case["name"]
            assert list(got["idxToSpread"]) == case["expectIdxToSpread"], case["name"]


def test_chapter_padding_js(any_browser):
    """padChapterSpacers + rebuildSpreads against chapter-padding vectors.

    The chapter->flat-images/boundaries mapping is glue (same shape
    loadImages builds); the padding rule and spread grouping under test are
    the shipped spread.js functions.
    """
    data = _load("chapter-padding.json")
    assert "js" in data["languages"]
    with _page(any_browser) as pg:
        pg.add_script_tag(path=str(SPREAD_JS))
        for case in data["cases"]:
            got = pg.evaluate(
                """([chapters, mode, offset]) => {
                     var images = [], boundaries = [];
                     for (var ch of chapters) {
                       boundaries.push(images.length);
                       for (var p of ch.pages) images.push({folder: ch.folder, name: p});
                     }
                     window.padChapterSpacers(images, boundaries, mode, offset);
                     window.images = images;
                     window.chapterBoundaries = boundaries;
                     window.pageMode = mode;
                     window.spreadOffset = offset;
                     window.anchorSpread = 0;
                     window.spreadSizeCache = {};
                     window.rebuildSpreads();
                     return {
                       images: images.map(im => [im.folder, im.name, !!im.isSpacer]),
                       boundaries: boundaries,
                       spreads: window.spreads.map(s => s.pages),
                       chapterStart: window.spreads.map(s => !!s.chapterStart),
                       idxToSpread: Array.from(window.idxToSpread, v => (v === undefined ? -1 : v)),
                     };
                   }""",
                [case["chapters"], case["pageMode"], case["spreadOffset"]],
            )
            expect_images = [
                [e["folder"], e["name"], bool(e.get("isSpacer"))]
                for e in case["expectImages"]
            ]
            assert got["images"] == expect_images, case["name"]
            assert got["boundaries"] == case["expectBoundaries"], case["name"]
            assert got["spreads"] == case["expectSpreads"], case["name"]
            assert got["chapterStart"] == case["expectChapterStart"], case["name"]
            assert list(got["idxToSpread"]) == case["expectIdxToSpread"], case["name"]


def _extract_function(src, name):
    """Extract `function name(...) {...}` verbatim by brace matching.

    String/comment aware so braces inside literals cannot end the scan.
    Raises on any shape change — a drift that breaks extraction fails the
    test instead of silently testing a stale copy.
    """
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\(", src)
    assert m, f"function {name} not found in app.js (moved to catalog/rules.js? update this harness)"
    i = src.index("{", m.end())
    depth = 0
    in_str = None
    in_line = False
    in_block = False
    j = i
    n = len(src)
    while j < n:
        c = src[j]
        nxt = src[j + 1] if j + 1 < n else ""
        if in_line:
            if c == "\n":
                in_line = False
        elif in_block:
            if c == "*" and nxt == "/":
                in_block = False
                j += 1
        elif in_str:
            if c == "\\":
                j += 1
            elif c == in_str:
                in_str = None
        else:
            if c == "/" and nxt == "/":
                in_line = True
                j += 1
            elif c == "/" and nxt == "*":
                in_block = True
                j += 1
            elif c in ("'", '"', "`"):
                in_str = c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return src[m.start():j + 1]
        j += 1
    raise AssertionError(f"unbalanced braces extracting {name}")


def _with_full_paths(nodes, parent=""):
    """Compose _fullPath like app.js setFullPathsAndIndex (parent/name)."""
    out = []
    for node in nodes:
        full = parent + "/" + node["name"] if parent else node["name"]
        entry = {"name": node["name"], "type": node["type"], "_fullPath": full}
        if node.get("children"):
            entry["children"] = _with_full_paths(node["children"], full)
        out.append(entry)
    return out


def _app_js_harness():
    """Production tree-walk sources + depth-guard pin + thin assembly glue.

    The extracted functions close over the page-level `tree` (as in app.js);
    per-case setup installs the vector tree there. Only assembleWork's
    mapping (siblings -> chapters) is glue — the depth guard, parent walk,
    leaf filter and node lookup are production code.
    """
    src = APP_JS.read_text(encoding="utf-8")
    m = re.search(r"const\s+SIBLING_AUTOLOAD_MIN_DEPTH\s*=\s*(\d+)", src)
    assert m and m.group(1) == "2", "SIBLING_AUTOLOAD_MIN_DEPTH must stay 2"
    parts = [
        "var tree = null;",
        "const folderHasOnlyImages = window.OfflineCore.folderHasOnlyImages;",
        "const folderHasImages = window.OfflineCore.folderHasImages;",
        "const SIBLING_AUTOLOAD_MIN_DEPTH = 2;",
    ]
    for name in (
        "findParentOf",
        "findNodeByPath",
        "findParentPath",
        "getParentPath",
        "getSiblingLeafFolders",
    ):
        parts.append(_extract_function(src, name))
    parts.append(
        """function assembleWork(folder) {
             const siblings = getSiblingLeafFolders(folder);
             const node = findNodeByPath(tree, folder);
             const isSelfMixed = !!(node && folderHasImages(node) && !folderHasOnlyImages(node));
             const cross = siblings.length > 1 && !isSelfMixed;
             if (cross) {
               return {cross: cross,
                       folders: siblings.map(s => s.fullPath),
                       chapters: siblings.map(s => {
                         const n = findNodeByPath(tree, s.fullPath);
                         const pages = (n && folderHasOnlyImages(n)) ? n.children.map(c => c.name) : [];
                         return {name: s.name, folder: s.fullPath, pages: pages};
                       })};
             }
             let pages = [];
             if (node && folderHasOnlyImages(node)) pages = node.children.map(c => c.name);
             else if (node && folderHasImages(node) && !folderHasOnlyImages(node))
               pages = node.children.filter(c => c.type === 'image').map(c => c.name);
             return {cross: cross,
                     folders: [folder],
                     chapters: [{name: folder.split('/').pop(), folder: folder, pages: pages}]};
           }"""
    )
    return "\n".join(parts)


def test_work_chapters_js(any_browser):
    """Production getSiblingLeafFolders assembly against work-chapters."""
    data = _load("work-chapters.json")
    assert "js" in data["languages"]
    harness = _app_js_harness()
    with _page(any_browser) as pg:
        pg.add_script_tag(path=str(OFFLINE_CORE_JS))
        pg.evaluate(harness)
        for case in data["cases"]:
            got = pg.evaluate(
                "([t, folder]) => { tree = t; return assembleWork(folder); }",
                [_with_full_paths(case["tree"]), case["folder"]],
            )
            assert got["cross"] == case["expectCross"], case["name"]
            assert got["folders"] == case["expectChapterFolders"], case["name"]
            assert got["chapters"] == case["expectChapters"], case["name"]


def test_path_lookup_js(any_browser):
    """Production findNodeByPath/getParentPath against path-lookup."""
    data = _load("path-lookup.json")
    assert "js" in data["languages"]
    harness = _app_js_harness()
    with _page(any_browser) as pg:
        pg.add_script_tag(path=str(OFFLINE_CORE_JS))
        pg.evaluate(harness)
        for case in data["cases"]:
            got = pg.evaluate(
                """([t, target]) => {
                     tree = t;
                     const n = target ? findNodeByPath(tree, target) : null;
                     return {node: n ? {name: n.name, type: n.type} : null,
                             parent: getParentPath(target)};
                   }""",
                [_with_full_paths(case["tree"]), case["target"]],
            )
            assert got["node"] == case["expectNode"], case["name"]
            assert got["parent"] == case["expectParent"], case["name"]
