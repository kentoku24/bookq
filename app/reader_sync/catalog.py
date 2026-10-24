"""dual-reader shared work manifest — S04 (#79).

``resolve_work(connection, openedFolder, initialPageRef, offset)`` builds the
``{chapters, pageRefs, spreadTable, digest}`` manifest both devices share.
All enumeration is the SAME code path the solo viewer uses — the scan_tree
tree behind ``app.state.metadata_index_cache`` (``app.routes._ensure_tree_and_index``)
and the shared ``/api/images`` core (``app.routes._list_folder_images``).
There is no second enumeration implementation here.

Chapter selection mirrors ``app.js`` ``getSiblingLeafFolders`` /
``loadImages`` byte-for-byte:

- raw depth (``len(path.split("/")) - 1``) below ``SIBLING_AUTOLOAD_MIN_DEPTH``
  (2) → the opened folder is its own single chapter.
- otherwise the STRUCTURAL parent is found by walking composed
  ``parent/name`` paths (flattened node names like ``"inner/vol1"`` contain
  "/" and are never split) — the parent's children filtered to
  folder nodes whose children are all images become the ordered chapters.
- a mixed opened folder (loose images + subfolders) reads its direct image
  children only and never joins the sibling run.
- a non-leaf or not-in-tree folder falls back to the shared enumeration
  (archive file → flat entry list, archive inner prefix → scoped list,
  PDF → synthetic ``pNNN.png`` pages, plain dir → direct images).

``PageRef = {folder, name}`` — *folder* is the downloads-relative serving
path (it may carry "/" via flattening and archive-inner prefixes), *name*
the folder-relative entry name verbatim (it may itself carry "/" for
archive inner names). Together ``/image/<folder>/<name>`` serves the page;
manifest entries never carry image bytes.

Determinism: ``digest`` is ``"sha256:" + hex`` over the canonical JSON of
the content fields (``v/folder/offset/chapters/pageRefs/spreadTable``), so
two devices (or two library roots with identical content) verify identical
manifests. ``split_manifest``/``join_manifest`` chunk and reassemble that
canonical byte stream — reassembly re-verifies the digest.

Error kinds are typed and distinct via :class:`ManifestError` ``.code`` —
there is NEVER a silent fallback to another page: the returned
``initial.page`` is always the input ``initialPageRef`` or the call raises.
"""
from __future__ import annotations

import copy
import hashlib
import json
import zipfile
from pathlib import Path

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from app.archive import RAR_EXTENSIONS, is_archive_file
from app.reader_sync import model, protocol
from app.reader_sync.model import PageRef
from app.routes import (
    _ensure_tree_and_index,
    _find_tree_node,
    _is_within,
    _list_folder_images,
    _resolve_archive,
)
from app.scanner import PDF_EXTENSIONS, is_pdf_file

# ── error codes (typed, distinguishable — never conflated) ───────────────

ERR_MALFORMED = "ERR_MALFORMED"            # bad argument shapes
ERR_RANGE = "ERR_RANGE"                    # offset outside {0,1}
ERR_SIZE = "ERR_SIZE"                      # manifest/chunk over its cap
ERR_NO_INITIAL_PAGE = "ERR_NO_INITIAL_PAGE"  # initialPageRef absent
ERR_PAGE_NOT_FOUND = "ERR_PAGE_NOT_FOUND"    # initialPageRef not in the work
ERR_EMPTY_WORK = "ERR_EMPTY_WORK"          # resolved work has zero pages
ERR_FORBIDDEN = "ERR_FORBIDDEN"            # outside downloads / unreadable
ERR_NOT_FOUND = "ERR_NOT_FOUND"            # folder (or inner prefix) absent
ERR_BROKEN_ARCHIVE = "ERR_BROKEN_ARCHIVE"    # container unreadable as its type


class ManifestError(model.ModelError):
    """Typed manifest-resolution failure; ``.code`` is one of the ERR_*
    constants above (inherits ModelError's ``code``/``str`` contract)."""


# Sibling chapter autoload floor — the same constant as app.js
# SIBLING_AUTOLOAD_MIN_DEPTH: only a folder at raw depth >= 2 (two or more
# path separators, i.e. its containing folder is at least a series-level
# directory) concatenates leaf siblings.
_SIBLING_MIN_DEPTH = 2

# Chunk budget: every chunk dict must still fit a normal 64KiB wire frame
# once its envelope (v/kind/digest/index/count keys) is added. The "data"
# field re-encodes the canonical JSON text, so every '"' or '\' inside the
# slice re-escapes to two chars — the worst-case frame is 2*data + ~150B of
# envelope. 30KiB keeps even an all-quotes slice under the cap.
MANIFEST_CHUNK_BYTES = 30 * 1024
MAX_MANIFEST_BYTES = 32 * 1024 * 1024

_MANIFEST_KIND = "manifest-chunk"

# Digest covers work content only — the per-call ``initial`` anchor and the
# digest field itself are excluded, so two devices at different positions
# still verify identical manifests.
_CONTENT_KEYS = ("v", "folder", "offset", "chapters", "pageRefs", "spreadTable")


# ── tree predicates (ports of OfflineCore folderHasOnlyImages/HasImages) ──


def _has_only_images(node: dict | None) -> bool:
    children = node.get("children") if isinstance(node, dict) else None
    return bool(children) and all(c.get("type") == "image" for c in children)


def _has_images(node: dict | None) -> bool:
    children = node.get("children") if isinstance(node, dict) else None
    return bool(children) and any(c.get("type") == "image" for c in children)


def _compose(parent_path: str, name: str) -> str:
    return f"{parent_path}/{name}" if parent_path else name


def _norm_path(path: str) -> str:
    """Collapse empty and "." segments — one canonical spelling per folder."""
    return "/".join(p for p in path.split("/") if p not in ("", "."))


def _find_container(nodes: list[dict], target: str, path: str = ""):
    """Return ``(siblings_list, parent_path)`` whose list holds *target*.

    Structural parent lookup mirroring ``app.js`` ``findParentOf`` /
    ``app.routes._find_tree_node``: names are never split on "/" (a
    flattened name already contains it) — the full path is always the
    composed ``parent/name``. ``parent_path`` is ``""`` for a root-level
    match (meaning "no real parent"). ``None`` when *target* is absent.
    """
    for node in nodes:
        name = node.get("name")
        if not isinstance(name, str):
            continue
        full = _compose(path, name)
        if full == target:
            return nodes, path
        children = node.get("children")
        if children and target.startswith(full + "/"):
            hit = _find_container(children, target, full)
            if hit is not None:
                return hit
    return None


def _sibling_leaf_folders(tree: list[dict], folder: str) -> list[str] | None:
    """Port of ``getSiblingLeafFolders``: ordered chapter-folder full paths
    when the sibling run applies, else ``None`` (single chapter)."""
    if len(folder.split("/")) - 1 < _SIBLING_MIN_DEPTH:
        return None
    found = _find_container(tree, folder)
    if found is None:
        return None
    siblings, parent_path = found
    if not parent_path:
        return None  # root-level node — no parent run (viewer parity)
    leaves = [
        _compose(parent_path, n["name"])
        for n in siblings
        if n.get("type") == "folder" and _has_only_images(n)
    ]
    if len(leaves) <= 1:
        return None
    return leaves


# ── shared enumeration bridge ────────────────────────────────────────────


def _archive_source(downloads_dir: Path, folder: str) -> Path | None:
    """The archive/PDF file behind *folder* — the file itself, or the
    longest ancestor that is one (inner-prefix paths)."""
    file_path = downloads_dir / folder
    try:
        if is_archive_file(file_path) or is_pdf_file(file_path):
            return file_path
    except (OSError, ValueError):
        return None
    resolved = _resolve_archive(downloads_dir, folder)
    if resolved is not None:
        return resolved[0]
    return None


def _container_intact(path: Path) -> bool:
    """Cheap "is this really its claimed container type" check.

    Distinguishes a BROKEN archive (container unrecognized — wrong magic,
    truncated headers) from an EMPTY one (valid container, zero image
    entries). Listing readers swallow every error into ``[]``; this check
    is what keeps the two failure kinds apart.
    """
    suffix = path.suffix.lower()
    try:
        if suffix in RAR_EXTENSIONS:
            try:
                import rarfile

                return bool(rarfile.is_rarfile(str(path)))
            except ImportError:
                with open(path, "rb") as f:
                    return f.read(7) == b"Rar!\x1a\x07"
        if suffix in PDF_EXTENSIONS:
            with open(path, "rb") as f:
                return f.read(5) == b"%PDF-"
        return zipfile.is_zipfile(path)
    except (OSError, ValueError):
        return False


def _chapter_pages(downloads_dir: Path, tree: list[dict], folder: str) -> list[str]:
    """Page names for one chapter — identical precedence to loadImages:
    tree leaf children, then mixed-folder direct images, then the shared
    ``/api/images`` enumeration (never a second implementation)."""
    node = _find_tree_node(tree, folder) if folder else None
    if node is not None and _has_only_images(node):
        return [c["name"] for c in node["children"]]
    if node is not None and _has_images(node):
        # Mixed folder: direct image children only — the subfolders' pages
        # live in their own leaf chapters, exactly like the solo viewer.
        return [c["name"] for c in node["children"] if c.get("type") == "image"]
    try:
        return _list_folder_images(downloads_dir, folder)
    except HTTPException as e:
        if e.status_code == 403:
            raise ManifestError(ERR_FORBIDDEN, str(e.detail)) from e
        if e.status_code == 404:
            # A path under (or pointing at) an unreadable archive reports
            # broken, not "not found".
            arc = _archive_source(downloads_dir, folder)
            if arc is not None and not _container_intact(arc):
                raise ManifestError(
                    ERR_BROKEN_ARCHIVE, f"unreadable archive for {folder!r}"
                ) from e
            raise ManifestError(ERR_NOT_FOUND, str(e.detail)) from e
        raise ManifestError(ERR_MALFORMED, str(e.detail)) from e
    except (OSError, ValueError) as e:
        # Permission / unresolvable fs errors during enumeration.
        raise ManifestError(ERR_FORBIDDEN, f"{type(e).__name__}: {e}") from e


# ── manifest build (sync — runs off the event loop) ─────────────────────


def _build_manifest_core(
    downloads_dir: Path, tree: list[dict], folder: str, offset: int
) -> dict:
    """Resolve *folder* into the shared manifest (content fields + digest).

    Containment first (same ``_is_within`` rule as ``/api/images``), then
    chapter selection, then per-chapter pages, then the S01 spread plan.
    """
    if not _is_within(downloads_dir, downloads_dir / folder):
        raise ManifestError(ERR_FORBIDDEN, f"path outside downloads dir: {folder!r}")

    node = _find_tree_node(tree, folder) if folder else None
    is_mixed = node is not None and _has_images(node) and not _has_only_images(node)
    siblings = _sibling_leaf_folders(tree, folder)

    # loadImages parity: cross-chapter iff a multi-leaf sibling run exists
    # AND the opened folder isn't itself mixed.
    if siblings is not None and len(siblings) > 1 and not is_mixed:
        chapter_folders = siblings
        in_tree_names = True
    else:
        chapter_folders = [folder]
        in_tree_names = False

    chapters: list[dict] = []
    page_refs: list[dict] = []
    for cf in chapter_folders:
        names = _chapter_pages(downloads_dir, tree, cf)
        cnode = _find_tree_node(tree, cf) if cf else None
        if in_tree_names and cnode is not None:
            cname = cnode["name"]  # viewer uses the node name (flattened ok)
        else:
            cname = cf.split("/")[-1] if cf else ""  # single mode: basename
        pages = [{"folder": cf, "name": n} for n in names]
        chapters.append({"name": cname, "folder": cf, "pages": pages})
        page_refs.extend(pages)

    if not page_refs:
        arc = _archive_source(downloads_dir, folder)
        if arc is not None and not _container_intact(arc):
            raise ManifestError(
                ERR_BROKEN_ARCHIVE, f"unreadable archive for {folder!r}"
            )
        raise ManifestError(ERR_EMPTY_WORK, f"no pages in work: {folder!r}")

    # S01 builds the shared spread table — chapters never share a spread.
    plan = model.build_spread_plan(
        [{"folder": c["folder"], "pages": c["pages"]} for c in chapters],
        offset,
    )
    spread_table = [model.serialize_spread(s) for s in plan]
    core = {
        "v": protocol.PROTOCOL_VERSION,
        "folder": folder,
        "offset": offset,
        "chapters": chapters,
        "pageRefs": page_refs,
        "spreadTable": spread_table,
    }
    core["digest"] = manifest_digest(core)
    return core


# ── digest / canonical bytes / chunking ──────────────────────────────────


def _canonical(obj) -> str:
    """Deterministic JSON: sorted keys, compact separators, ASCII-only
    (ensure_ascii=True) so byte offsets equal char offsets — chunk slices
    can never split a multibyte character."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def manifest_digest(manifest: dict) -> str:
    """sha256 over the canonical content fields (``_CONTENT_KEYS``)."""
    try:
        content = {k: manifest[k] for k in _CONTENT_KEYS}
    except (KeyError, TypeError) as e:
        raise ManifestError(ERR_MALFORMED, f"manifest missing content key: {e}") from e
    return "sha256:" + hashlib.sha256(_canonical(content).encode("ascii")).hexdigest()


def encode_manifest(manifest: dict) -> bytes:
    """Canonical wire bytes of the full manifest dict (ASCII JSON text —
    never image bytes)."""
    return _canonical(manifest).encode("ascii")


def split_manifest(manifest: dict, max_bytes: int = MANIFEST_CHUNK_BYTES) -> list[dict]:
    """Split the canonical manifest bytes into bounded chunk dicts.

    Each chunk ``{v, kind:"manifest-chunk", digest, index, count, data}``
    still fits one normal-size wire frame; joined in index order the data
    reassembles to byte-identical canonical text, so the digest survives.
    Refuses an over-cap aggregate (ERR_SIZE) or a chunk budget that would
    itself overflow a frame.
    """
    if not isinstance(manifest, dict):
        raise ManifestError(ERR_MALFORMED, "manifest must be a dict")
    digest = manifest.get("digest")
    if not isinstance(digest, str) or manifest_digest(manifest) != digest:
        raise ManifestError(ERR_MALFORMED, "manifest digest missing or stale")
    data = _canonical(manifest)
    if len(data) > MAX_MANIFEST_BYTES:
        raise ManifestError(ERR_SIZE, f"manifest exceeds {MAX_MANIFEST_BYTES} bytes")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        raise ManifestError(ERR_MALFORMED, "max_bytes must be a positive int")
    slices = [data[i:i + max_bytes] for i in range(0, len(data), max_bytes)] or [""]
    chunks: list[dict] = []
    for i, piece in enumerate(slices):
        chunk = {
            "v": protocol.PROTOCOL_VERSION,
            "kind": _MANIFEST_KIND,
            "digest": digest,
            "index": i,
            "count": len(slices),
            "data": piece,
        }
        try:
            protocol.encode_frame(chunk)
        except protocol.ProtocolError as e:
            raise ManifestError(ERR_SIZE, "chunk exceeds the frame cap") from e
        chunks.append(chunk)
    return chunks


def join_manifest(chunks) -> dict:
    """Reassemble ``split_manifest`` output; verifies count, indices, the
    declared digest and the recomputed digest — a tampered or incomplete
    set is a typed error, never a partial manifest."""
    if not isinstance(chunks, (list, tuple)) or not chunks:
        raise ManifestError(ERR_MALFORMED, "no manifest chunks")
    digests: set[str] = set()
    count: int | None = None
    parts: dict[int, str] = {}
    for ch in chunks:
        if (not isinstance(ch, dict)
                or ch.get("v") != protocol.PROTOCOL_VERSION
                or ch.get("kind") != _MANIFEST_KIND
                or not isinstance(ch.get("index"), int)
                or isinstance(ch.get("index"), bool)
                or not isinstance(ch.get("count"), int)
                or isinstance(ch.get("count"), bool)
                or not isinstance(ch.get("digest"), str)
                or not isinstance(ch.get("data"), str)):
            raise ManifestError(ERR_MALFORMED, "malformed manifest chunk")
        digests.add(ch["digest"])
        if count is None:
            count = ch["count"]
        elif count != ch["count"]:
            raise ManifestError(ERR_MALFORMED, "inconsistent chunk count")
        if ch["index"] in parts:
            raise ManifestError(ERR_MALFORMED, "duplicate chunk index")
        parts[ch["index"]] = ch["data"]
    if count is None or count != len(chunks) or len(digests) != 1:
        raise ManifestError(ERR_MALFORMED, "manifest chunk set incomplete")
    if sorted(parts) != list(range(count)):
        raise ManifestError(ERR_MALFORMED, "manifest chunk indices missing")
    data = "".join(parts[i] for i in range(count))
    if len(data.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise ManifestError(ERR_SIZE, f"manifest exceeds {MAX_MANIFEST_BYTES} bytes")
    try:
        manifest = json.loads(data)
    except json.JSONDecodeError as e:
        raise ManifestError(ERR_MALFORMED, "manifest payload is not json") from e
    declared = digests.pop()
    if not isinstance(manifest, dict) or manifest.get("digest") != declared:
        raise ManifestError(ERR_MALFORMED, "manifest digest field mismatch")
    if manifest_digest(manifest) != declared:
        raise ManifestError(ERR_MALFORMED, "manifest digest verification failed")
    return manifest


# ── public entry point ────────────────────────────────────────────────────


async def resolve_work(connection, openedFolder, initialPageRef, offset) -> dict:
    """Resolve *openedFolder* into the shared work manifest.

    ``connection`` is any HTTPConnection (Request/WebSocket — only
    ``.app.state`` is consumed): the tree/index come from the shared
    ``metadata_index_cache`` (never a fresh scan on the TURN path), and
    resolved manifests are memoized per tree generation on
    ``app.state.reader_manifest_cache`` so repeated resolves cost zero
    enumeration.

    Returns ``{v, folder, offset, chapters, pageRefs, spreadTable, digest,
    initial: {page, spreadId}}``. ``initial.page`` is always the input
    ``initialPageRef`` — a ref absent from the work is
    ``ERR_PAGE_NOT_FOUND``, never a clamped/fallback position.
    """
    if not isinstance(openedFolder, str):
        raise ManifestError(ERR_MALFORMED, "openedFolder must be str")
    folder = _norm_path(openedFolder)
    if ".." in folder.split("/"):
        raise ManifestError(ERR_FORBIDDEN, f"path segment '..': {openedFolder!r}")
    if isinstance(offset, bool) or offset not in (0, 1):
        raise ManifestError(ERR_RANGE, f"offset must be 0 or 1, got {offset!r}")
    if initialPageRef is None:
        raise ManifestError(ERR_NO_INITIAL_PAGE, "initialPageRef is required")
    if isinstance(initialPageRef, PageRef):
        ref = initialPageRef
    elif isinstance(initialPageRef, dict):
        f, n = initialPageRef.get("folder"), initialPageRef.get("name")
        if not isinstance(f, str) or not isinstance(n, str):
            raise ManifestError(ERR_MALFORMED, "initialPageRef needs {folder:str,name:str}")
        ref = PageRef(_norm_path(f), n)
    else:
        raise ManifestError(ERR_MALFORMED, "initialPageRef must be a PageRef dict")

    state = connection.app.state
    tree, _index = await _ensure_tree_and_index(connection)
    downloads_dir = state.settings.downloads_path

    # Memoize resolved manifests per tree generation: the dict holds the
    # tree reference, so `is` identity also pins the id() stable.
    cache = getattr(state, "reader_manifest_cache", None)
    if not isinstance(cache, dict) or cache.get("tree") is not tree:
        cache = {"tree": tree, "items": {}}
        state.reader_manifest_cache = cache
    key = (folder, offset)
    core = cache["items"].get(key)
    if core is None:
        # Heavy enumeration stays off the event loop (§3.4).
        core = await run_in_threadpool(
            _build_manifest_core, downloads_dir, tree, folder, offset
        )
        cache["items"][key] = core

    refd = {"folder": ref.folder, "name": ref.name}
    if refd not in core["pageRefs"]:
        raise ManifestError(
            ERR_PAGE_NOT_FOUND, f"initial page not in work: {refd!r}"
        )
    spread_id = None
    for s in core["spreadTable"]:
        if s["right"] == refd or s["left"] == refd:
            spread_id = s["id"]
            break
    result = copy.deepcopy(core)
    result["initial"] = {"page": refd, "spreadId": spread_id}
    return result
