"""Facet aggregation helpers."""

from app.folder_visibility import covered_by_roots

from .models import ComicMetadata

__all__ = ["_build_facets", "_facets_for", "_subtree_facets"]


def _build_facets(comics: list[ComicMetadata]) -> dict[str, list[str]]:
    """Build the facet dictionary from *comics*."""
    artists: set[str] = set()
    all_tags: set[str] = set()

    for comic in comics:
        artists.update(comic.artist)
        all_tags.update(comic.tags)

    return {
        "artists": sorted(artists, key=str.casefold),
        "tags": sorted(all_tags, key=str.casefold),
    }


# Generation-scoped per-path memo for _facets_for. Key is id(comics); the
# entry holds a strong reference to the comics list so the id can never be
# recycled while cached, and a new list object (every index rebuild) gets a
# fresh id and therefore a recompute. Capped to recent generations. Within one
# generation the per-prefix sorted build runs once per path; the returned
# object is shared read-only (callers only serialize it), mirroring how the
# empty-path case already returns the shared whole_facets object.
_FACETS_FOR_MEMO_MAX_GENS = 8
_FACETS_FOR_MEMO: dict[int, tuple[list[ComicMetadata], dict[str, dict[str, list[str]]]]] = {}


def _facets_for(comics: list[ComicMetadata], path: str, whole_facets: dict[str, list[str]], exclude_roots=()) -> dict[str, list[str]]:
    """Return facets scoped to *path*'s subtree (``""`` = whole library).

    *exclude_roots* drops comics covered by a hidden root — the caller's
    own roots, so per-user sets never share the unfiltered aggregate.
    """
    roots = tuple(sorted(r for r in (exclude_roots or ()) if isinstance(r, str) and r))
    if not path and not roots:
        return whole_facets
    gen = id(comics)
    entry = _FACETS_FOR_MEMO.get(gen)
    if entry is None or entry[0] is not comics:
        if len(_FACETS_FOR_MEMO) >= _FACETS_FOR_MEMO_MAX_GENS:
            _FACETS_FOR_MEMO.pop(next(iter(_FACETS_FOR_MEMO)))
        entry = (comics, {})
        _FACETS_FOR_MEMO[gen] = entry
    per_path = entry[1]
    key = (path, roots)
    if key not in per_path:
        if not path:
            # Whole library: every comic is in scope; only hidden-root
            # coverage narrows it (path prefixing would drop all of them —
            # "" + "/" never prefixes a relative folder_path).
            subset = [c for c in comics
                      if not covered_by_roots(c.folder_path, roots)]
        else:
            prefix = path.rstrip("/") + "/"
            subset = [
                c for c in comics
                if (c.folder_path == path or c.folder_path.startswith(prefix))
                and not covered_by_roots(c.folder_path, roots)
            ]
        per_path[key] = _build_facets(subset)
    return per_path[key]


def _subtree_facets(comics: list[ComicMetadata]) -> dict[str, dict[str, list[str]]]:
    """path -> aggregate facets of every comic in that subtree."""
    agg: dict[str, dict[str, set[str]]] = {}
    for c in comics:
        parts = c.folder_path.split("/")
        prefix = ""
        for i, part in enumerate(parts):
            prefix = part if i == 0 else prefix + "/" + part
            a = agg.setdefault(prefix, {"artists": set(), "tags": set()})
            a["artists"].update(c.artist)
            a["tags"].update(c.tags)
    return {
        path: {
            "artists": sorted(a["artists"], key=str.casefold),
            "tags": sorted(a["tags"], key=str.casefold),
        }
        for path, a in agg.items()
    }
