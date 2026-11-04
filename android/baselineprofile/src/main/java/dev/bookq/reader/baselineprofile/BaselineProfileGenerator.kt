package dev.bookq.reader.baselineprofile

import androidx.benchmark.macro.junit4.BaselineProfileRule
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.uiautomator.Direction
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * Baseline Profile + Startup Profile generation (issue #302).
 *
 * - [startup]: app launch → library grid — `includeInStartupProfile`
 *   marks it for the dex-layout Startup Profile (launch-critical only).
 * - [criticalJourneys]: browse fling, reader open, page turns —
 *   compiled into the Baseline Profile but kept OUT of the Startup
 *   Profile so dex layout only carries launch-path code.
 *
 * Run on CP08-J1 (API 35, non-rooted collection OK):
 *   ./gradlew :app:generateReleaseBaselineProfile
 */
@RunWith(AndroidJUnit4::class)
class BaselineProfileGenerator {

    @get:Rule
    val baselineProfileRule = BaselineProfileRule()

    @Test
    fun startup() = baselineProfileRule.collect(
        packageName = PACKAGE_NAME,
        includeInStartupProfile = true,
    ) {
        // launchToBrowse guarantees the browse surface — a bare
        // wait-for-grid lets a reader-landing (saved progress) or bad
        // URL silently collect a browse-less startup profile.
        launchToBrowse(serverUrl())
    }

    @Test
    fun criticalJourneys() = baselineProfileRule.collect(
        packageName = PACKAGE_NAME,
        includeInStartupProfile = false,
    ) {
        val url = serverUrl()
        pressHome()
        launchToBrowse(url)
        // Browse fling — cover decode + tile composition.
        device.flingObject("browseGrid", Direction.DOWN, 3)
        // Reader: open, two tap turns, one swipe turn, overview open.
        reachReader()
        repeat(2) { device.tapForward(); device.waitForIdle(500) }
        device.flingForward()
        openOverview()
        device.flingObject("overviewGrid", Direction.DOWN, 2)
    }
}
