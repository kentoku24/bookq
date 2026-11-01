package dev.bookq.reader

import android.content.Intent
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.compose.ui.test.junit4.createEmptyComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.assertIsDisplayed
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * M1 smoke: launch MainActivity with `--es serverURL` (instrumentation arg
 * `bookqServerUrl`, default the emulator host bridge), boot in TEST_MODE,
 * and assert a top-level folder name from the synthetic tree appears.
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
                    composeRule.onNodeWithText(expected).assertExists()
                    true
                }.getOrDefault(false)
            }
            composeRule.onNodeWithText(expected).assertIsDisplayed()
        }
    }
}
