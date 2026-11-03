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
        if (device.wait(Until.hasObject(
                By.res("browseGrid").pkg(PACKAGE_NAME)),
                30_000) != true) {
            error("browse grid never appeared")
        }
    }
}

internal fun compilationMode(): CompilationMode =
    when (InstrumentationRegistry.getArguments()
        .getString("compilation")) {
        "none" -> CompilationMode.None()
        "full" -> CompilationMode.Full()
        else -> CompilationMode.Partial(BaselineProfileMode.Require)
    }
