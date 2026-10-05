"""Search, relevance scoring, filtering and sorting helpers."""

from app.scanner import natural_key

from .models import ComicMetadata

__all__ = ["_calculate_relevance", "_get_field_values", "_matches_filters"]


def _calculate_relevance(query: str, comic: ComicMetadata) -> float:
    """Score *comic* against the *query*.

    Returns 0 when nothing matches (caller should exclude).
    """
    words = [w.strip().lower() for w in query.split() if w.strip()]
    if not words:
        return 0.0

    # Pre-lowercase searchable fields (once, not per word)
    title = (comic.title_jp + " " + comic.title_en).lower()
    artists = [a.lower() for a in comic.artist]
    tags = [t.lower() for t in comic.tags]

    score = 0.0
    matched_fields: set[str] = set()

    for word in words:
        # Title match (+10)
        if word in title:
            score += 10
            matched_fields.add("title")

        # Artist match (+8)
        if any(word in a for a in artists):
            score += 8
            matched_fields.add("artist")

        # Tag match (+3 per tag, per word)
        if any(word in t for t in tags):
            score += 3
            matched_fields.add("tag")

    # Multi-field match bonus: +2 per matching field beyond the first
    if len(matched_fields) > 1:
        score += (len(matched_fields) - 1) * 2

    return score


def _get_field_values(comic: ComicMetadata, field: str) -> list[str]:
    """Return the list of values for a given filter field name."""
    if field not in ("artist", "tags"):
        return []
    return getattr(comic, field)


def _matches_filters(comic: ComicMetadata, filters: dict) -> bool:
    """Check if *comic* satisfies all filter constraints.

    Values within a key use OR semantics; keys are AND-ed.
    """
    for key, values in filters.items():
        if not values:
            continue
        comic_values = _get_field_values(comic, key)
        if not comic_values:
            return False
        # OR: at least one filter value must match at least one comic value
        if not any(
            any(fv.lower() in cv.lower() for cv in comic_values)
            for fv in values
        ):
            return False
    return True
