@file:OptIn(androidx.benchmark.macro.ExperimentalMetricApi::class)

package dev.bookq.reader.baselineprofile

import androidx.benchmark.macro.FrameTimingMetric
import androidx.benchmark.macro.StartupMode
import androidx.benchmark.macro.TraceSectionMetric
import androidx.benchmark.macro.junit4.MacrobenchmarkRule
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.uiautomator.By
import androidx.test.uiautomator.Direction
import androidx.test.uiautomator.Until
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * Reader gates (issue #302 §5), all measured WARM: the process stays
 * resident between iterations (StartupMode.HOT only pressHomes), so
 * Coil's memory/disk caches reflect a real reading session.
 *
 * - tapTurn: `bookq.pageTurn` sections, median < 60 ms
 * - swipeTurn / overviewFling: janky < 1 %, p99 frame < 16 ms
 * - readerOpen: `bookq.readerOpen` sections, < 400 ms cached
 *
 * Only the fling tests carry FrameTimingMetric: emulators expose no
 * SurfaceFlinger frame timeline, so FrameTimingMetric aborts with
 * "no expect/actual slices" there. Section metrics work on both,
 * keeping tapTurn/readerOpen verifiable on the emulator while the
 * frame gates stay physical-device-only.
 */
@RunWith(AndroidJUnit4::class)
class ReaderBenchmark {

    @get:Rule
    val benchmarkRule = MacrobenchmarkRule()

    @Test
    fun tapTurn() = benchmarkRule.measureRepeated(
        packageName = PACKAGE_NAME,
        metrics = listOf(TraceSectionMetric("bookq.pageTurn")),
        compilationMode = compilationMode(),
        startupMode = StartupMode.HOT,
        iterations = 10,
        setupBlock = {
            val url = serverUrl()
            launchToBrowse(url)
            reachReader()
            device.waitForIdle(1_000)
        },
    ) {
        // Alternate direction: single-direction taps strand the reader
        // on an edge spread where turn() is a no-op and the section
        // metric collects zero slices.
        if (tapDir > 0) device.tapForward() else device.tapBack()
        tapDir = -tapDir
        device.waitForIdle(400)
    }

    @Test
    fun swipeTurn() = benchmarkRule.measureRepeated(
        packageName = PACKAGE_NAME,
        metrics = listOf(FrameTimingMetric()),
        compilationMode = compilationMode(),
        startupMode = StartupMode.HOT,
        iterations = 10,
        setupBlock = {
            val url = serverUrl()
            launchToBrowse(url)
            reachReader()
            device.waitForIdle(1_000)
        },
    ) {
        if (swipeDir > 0) device.flingForward() else device.flingBack()
        swipeDir = -swipeDir
    }

    @Test
    fun overviewFling() = benchmarkRule.measureRepeated(
        packageName = PACKAGE_NAME,
        metrics = listOf(FrameTimingMetric()),
        compilationMode = compilationMode(),
        startupMode = StartupMode.HOT,
        iterations = 10,
        setupBlock = {
            val url = serverUrl()
            launchToBrowse(url)
            reachReader()
            openOverview()
            device.waitForIdle(800)
        },
    ) {
        val dir = if (flingDir > 0) Direction.DOWN else Direction.UP
        flingDir = -flingDir
        device.flingObject("overviewGrid", dir, 3)
    }

    @Test
    fun readerOpen() = benchmarkRule.measureRepeated(
        packageName = PACKAGE_NAME,
        metrics = listOf(TraceSectionMetric("bookq.readerOpen")),
        compilationMode = compilationMode(),
        startupMode = StartupMode.HOT,
        iterations = 10,
        setupBlock = {
            val url = serverUrl()
            launchToBrowse(url)
            // Resume may land inside the reader — back out to browse.
            if (inReader()) {
                device.pressBack()
                if (device.wait(Until.hasObject(
                        By.res("browseGrid").pkg(PACKAGE_NAME)),
                        15_000) != true) {
                    error("browse grid never appeared after back")
                }
            }
            device.waitForIdle(800)
        },
    ) {
        // The reader may still be open from the previous iteration (or
        // auto-resume may land inside it) — back out so every iteration
        // navigates fresh and emits the section. Section metrics only
        // time the slice, so the back-out clicks cost nothing here.
        if (inReader()) {
            device.pressBack()
            if (device.wait(Until.hasObject(
                    By.res("browseGrid").pkg(PACKAGE_NAME)),
                    10_000) != true) {
                error("browse grid never appeared after back")
            }
        }
        // Drill the first tile until the reader shows — folder tiles
        // navigate without emitting the section.
        repeat(6) {
            if (waitForReader(2_000)) return@repeat
            val first = device.findObjects(
                By.res(PATTERN_TILE).pkg(PACKAGE_NAME))
                .firstOrNull() ?: return@repeat
            first.click()
            device.waitForIdle(1_200)
        }
        if (!waitForReader(15_000)) error("reader never opened")
    }
}

private val PATTERN_TILE = java.util.regex.Pattern.compile("tile:.*")

// Alternate tap/fling direction per iteration — single-direction turns
// strand the reader on an edge spread where turn() is a no-op and the
// section/frame metrics degenerate.
private var tapDir = 1
private var swipeDir = 1
private var flingDir = 1
