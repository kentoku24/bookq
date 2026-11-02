package dev.bookq.reader

import android.content.Intent
import android.os.Build
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.SemanticsNodeInteraction
import androidx.compose.ui.test.junit4.createEmptyComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.hasScrollToIndexAction
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollToNode
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.uiautomator.UiDevice
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * M5 offline-download flow: badge tap → WorkManager download → COMPLETE →
 * files on disk → reader renders the downloaded work (on the emulator the
 * radio is cut to prove the local-first read path) → confirm-delete →
 * files gone. Badge state is read off the `stateDescription` semantics
 * (`offline:<STATE>`) — deterministic, no window-focus dependency.
 *
 * POST_NOTIFICATIONS is pre-granted via UiAutomation so the system dialog
 * never interrupts the flow.
 */
@RunWith(AndroidJUnit4::class)
class OfflineDownloadTest {

    @get:Rule
    val composeRule = createEmptyComposeRule()

    private val instr = InstrumentationRegistry.getInstrumentation()
    private val d = UiDevice.getInstance(instr)
    private val pkg = "dev.bookq.reader.debug"

    private fun badge(tag: String): SemanticsNodeInteraction =
        composeRule.onNodeWithTag("offline:$tag")

    private fun stateOf(tag: String): String? = runCatching {
        badge(tag).fetchSemanticsNode()
            .config.getOrElseNullable(SemanticsProperties.StateDescription) { null }
    }.getOrNull()

    private fun waitState(tag: String, want: String, ms: Long): Boolean = runCatching {
        composeRule.waitUntil(timeoutMillis = ms) { stateOf(tag) == "offline:$want" }
    }.isSuccess

    private fun shell(cmd: String): String = runCatching {
        d.executeShellCommand(cmd).trim()
    }.getOrDefault("")

    private fun isEmulator(): Boolean =
        Build.FINGERPRINT.contains("generic") ||
            Build.FINGERPRINT.contains("sdk_") ||
            Build.HARDWARE.contains("ranchu") ||
            Build.HARDWARE.contains("goldfish")

    private fun offlineFiles(): List<String> =
        shell("run-as $pkg find files/offline -type f")
            .lines().filter { it.isNotBlank() && it.startsWith("files/") }

    private val pageCounter = SemanticsMatcher("page counter") { n ->
        (n.config.getOrElseNullable(SemanticsProperties.Text) { null }
            ?.joinToString("") { it.text } ?: "")
            .matches(Regex("\\d+ / \\d+"))
    }

    private fun waitForTile(tag: String, ms: Long): Boolean = runCatching {
        composeRule.waitUntil(timeoutMillis = ms) {
            runCatching {
                composeRule.onNodeWithTag("tile:$tag").assertExists(); true
            }.getOrDefault(false)
        }
    }.isSuccess

    /** waitForTile + lazy-container scroll — off-screen tiles aren't
     *  composed at all; `performScrollToNode` drives the lazy grid to the
     *  node directly (ScrollToIndex semantics), then a short confirm wait. */
    private fun ensureTile(tag: String, ms: Long): Boolean {
        if (waitForTile(tag, 4_000)) return true
        val grids = composeRule.onAllNodes(hasScrollToIndexAction())
        val count = grids.fetchSemanticsNodes().size
        if (count == 0) return waitForTile(tag, ms)
        for (i in 0 until count) {
            runCatching {
                grids[i].performScrollToNode(hasTestTag("tile:$tag"))
            }
            if (waitForTile(tag, 3_000)) return true
        }
        return waitForTile(tag, ms)
    }

    private fun waitForCounter(ms: Long): Boolean = runCatching {
        composeRule.waitUntil(timeoutMillis = ms) {
            composeRule.onAllNodes(pageCounter).fetchSemanticsNodes().isNotEmpty()
        }
    }.isSuccess

    @Test
    fun downloadCompleteReadDelete() {
        val args = InstrumentationRegistry.getArguments()
        val url = args.getString("bookqServerUrl") ?: "http://10.0.2.2:8080"
        val expected = args.getString("bookqExpectedFolder") ?: "Series/vol1"
        // `bookqDrill`: |-separated ancestor tile paths to tap before the
        // leaf is visible (real trees are deeper than CI's root-level leaf).
        val drill = (args.getString("bookqDrill") ?: "")
            .split("|").filter { it.isNotBlank() }
        val intent = Intent(
            ApplicationProvider.getApplicationContext(),
            MainActivity::class.java,
        ).putExtra("serverURL", url)

        instr.uiAutomation.grantRuntimePermission(
            pkg, android.Manifest.permission.POST_NOTIFICATIONS)

        var scenario = ActivityScenario.launch<MainActivity>(intent)
        try {
            for (step in drill) {
                assertTrue("drill tile $step never rendered", ensureTile(step, 30_000))
                composeRule.onNodeWithTag("tile:$step").performClick()
            }
            assertTrue("tile $expected never rendered", ensureTile(expected, 30_000))

            // IDLE → tap → listing → downloading → COMPLETE
            assertTrue("badge not IDLE initially (state=${stateOf(expected)})",
                stateOf(expected) == "offline:IDLE")
            badge(expected).performClick()
            assertTrue("download never reached COMPLETE",
                waitState(expected, "COMPLETE", 120_000))
            val files = offlineFiles()
            assertTrue("no files under filesDir/offline: ${files.size}", files.isNotEmpty())

            // ── local-first read path ──
            var wifiCut = false
            try {
                if (isEmulator()) {
                    // Cut the radio: listing fails → record-names∩files
                    // fallback; pages load from filesDir.
                    shell("svc wifi disable"); wifiCut = true
                    Thread.sleep(2_000)
                } else {
                    // Physical device: adb rides the same wifi, so relaunch
                    // instead — File-backed page data is exercised either
                    // way (same scope → same offline store).
                    scenario.close()
                    scenario = ActivityScenario.launch(intent)
                    for (step in drill) {
                        assertTrue("drill $step after relaunch",
                            ensureTile(step, 45_000))
                        composeRule.onNodeWithTag("tile:$step").performClick()
                    }
                    assertTrue("tile $expected after relaunch",
                        ensureTile(expected, 45_000))
                }
                composeRule.onNodeWithTag("tile:$expected").performClick()
                assertTrue("reader page counter never appeared",
                    waitForCounter(30_000))
            } finally {
                if (wifiCut) { shell("svc wifi enable"); Thread.sleep(1_500) }
            }

            // Back to browse, then confirm-delete → IDLE → files gone.
            scenario.onActivity { it.onBackPressedDispatcher.onBackPressed() }
            assertTrue("badge not COMPLETE after reader back",
                waitState(expected, "COMPLETE", 15_000))
            badge(expected).performClick()                 // arm confirm
            assertTrue("confirm-delete did not arm",
                waitState(expected, "CONFIRM_DELETE", 6_000))
            badge(expected).performClick()                 // delete
            assertTrue("delete did not settle to IDLE",
                waitState(expected, "IDLE", 20_000))
            assertTrue("offline files remain after delete",
                offlineFiles().isEmpty())
        } finally {
            scenario.close()
        }
    }
}
