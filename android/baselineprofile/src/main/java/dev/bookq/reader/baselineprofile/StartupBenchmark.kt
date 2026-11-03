@file:OptIn(androidx.benchmark.macro.ExperimentalMetricApi::class)

package dev.bookq.reader.baselineprofile

import androidx.benchmark.macro.BaselineProfileMode
import androidx.benchmark.macro.CompilationMode
import androidx.benchmark.macro.StartupMode
import androidx.benchmark.macro.StartupTimingMetric
import androidx.benchmark.macro.junit4.MacrobenchmarkRule
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.uiautomator.By
import androidx.test.uiautomator.Until
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/** Gate: cold start → library displayed < 1.5 s (issue #302 §5,
 *  `reportFullyDrawn` fires when the first grid content composes). */
@RunWith(AndroidJUnit4::class)
class StartupBenchmark {

    @get:Rule
    val benchmarkRule = MacrobenchmarkRule()

    @Test
    fun coldStartup() = benchmarkRule.measureRepeated(
        packageName = PACKAGE_NAME,
        metrics = listOf(StartupTimingMetric()),
        compilationMode = compilationMode(),
        startupMode = StartupMode.COLD,
        iterations = 10,
        setupBlock = { pressHome() },
    ) {
        startActivityAndWait(launchIntent(serverUrl()))
        // Cold start can auto-resume straight into the reader (saved
        // progress on the device) — TTFD already fired on whichever
        // surface drew first. Only WAIT here: a pressBack during the
        // measured window adds navigation frames and can orphan the
        // reportFullyDrawn frame's RT slice ("No RT frame slice
        // associated with UI thread frame").
        val deadline = android.os.SystemClock.uptimeMillis() + 30_000
        var surfaced = false
        while (android.os.SystemClock.uptimeMillis() < deadline) {
            if (device.hasObject(
                    By.res("browseGrid").pkg(PACKAGE_NAME)) ||
                inReader()) { surfaced = true; break }
            android.os.SystemClock.sleep(300)
        }
        if (!surfaced) error("no app surface appeared within 30s")
    }
}

internal fun compilationMode(): CompilationMode =
    when (InstrumentationRegistry.getArguments()
        .getString("compilation")) {
        "none" -> CompilationMode.None()
        "full" -> CompilationMode.Full()
        else -> CompilationMode.Partial(BaselineProfileMode.Require)
    }
