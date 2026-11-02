package dev.bookq.reader

import android.content.Intent
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.compose.ui.test.junit4.createEmptyComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.assertIsDisplayed
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * Smoke: launch MainActivity with `--es serverURL` (instrumentation arg
 * `bookqServerUrl`, default the emulator host bridge), boot in TEST_MODE,
 * and assert a top-level folder tile from the synthetic tree appears.
 * Tiles are tagged `tile:<fullPath>` — the display text splits leaf/parent
 * so the tag is the stable assertion.
 */
@RunWith(AndroidJUnit4::class)
class HomeSmokeTest {

    @get:Rule
    val composeRule = createEmptyComposeRule()

    @Test
    fun homeShowsTopLevelFolder() {
        val args = InstrumentationRegistry.getArguments()
        val url = args.getString("bookqServerUrl") ?: "http://10.0.2.2:8080"
        val expected = args.getString("bookqExpectedFolder") ?: "Series/vol1"
        val intent = Intent(
            ApplicationProvider.getApplicationContext(),
            MainActivity::class.java,
        ).putExtra("serverURL", url)

        ActivityScenario.launch<MainActivity>(intent).use {
            composeRule.waitUntil(timeoutMillis = 30_000) {
                runCatching {
                    composeRule.onNodeWithTag("tile:$expected").assertExists()
                    true
                }.getOrDefault(false)
            }
            composeRule.onNodeWithTag("tile:$expected").assertIsDisplayed()
        }
    }
}
