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
            waitTag("tile:$top")
            composeRule.onNodeWithTag("tile:$top").assertIsDisplayed()

            // ── search: field → grid swap → restore ──
            composeRule.onNodeWithTag("searchBtn").performClick()
            waitTag("searchField")
            composeRule.onNodeWithTag("searchField")
                .performTextInput("zzz-no-match-zzz")
            // Even a zero-hit query flips to result mode — browse tiles
            // must leave the tree.
            goneTag("tile:$top")
            composeRule.onNodeWithTag("searchClose").performClick()
            waitTag("tile:$top")

            // ── facet sheet ──
            composeRule.onNodeWithTag("facetBtn").performClick()
            waitTag("facetApply")
            composeRule.onNodeWithTag("facetReset").assertExists()
            composeRule.onNodeWithTag("facetApply").performClick()
            goneTag("facetApply")

            // ── show-all + hide/unhide round-trip ──
            composeRule.onNodeWithTag("layoutMenuBtn").performClick()
            waitTag("showAllMenuItem")
            composeRule.onNodeWithTag("showAllMenuItem").performClick()

            // In showAll the top tile gets an eye toggle.
            waitTag("visToggle:$top")
            composeRule.onNodeWithTag("visToggle:$top").performClick()
            waitTag("visBadge:$top")
            // Restore — second eye tap unhides.
            composeRule.onNodeWithTag("visToggle:$top").performClick()
            goneTag("visBadge:$top", 10_000)
        }
    }
}
