"""app.metadata package — public API for comic metadata indexing and search."""

from .index import MetadataIndex
from .models import ComicMetadata, _split_aliases

__all__ = ["ComicMetadata", "MetadataIndex", "_split_aliases"]
