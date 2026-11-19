@file:OptIn(androidx.benchmark.macro.ExperimentalMetricApi::class)

package dev.bookq.reader.baselineprofile

import androidx.benchmark.macro.FrameTimingMetric
import androidx.benchmark.macro.StartupMode
import androidx.benchmark.macro.junit4.MacrobenchmarkRule
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.uiautomator.By
import androidx.test.uiautomator.Direction
import androidx.test.uiautomator.Until
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/** Gate: tile-grid fling on a large tree — per-test thresholds live in
 *  perf-gate.sh's GATES table (issue #302 §5). */
@RunWith(AndroidJUnit4::class)
class BrowseBenchmark {

    @get:Rule
    val benchmarkRule = MacrobenchmarkRule()

    @Test
    fun gridFling() = benchmarkRule.measureRepeated(
        packageName = PACKAGE_NAME,
        metrics = listOf(FrameTimingMetric()),
        compilationMode = compilationMode(),
        startupMode = StartupMode.HOT,
        iterations = 10,
        setupBlock = {
            val url = serverUrl()
            launchToBrowse(url)
            // Resume lands wherever the task left off — back out of the
            // reader, then drill one level so the grid is populated.
            if (inReader()) device.pressBack()
            if (device.wait(Until.hasObject(
                    By.res("browseGrid").pkg(PACKAGE_NAME)),
                    30_000) != true) {
                error("browse grid never appeared")
            }
            // Root can be a single folder — one free drill inside setup
            // so the measured level has a large tile list. A leaf tile
            // opens the reader instead — back out so the fling still
            // has a grid to scroll.
            device.findObjects(By.res(java.util.regex.Pattern
                .compile("tile:.*")).pkg(PACKAGE_NAME))
                .firstOrNull()?.click()
            device.waitForIdle(1_200)
            if (inReader()) device.pressBack()
        },
    ) {
        // Alternate direction — repeated DOWN flings hit the end of the
        // grid, where scrolling emits almost no frames to measure.
        val dir = if (gridDir > 0) Direction.DOWN else Direction.UP
        gridDir = -gridDir
        device.flingObject("browseGrid", dir, 3)
    }
}

private var gridDir = 1
