"""ComicMetadata dataclass and pure helpers."""

import logging
import threading
import tomllib
from collections import OrderedDict
from dataclasses import dataclass, field, asdict
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["ComicMetadata", "_split_aliases", "_clear_parse_cache"]


# ── metadata.toml parse cache ─────────────────────────────────────────────────
#
# rebuild_from_tree re-parses every metadata.toml on each rebuild even when
# nothing changed (#245). Entries are keyed by file identity —
# (toml_path, st_mtime_ns, st_size) — so editing one file invalidates exactly
# one entry. The stat that builds the key doubles as the existence check: a
# missing/unreadable file caches None under (toml_path, None, None), and a
# file that later appears simply resolves under its real stat key, so the
# negative entries never go stale.
#
# Values are the raw parsed TOML dicts (None = absent or unparseable). They
# are shared read-only: from_folder copies everything it keeps into fresh
# lists, so callers may mutate the returned ComicMetadata freely and the next
# parse of the same file still builds an independent record.
#
# Guarded by a lock because rebuilds run under run_in_threadpool.

_TOML_CACHE_MAX = 10_000
_TOML_CACHE: OrderedDict[tuple, dict | None] = OrderedDict()
_TOML_CACHE_LOCK = threading.Lock()
_CACHE_MISS = object()


def _clear_parse_cache() -> None:
    """Drop every cached metadata.toml parse (test isolation)."""
    with _TOML_CACHE_LOCK:
        _TOML_CACHE.clear()


def _load_metadata_toml(toml_path: Path) -> dict | None:
    """Return the parsed metadata.toml at *toml_path*, or None when absent/unparseable.

    Memoized per file identity — ``(toml_path, st_mtime_ns, st_size)`` — so a
    repeat index rebuild re-parses only files whose stat changed. The failed-
    stat negative case is cached too (under ``(toml_path, None, None)``); the
    stat is needed for the key anyway, and when the file appears its real
    (mtime_ns, size) produces a different key.
    """
    try:
        st = toml_path.stat()
        key = (toml_path, st.st_mtime_ns, st.st_size)
    except OSError:
        st = None
        key = (toml_path, None, None)

    with _TOML_CACHE_LOCK:
        hit = _TOML_CACHE.get(key, _CACHE_MISS)
        if hit is not _CACHE_MISS:
            _TOML_CACHE.move_to_end(key)
            return hit

    data: dict | None = None
    if st is not None:
        try:
            with open(toml_path, "rb") as fh:
                data = tomllib.load(fh)
        except Exception:
            logger.warning("Failed to parse %s", toml_path)

    with _TOML_CACHE_LOCK:
        _TOML_CACHE[key] = data
        _TOML_CACHE.move_to_end(key)
        while len(_TOML_CACHE) > _TOML_CACHE_MAX:
            _TOML_CACHE.popitem(last=False)
    return data


def _split_aliases(raw: list[str]) -> list[str]:
    """Split ' | '-separated aliases into individual strings."""
    result: list[str] = []
    for entry in raw:
        for part in entry.split(" | "):
            part = part.strip()
            if part:
                result.append(part)
    return result


@dataclass
class ComicMetadata:
    """Structured metadata for a single comic folder."""

    folder_path: str
    folder_name: str
    title_jp: str = ""
    title_en: str = ""
    artist: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    source: str = ""
    has_metadata: bool = False
    total_pages: int = 0

    @classmethod
    def from_folder(cls, folder_path: Path, downloads_base: Path) -> "ComicMetadata":
        """Parse metadata.toml from *folder_path*, or return basic folder-name metadata.

        When no metadata.toml exists (or parsing fails) a minimal record with
        *has_metadata=False* is returned so callers never receive *None* from
        this method.  Use :meth:`MetadataIndex._parse_folder` if you want
        *None* on unexpected errors.
        """
        folder_rel = str(folder_path.relative_to(downloads_base))
        data = _load_metadata_toml(folder_path / "metadata.toml")

        if data is None:
            return cls(
                folder_path=folder_rel,
                folder_name=folder_path.name,
                has_metadata=False,
            )

        source_data = data.get("source", {})
        comic_data = data.get("comic", {})
        tags_data = data.get("tags", {})

        source_name = source_data.get("name", "") if isinstance(source_data, dict) else ""

        return cls(
            folder_path=folder_rel,
            folder_name=folder_path.name,
            title_jp=comic_data.get("title_jp", "") if isinstance(comic_data, dict) else "",
            title_en=comic_data.get("title_en", "") if isinstance(comic_data, dict) else "",
            artist=_split_aliases(tags_data.get("artist", [])) if isinstance(tags_data, dict) else [],
            tags=cls._flatten_tags(tags_data) if isinstance(tags_data, dict) else [],
            source=source_name,
            has_metadata=True,
            total_pages=int(comic_data["total_pages"]) if isinstance(comic_data, dict) and comic_data.get("total_pages") is not None else 0,
        )

    def to_dict(self) -> dict:
        """Convert to a plain dict suitable for JSON serialisation."""
        return asdict(self)

    @staticmethod
    def _flatten_tags(tags_data: dict) -> list[str]:
        """Flatten tag categories into a single deduplicated list.

        Includes both English and comma-separated Japanese translations
        (e.g. ``"flower,花"`` → ``["flower", "花"]``).
        """
        result: list[str] = []
        for key in ("male", "female", "mixed", "other", "tag", "misc"):
            entries = tags_data.get(key, [])
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, str):
                    continue
                # Each entry may contain comma-separated translations
                for part in entry.split(","):
                    part = part.strip()
                    if part:
                        result.append(part)
        # Deduplicate preserving order (case-sensitive to keep e.g. 花 separate)
        seen: set[str] = set()
        unique: list[str] = []
        for item in result:
            if item not in seen:
                seen.add(item)
                unique.append(item)
        return unique
