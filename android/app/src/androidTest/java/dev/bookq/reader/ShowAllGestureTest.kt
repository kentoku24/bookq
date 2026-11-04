package dev.bookq.reader

import android.content.Intent
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createEmptyComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.swipe
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.uiautomator.UiDevice
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * Show-all entry gesture (iOS `bookqSwipe` parity): dragging the root
 * "bookq" title to the right screen edge (≥90% width) and releasing
 * toggles 全表示モード — editable top tiles gain the eye toggle, and
 * the same gesture exits. The menu item remains an alternative entry.
 *
 * Works against any TEST_MODE tree via `bookqExpectedFolder`
 * (default the CI fixture's top segment).
 */
@RunWith(AndroidJUnit4::class)
class ShowAllGestureTest {

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
    fun titleEdgeSwipeTogglesShowAll() {
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
            check(waitTag("showAllTitle")) { "root title missing" }
            composeRule.onNodeWithTag("tile:$top").assertIsDisplayed()

            val title = composeRule.onNodeWithTag("showAllTitle")
            val bounds = title.fetchSemanticsNode().boundsInWindow
            val screenW = UiDevice.getInstance(
                InstrumentationRegistry.getInstrumentation()).displayWidth
            // Release past the 90%-of-width edge — the web `_edge` /
            // iOS swipeWidth threshold the gesture shares.
            val endLocalX = screenW * 0.95f - bounds.left
            title.performTouchInput {
                swipe(Offset(8f, bounds.height / 2f),
                    Offset(endLocalX, bounds.height / 2f),
                    durationMillis = 400)
            }
            check(waitTag("visToggle:$top")) {
                "edge swipe did not enter show-all" }

            // Same gesture exits — release at the edge again.
            title.performTouchInput {
                swipe(Offset(8f, bounds.height / 2f),
                    Offset(endLocalX, bounds.height / 2f),
                    durationMillis = 400)
            }
            check(goneTag("visToggle:$top")) {
                "second edge swipe did not exit show-all" }
        }
    }
}
