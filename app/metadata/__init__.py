"""app.metadata package — public API for comic metadata indexing and search."""

from .index import MetadataIndex
from .models import ComicMetadata, _clear_parse_cache, _split_aliases

__all__ = ["ComicMetadata", "MetadataIndex", "_split_aliases", "_clear_parse_cache"]
