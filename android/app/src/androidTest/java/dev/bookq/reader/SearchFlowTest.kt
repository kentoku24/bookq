package dev.bookq.reader

import android.content.Intent
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createEmptyComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * M6 search/facets + visibility show-all flow (issue #298):
 *  - search icon → field → any query swaps the browse grid for the
 *    global result grid (`searchActive`), close restores it.
 *  - facet button → bottom sheet opens (footer buttons deterministic).
 *  - menu → すべて表示 toggles showAll; a top tile's eye then hides it
 *    (`visBadge:` appears) and a second tap restores it.
 *
 * Works against any TEST_MODE tree via `bookqExpectedFolder`
 * (default the CI fixture's top segment).
 */
@RunWith(AndroidJUnit4::class)
class SearchFlowTest {

    @get:Rule
    val composeRule = createEmptyComposeRule()

    private fun waitTag(tag: String, ms: Long = 15_000): Boolean = runCatching {
        composeRule.waitUntil(timeoutMillis = ms) {
            composeRule.onAllNodesWithTag(tag).fetchSemanticsNodes().isNotEmpty()
        }
        true
    }.getOrDefault(false)

    private fun goneTag(tag: String, ms: Long = 15_000): Boolean = runCatching {
        composeRule.waitUntil(timeoutMillis = ms) {
            composeRule.onAllNodesWithTag(tag).fetchSemanticsNodes().isEmpty()
        }
        true
    }.getOrDefault(false)

    /** Every testTag in the merged tree — failure diagnostics only. */
    private fun allTags(): List<String> {
        val out = mutableListOf<String>()
        fun walk(n: androidx.compose.ui.semantics.SemanticsNode) {
            val k = androidx.compose.ui.semantics.SemanticsProperties.TestTag
            if (k in n.config) out.add(n.config[k])
            n.children.forEach(::walk)
        }
        return runCatching {
            composeRule.onAllNodes(androidx.compose.ui.test.isRoot())
                .fetchSemanticsNodes().forEach(::walk)
            out
        }.getOrDefault(out)
    }

    @Test
    fun searchFacetAndShowAllFlow() {
        val args = InstrumentationRegistry.getArguments()
        val url = args.getString("bookqServerUrl") ?: "http://10.0.2.2:8080"
        val top = (args.getString("bookqExpectedFolder") ?: "Series")
            .substringBefore('/')
        val intent = Intent(
            ApplicationProvider.getApplicationContext(),
            MainActivity::class.java,
        ).putExtra("serverURL", url)

        ActivityScenario.launch<MainActivity>(intent).use {
            check(waitTag("tile:$top")) { "browse grid never rendered" }
            composeRule.onNodeWithTag("tile:$top").assertIsDisplayed()

            // ── search: field → grid swap → restore ──
            composeRule.onNodeWithTag("searchBtn").performClick()
            check(waitTag("searchField")) { "search field did not open" }
            composeRule.onNodeWithTag("searchField")
                .performTextInput("zzz-no-match-zzz")
            // Even a zero-hit query flips to result mode — browse tiles
            // must leave the tree.
            check(goneTag("tile:$top")) {
                "search results did not replace the browse grid" }
            composeRule.onNodeWithTag("searchClose").performClick()
            check(waitTag("tile:$top")) { "browse grid did not return" }

            // ── facet sheet ──
            composeRule.onNodeWithTag("facetBtn").performClick()
            check(waitTag("facetApply")) { "facet sheet did not open" }
            composeRule.onNodeWithTag("facetReset").assertExists()
            composeRule.onNodeWithTag("facetApply").performClick()
            check(goneTag("facetApply")) { "facet sheet did not close" }

            // ── show-all + hide/unhide round-trip ──
            composeRule.onNodeWithTag("layoutMenuBtn").performClick()
            check(waitTag("showAllMenuItem")) { "menu did not open" }
            composeRule.onNodeWithTag("showAllMenuItem").performClick()

            // In showAll the top tile gets an eye toggle.
            check(waitTag("visToggle:$top")) {
                "no visToggle on the top tile in show-all" }
            try {
                composeRule.onNodeWithTag("visToggle:$top").performClick()
                check(waitTag("visBadge:$top")) {
                    "hide did not mark the top tile — tags: " + allTags() }
            } finally {
                // Restore only when the tile is actually marked — a blind
                // second tap would HIDE it when the first write failed.
                val marked = composeRule.onAllNodesWithTag("visBadge:$top")
                    .fetchSemanticsNodes().isNotEmpty()
                if (marked) {
                    runCatching {
                        composeRule.onNodeWithTag("visToggle:$top")
                            .performClick()
                    }
                    check(goneTag("visBadge:$top", 10_000)) {
                        "unhide did not clear the badge" }
                }
            }
        }
    }
}
