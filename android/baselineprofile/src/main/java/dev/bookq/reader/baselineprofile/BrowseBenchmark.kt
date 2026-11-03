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

/** Gate: tile-grid fling on a large tree — janky < 1 %, p99 < 16 ms
 *  (issue #302 §5). */
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
            device.wait(
                Until.hasObject(By.res("browseGrid")), 30_000)
                ?: error("browse grid never appeared")
            // Root can be a single folder — one free drill inside setup
            // so the measured level has a large tile list.
            device.findObjects(By.res(java.util.regex.Pattern
                .compile("tile:.*"))).firstOrNull()?.click()
            device.waitForIdle(1_200)
        },
    ) {
        device.flingObject("browseGrid", Direction.DOWN, 3)
    }
}
