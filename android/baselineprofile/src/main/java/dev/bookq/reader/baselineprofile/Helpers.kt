package dev.bookq.reader.baselineprofile

import android.content.Intent
import android.view.KeyEvent
import androidx.benchmark.macro.MacrobenchmarkScope
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.uiautomator.By
import androidx.test.uiautomator.Direction
import androidx.test.uiautomator.UiDevice
import androidx.test.uiautomator.Until
import java.util.regex.Pattern

// Release-shaped benchmark variant ships the BASE applicationId (only
// debug carries .debug) — the gates measure what users get.
const val PACKAGE_NAME = "dev.bookq.reader"

/** `N / M` counter text — the reader's reliable "open" signal (same
 *  locator ReaderPinchTest uses: it is chrome, not a tile). */
private val READER_COUNTER = Pattern.compile("\\d+ / \\d+")
private val TILE = Pattern.compile("tile:.*")

/** TEST_MODE dev environment URL passed by perf-gate.sh
 *  (`-e bookqServerUrl https://dev-XX…`). */
fun serverUrl(): String = InstrumentationRegistry.getArguments()
    .getString("bookqServerUrl")
    ?: error("-e bookqServerUrl <url> is required for the gate run")

fun launchIntent(url: String): Intent =
    Intent(Intent.ACTION_MAIN).apply {
        addCategory(Intent.CATEGORY_LAUNCHER)
        setClassName(PACKAGE_NAME, "dev.bookq.reader.MainActivity")
        addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or
            Intent.FLAG_ACTIVITY_CLEAR_TASK)
        putExtra("serverURL", url)
    }

/** Cold launch into TEST_MODE browse; waits for the grid. */
fun MacrobenchmarkScope.launchToBrowse(url: String) {
    startActivityAndWait(launchIntent(url))
    device.wait(Until.findObject(By.res("browseGrid")), 30_000)
        ?: error("browse grid never appeared")
    device.waitForIdle(1_500)
}

/** Deterministic reader entry: the seeded resume pill when present
 *  (CP08-J1 keeps saved progress), else drill the first tile until a
 *  leaf opens — same strategy as ReaderPinchTest. */
fun MacrobenchmarkScope.reachReader() {
    device.wait(Until.findObject(By.res("resumePill")), 8_000)
        ?.click()
    if (waitForReader(20_000)) return
    repeat(8) {
        if (waitForReader(3_000)) return
        device.findObjects(By.res(TILE))
            .firstOrNull()?.click() ?: return
        device.waitForIdle(1_500)
    }
    if (!waitForReader(30_000)) error("reader counter never appeared")
}

fun MacrobenchmarkScope.waitForReader(ms: Long): Boolean =
    device.wait(Until.hasObject(By.text(READER_COUNTER)), ms) != null

fun MacrobenchmarkScope.inReader(): Boolean =
    device.hasObject(By.text(READER_COUNTER))

fun UiDevice.tapForward() = click(
    displayWidth * 15 / 100, displayHeight / 2)

fun UiDevice.tapBack() = click(
    displayWidth * 85 / 100, displayHeight / 2)

/** RTL forward fling: a fast right→left swipe through the spread. */
fun UiDevice.flingForward() {
    swipe(
        displayWidth * 80 / 100, displayHeight / 2,
        displayWidth * 15 / 100, displayHeight / 2, 24)
    waitForIdle(600)
}

fun MacrobenchmarkScope.openOverview() {
    device.pressKeyCode(KeyEvent.KEYCODE_G)
    device.wait(Until.findObject(By.res("overviewGrid")), 10_000)
        ?: error("overview grid never opened")
}

fun UiDevice.flingObject(res: String, direction: Direction, times: Int) {
    // wait→object: UiObject2 re-queries on each action, so a fling fired
    // on the waited handle survives recomposition between iterations.
    val target = wait(Until.findObject(By.res(res)), 15_000)
        ?: error("$res never appeared for fling")
    target.setGestureMarginPercentage(0.15f)
    repeat(times) {
        target.fling(direction)
        waitForIdle(800)
    }
}
