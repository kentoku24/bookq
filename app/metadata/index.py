"""MetadataIndex — searchable index of comic metadata."""

import logging
from pathlib import Path

from app.archive import ARCHIVE_EXTENSIONS
from app.scanner import PDF_EXTENSIONS, natural_key

_ARCHIVE_SUFFIXES = ARCHIVE_EXTENSIONS | PDF_EXTENSIONS

from .facets import _build_facets as _facets_build
from .facets import _facets_for, _subtree_facets
from .models import ComicMetadata
from .search import _calculate_relevance, _matches_filters

logger = logging.getLogger(__name__)

__all__ = ["MetadataIndex"]


class MetadataIndex:
    """Searchable index of comic metadata with relevance ranking and facets."""

    def __init__(self) -> None:
        self.comics: list[ComicMetadata] = []
        self.facets: dict[str, list[str]] = {}
        self._subtree_facets_cache: dict[str, dict[str, list[str]]] | None = None

    # ── public API ────────────────────────────────────────────────────────

    def rebuild(self, downloads_path: Path) -> None:
        """Scan all sub-directories under *downloads_path* and rebuild the index."""
        self.comics = []
        self.facets = {}
        self._subtree_facets_cache = None

        if not downloads_path.is_dir():
            logger.warning("downloads_path does not exist: %s", downloads_path)
            return

        self.comics = self._walk(downloads_path, downloads_path)

        self._build_facets()

    def rebuild_from_tree(self, tree: list[dict], downloads_path: Path) -> None:
        """Rebuild the index from a :func:`scan_tree` result instead of walking the FS.

        Parses the same folders :meth:`_walk` would visit: every folder node's
        own path plus, for single-child flattening, each ancestor prefix of a
        flattened name (``"a/b/c"`` → ``"a"``, ``"a/b"``, ``"a/b/c"``),
        whether the flattened name sits on a folder node or — for image-only
        chains — on an image node (whose own filename is never parsed).
        Image leaves themselves are never parsed, and a folder whose parse
        returns *None* still has its descendants visited, mirroring
        :meth:`_walk`. Each folder is parsed at most once even when nested
        nodes repeat an ancestor prefix.
        """
        self.comics = []
        self.facets = {}
        self._subtree_facets_cache = None

        if not downloads_path.is_dir():
            logger.warning("downloads_path does not exist: %s", downloads_path)
            return

        found: list[ComicMetadata] = []
        seen: set[str] = set()

        def _parse_prefixes(prefixes: list[str]) -> None:
            for depth in range(1, len(prefixes) + 1):
                prefix = "/".join(prefixes[:depth])
                if prefix in seen:
                    continue
                seen.add(prefix)
                comic = self._parse_folder(downloads_path / prefix, downloads_path)
                if comic is not None:
                    found.append(comic)

        def _visit(nodes: list[dict], parent_rel: str) -> None:
            for node in nodes:
                if node.get("type") == "folder":
                    rel = f"{parent_rel}/{node['name']}" if parent_rel else node["name"]
                    _parse_prefixes(rel.split("/"))
                    if node.get("children"):
                        _visit(node["children"], rel)
                elif node.get("type") == "image":
                    # Image-only flatten chains carry ancestor dirs in the
                    # image name ("a/b/1.png"); parse the dir prefixes so the
                    # index set matches _walk. The filename itself is skipped.
                    rel = f"{parent_rel}/{node['name']}" if parent_rel else node["name"]
                    dir_parts = rel.split("/")[:-1]
                    if dir_parts:
                        _parse_prefixes(dir_parts)

        _visit(tree, "")
        self.comics = found

        self._build_facets()

    def search(
        self,
        q: str = "",
        filters: dict | None = None,
        sort: str = "relevance",
    ) -> list[dict]:
        """Search the index and return matching comics as dicts.

        Parameters
        ----------
        q : str
            Free-text query.  Multiple words are matched independently and
            their scores combined.
        filters : dict, optional
            Facet filters with optional keys ``artist``, ``tags``,
            each a list of strings.  Values are OR-ed within a key
            and AND-ed across keys.
        sort : str
            Only ``"relevance"`` is supported; any other value falls
            back to relevance order.

        Returns
        -------
        list[dict]
            Serialised :class:`ComicMetadata` matching the query.
        """
        if not q or not q.strip():
            # No query — return all (but still apply filters)
            candidates = [(0.0, c) for c in self.comics]
        else:
            candidates = []
            for comic in self.comics:
                score = _calculate_relevance(q.strip(), comic)
                if score <= 0:
                    continue
                candidates.append((score, comic))

        # Apply filters (post-score so we can filter the candidate list)
        if filters:
            filtered: list[tuple[float, ComicMetadata]] = []
            for score, comic in candidates:
                if _matches_filters(comic, filters):
                    filtered.append((score, comic))
            candidates = filtered

        if sort == "relevance":
            candidates.sort(
                key=lambda x: (-x[0], natural_key(x[1].folder_name), x[1].folder_name)
            )
        else:
            # Unknown sort (e.g. legacy "newest"/"rating") falls back to relevance order.
            candidates.sort(
                key=lambda x: (-x[0], natural_key(x[1].folder_name), x[1].folder_name)
            )

        return [comic.to_dict() for _, comic in candidates]

    def facets_for(self, path: str) -> dict[str, list[str]]:
        """Return facets scoped to *path*'s subtree (``""`` = whole library).

        The subset build does not clobber :attr:`facets` — callers needing
        the whole-library set must pass an empty path.
        """
        return _facets_for(self.comics, path, self.facets)

    def subtree_facets(self) -> dict[str, dict[str, list[str]]]:
        """path -> aggregate facets of every comic in that subtree (path = any ancestor prefix of a comic's folder_path).

        Computed once per :meth:`rebuild` and cached — the result is attached
        to every folder node on each ``/api/folders`` response, so recomputing
        it per request would duplicate work on every browse call.
        """
        if self._subtree_facets_cache is not None:
            return self._subtree_facets_cache
        self._subtree_facets_cache = _subtree_facets(self.comics)
        return self._subtree_facets_cache

    # ── indexing helpers ──────────────────────────────────────────────────

    def _walk(
        self,
        directory: Path,
        downloads_base: Path,
        visited: set[tuple[int, int]] | None = None,
    ) -> list[ComicMetadata]:
        """Depth-first walk of *directory* collecting ComicMetadata records.

        Symlinked directories are followed (a top-level symlinked collection
        must stay indexable and searchable), but each visited directory is
        recorded by its ``(st_dev, st_ino)`` identity so a symlink cycle or a
        subtree reachable through two aliases is traversed only once.
        Non-directories and dot-directories are skipped — at every depth,
        except supported archives (ZIP/CBZ/PDF) which are indexed as comics
        so search/facets keep working for archive works.
        """
        if visited is None:
            visited = set()
            try:
                st = directory.stat()
                visited.add((st.st_dev, st.st_ino))
            except OSError:
                pass  # unreadable root — children will fail individually

        found: list[ComicMetadata] = []
        for entry in sorted(directory.iterdir(), key=lambda p: (natural_key(p.name), p.name)):
            if entry.name.startswith("."):
                continue
            if not entry.is_dir():
                # Archive works (ZIP/CBZ/RAR/CBR/EPUB/PDF) are indexed as comics by name.
                # ComicMetadata.from_folder expects a dir with optional
                # metadata.toml; for archives there is none, so build the
                # minimal record directly (has_metadata=False).
                try:
                    if entry.is_file() and entry.suffix.lower() in _ARCHIVE_SUFFIXES:
                        rel = str(entry.relative_to(downloads_base))
                        found.append(
                            ComicMetadata(
                                folder_path=rel,
                                folder_name=entry.name,
                                has_metadata=False,
                            )
                        )
                except (OSError, ValueError):
                    continue
                continue
            try:
                st = entry.stat()
            except OSError:
                continue  # broken symlink / unreadable entry — nothing to index
            ident = (st.st_dev, st.st_ino)
            if ident in visited:
                continue
            visited.add(ident)
            comic = self._parse_folder(entry, downloads_base)
            if comic is not None:
                found.append(comic)
            found.extend(self._walk(entry, downloads_base, visited))
        return found

    def _parse_folder(self, folder: Path, downloads_base: Path) -> ComicMetadata | None:
        """Parse a single folder and return its metadata, or *None* on error."""
        try:
            return ComicMetadata.from_folder(folder, downloads_base)
        except Exception:
            logger.exception("Unexpected error parsing %s", folder)
            return None

    def _build_facets(self, comics: list[ComicMetadata] | None = None) -> dict[str, list[str]]:
        """Build the facet dictionary from *comics* (all indexed comics by default).

        The built dict is stored on :attr:`facets` only when the default
        (*comics* is None) is used — subset builds return without clobbering
        the cached whole-library facets.
        """
        whole_library = comics is None
        comics = self.comics if comics is None else comics

        built = _facets_build(comics)
        if whole_library:
            self.facets = built
        return built
