"""ComicMetadata dataclass and pure helpers."""

import logging
import tomllib
from dataclasses import dataclass, field, asdict
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["ComicMetadata", "_split_aliases"]


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
        metadata_file = folder_path / "metadata.toml"

        if not metadata_file.exists():
            return cls(
                folder_path=folder_rel,
                folder_name=folder_path.name,
                has_metadata=False,
            )

        try:
            with open(metadata_file, "rb") as fh:
                data = tomllib.load(fh)
        except Exception:
            logger.warning("Failed to parse %s", metadata_file)
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
