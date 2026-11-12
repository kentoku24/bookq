"""Conformance vectors (#280) — Python side.

``api/vectors/*.json`` pins the domain rules shared by Python, JS and Swift:
work chapters (tree + opened folder -> chapters), path lookup (composed path
-> node + parent) and path encoding (segments -> percent-encoded URL).

Each vector file declares its ``languages``; which test file consumes it for
each language is pinned by tests/test_vectors_declaration.py.
"""
import json
import urllib.parse as up
from pathlib import Path

from app.reader_sync import catalog
from app.routes import _find_tree_node

REPO_ROOT = Path(__file__).resolve().parent.parent
VECTORS_DIR = REPO_ROOT / "api" / "vectors"

FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "reader_sync_cases.json"


def _load(name):
    return json.loads((VECTORS_DIR / name).read_text(encoding="utf-8"))


def _chapter_pages(tree, folder):
    """In-tree page names for one chapter folder.

    Mirrors catalog._chapter_pages' tree branches (leaf children, then mixed
    direct images). The filesystem fallback (archive/PDF/absent paths,
    ``_list_folder_images``) is out of scope for the vectors: such cases pin
    the selection (single chapter, empty pages) only.
    """
    node = _find_tree_node(tree, folder) if folder else None
    if node is not None and catalog._has_only_images(node):
        return [c["name"] for c in node["children"]]
    if node is not None and catalog._has_images(node):
        return [c["name"] for c in node["children"] if c.get("type") == "image"]
    return []


def _resolve_chapters(tree, folder):
    """Chapter selection mirroring catalog._build_manifest_core."""
    node = _find_tree_node(tree, folder) if folder else None
    is_mixed = (
        node is not None
        and catalog._has_images(node)
        and not catalog._has_only_images(node)
    )
    siblings = catalog._sibling_leaf_folders(tree, folder)
    if siblings is not None and len(siblings) > 1 and not is_mixed:
        chapter_folders = siblings
        in_tree_names = True
    else:
        chapter_folders = [folder]
        in_tree_names = False
    chapters = []
    for cf in chapter_folders:
        names = _chapter_pages(tree, cf)
        cnode = _find_tree_node(tree, cf) if cf else None
        if in_tree_names and cnode is not None:
            cname = cnode["name"]
        else:
            cname = cf.split("/")[-1] if cf else ""
        chapters.append({"name": cname, "folder": cf, "pages": names})
    return chapter_folders, chapters


def test_work_chapters_vectors():
    data = _load("work-chapters.json")
    assert data["format"] == 1
    assert "python" in data["languages"]
    for case in data["cases"]:
        folders, chapters = _resolve_chapters(case["tree"], case["folder"])
        cross = len(folders) > 1
        assert cross == case["expectCross"], case["name"]
        assert folders == case["expectChapterFolders"], case["name"]
        assert chapters == case["expectChapters"], case["name"]


def test_path_lookup_vectors():
    data = _load("path-lookup.json")
    assert data["format"] == 1
    assert "python" in data["languages"]
    for case in data["cases"]:
        tree, target = case["tree"], case["target"]
        node = _find_tree_node(tree, target) if target else None
        got_node = (
            None
            if node is None
            else {"name": node["name"], "type": node["type"]}
        )
        assert got_node == case["expectNode"], case["name"]
        found = catalog._find_container(tree, target) if target else None
        got_parent = None if found is None or not found[1] else found[1]
        assert got_parent == case["expectParent"], case["name"]


def _encode_segment(segment):
    """JS encodeURIComponent class: everything except A-Z a-z 0-9
    - _ . ! ~ * ' ( ) is percent-encoded (UTF-8, upper-hex)."""
    return up.quote(segment, safe="-_.!~*'()")


def _encode_path(path):
    return "/".join(_encode_segment(p) for p in path.split("/"))


def _image_url(folder, name):
    inner = folder + "/" + name if folder else name
    return "/image/" + _encode_path(inner)


def test_encode_path_vectors():
    data = _load("encode-path.json")
    assert data["format"] == 1
    assert "python" in data["languages"]
    for case in data["cases"]:
        assert _encode_path(case["path"]) == case["expect"], case["name"]
        if "folder" in case:
            inner = case["folder"] + "/" + case["file"] if case["folder"] else case["file"]
            assert _encode_path(inner) == case["expect"], case["name"]
            assert _image_url(case["folder"], case["file"]) == case["expectImageUrl"], case["name"]


def test_manifest_digest_vectors():
    """PairSyncTests' golden digest comes from this fixture (#280)."""
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    cases = fixture.get("manifestDigestCases", [])
    assert cases, "tests/fixtures/reader_sync_cases.json must carry manifestDigestCases"
    for case in cases:
        assert catalog.manifest_digest(case["manifest"]) == case["expectDigest"], case["name"]
