package dev.bookq.core.search

import dev.bookq.core.api.TreeNode

/**
 * Facet filter matching — `app.js` `facetGroupMatches` / `nodeMatchesFilters`
 * equivalent. Active artist/tag filters narrow the browse level by each
 * node's `subtree_facets` (a folder shows when its SUBTREE carries the
 * metadata, so drilling stays possible).
 */
object FacetRules {

    /** Any artist or tag filter armed? (`filtersActive`) */
    fun filtersActive(artists: List<String>, tags: List<String>): Boolean =
        artists.isNotEmpty() || tags.isNotEmpty()

    /**
     * One group (`facetGroupMatches`): empty filter set passes; otherwise
     * ANY filter value must appear as a case-insensitive substring in at
     * least one of the node's values. `contains` (not equality) — web
     * parity, so "spring" matches "spring 2024".
     */
    fun groupMatches(nodeValues: List<String>?, filterValues: List<String>): Boolean {
        if (filterValues.isEmpty()) return true
        return filterValues.any { fv ->
            (nodeValues ?: emptyList()).any { cv -> cv.contains(fv, ignoreCase = true) }
        }
    }

    /**
     * `nodeMatchesFilters` — both groups must pass; a node with NO
     * `subtree_facets` never matches an armed filter (web returns false
     * for `!f` — unknown metadata is not a silent pass).
     */
    fun nodeMatches(node: TreeNode, artists: List<String>, tags: List<String>): Boolean {
        val f = node.subtreeFacets ?: return false
        return groupMatches(f.artists, artists) && groupMatches(f.tags, tags)
    }
}
