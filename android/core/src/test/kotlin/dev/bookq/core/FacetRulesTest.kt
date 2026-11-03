package dev.bookq.core

import dev.bookq.core.api.Facets
import dev.bookq.core.api.TreeNode
import dev.bookq.core.search.FacetRules
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Port of `app.js` `facetGroupMatches` / `nodeMatchesFilters`. */
class FacetRulesTest {

    private fun node(artists: List<String>?, tags: List<String>?) =
        TreeNode(
            name = "n", type = "folder",
            subtreeFacets = if (artists == null && tags == null) null
            else Facets(artists ?: emptyList(), tags ?: emptyList()),
        )

    @Test fun filtersActive() {
        assertFalse(FacetRules.filtersActive(emptyList(), emptyList()))
        assertTrue(FacetRules.filtersActive(listOf("a"), emptyList()))
        assertTrue(FacetRules.filtersActive(emptyList(), listOf("t")))
    }

    @Test fun groupMatches() {
        // empty filter set passes everything
        assertTrue(FacetRules.groupMatches(null, emptyList()))
        assertTrue(FacetRules.groupMatches(listOf("x"), emptyList()))
        // substring, case-insensitive, ANY filter value may match
        assertTrue(FacetRules.groupMatches(listOf("spring 2024"), listOf("SPRING")))
        assertTrue(FacetRules.groupMatches(listOf("other"), listOf("x", "oth")))
        assertFalse(FacetRules.groupMatches(listOf("abc"), listOf("x")))
        assertFalse(FacetRules.groupMatches(null, listOf("x")))
    }

    @Test fun nodeMatches() {
        val n = node(listOf("Artist A"), listOf("romance", "drama"))
        assertTrue(FacetRules.nodeMatches(n, listOf("artist a"), emptyList()))
        assertTrue(FacetRules.nodeMatches(n, emptyList(), listOf("rom")))
        // both armed groups must pass
        assertFalse(FacetRules.nodeMatches(n, listOf("nobody"), listOf("romance")))
        // node without subtree_facets never matches an armed filter
        assertFalse(FacetRules.nodeMatches(node(null, null), listOf("a"), emptyList()))
    }
}
