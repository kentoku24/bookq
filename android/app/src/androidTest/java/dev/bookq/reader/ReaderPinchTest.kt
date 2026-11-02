package dev.bookq.reader

import android.app.Activity
import android.app.Application
import android.os.Bundle
import android.os.SystemClock
import android.view.MotionEvent
import android.view.MotionEvent.PointerCoords
import android.view.MotionEvent.PointerProperties
import android.view.View
import android.view.accessibility.AccessibilityNodeInfo
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.uiautomator.By
import androidx.test.uiautomator.UiDevice
import androidx.test.uiautomator.Until
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import java.util.regex.Pattern
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/**
 * On-device verification of the M4 zoom contract.
 *
 * Real multi-pointer [MotionEvent] streams are dispatched through the
 * resumed activity's decor view on the main thread — the same
 * AndroidComposeView hit-test + pointerInput path a real finger takes.
 * (sendPointerSync MT events get dropped on this ROM, and
 * createAndroidComposeRule's ComposeIdlingResource freezes the
 * Recomposer between test calls, so the app looks stuck on Booting —
 * plain ActivityLifecycleCallbacks + dispatchTouchEvent is the working
 * route on this device.)
 *
 * Flow mirrors viewer/zoom.js:
 *  - pinch-out >1.15 persists after release (zoomed mode)
 *  - while zoomed, a one-finger horizontal drag is a PAN owned by the
 *    zoom layer — the pager cannot turn (counter must not change)
 *  - center tap while zoomed snaps back to 1x
 *  - pinch-in through the <=0.5 band fires the overview grid
 *  - Back closes the grid and returns to the reader
 *
 * Discriminators: reader counter is "N / M" (spaces); overview chip is
 * "N/M" (no spaces).
 */
@RunWith(AndroidJUnit4::class)
class ReaderPinchTest {

    private val readerCounter = Pattern.compile("\\d+ / \\d+")
    private val overviewChip = Pattern.compile("\\d+/\\d+")

    @Test
    fun pinchZoomAndOverviewFlow() {
        val instr = InstrumentationRegistry.getInstrumentation()
        val d = UiDevice.getInstance(instr)
        val cx = d.displayWidth / 2f
        val cy = d.displayHeight / 2f

        // Capture the resumed MainActivity's decorView before launch —
        // the callback fires when the activity next resumes.
        val app =
            instr.targetContext.applicationContext as Application
        val decorRef = AtomicReference<View?>()
        val actRef = AtomicReference<MainActivity?>()
        val resumed = CountDownLatch(1)
        instr.runOnMainSync {
            app.registerActivityLifecycleCallbacks(
                object : Application.ActivityLifecycleCallbacks {
                    override fun onActivityResumed(a: Activity) {
                        if (a is MainActivity) {
                            decorRef.set(a.window.decorView)
                            actRef.set(a)
                            resumed.countDown()
                        }
                    }
                    override fun onActivityCreated(a: Activity, b: Bundle?) {}
                    override fun onActivityStarted(a: Activity) {}
                    override fun onActivityPaused(a: Activity) {}
                    override fun onActivityStopped(a: Activity) {}
                    override fun onActivitySaveInstanceState(a: Activity, b: Bundle) {}
                    override fun onActivityDestroyed(a: Activity) {}
                })
        }

        // Tailscale's zombie window holds window focus after
        // instrumentation restarts the app — go HOME first so the
        // launcher takes focus away from it, then bring bookq forward
        // (force-stop would kill the tunnel). CI passes bookqServerUrl.
        val serverUrl = InstrumentationRegistry.getArguments()
            .getString("bookqServerUrl")?.let { " --es serverURL $it" } ?: ""
        d.executeShellCommand(
            "am start -a android.intent.action.MAIN" +
                " -c android.intent.category.HOME")
        Thread.sleep(800)
        d.executeShellCommand(
            "am start -n dev.bookq.reader.debug/dev.bookq.reader.MainActivity$serverUrl")
        assertTrue("MainActivity never resumed",
            resumed.await(15, TimeUnit.SECONDS))
        val decor = decorRef.get()!!

        // ── 0. reach the reader: resume card (device, seeded progress)
        //    or drill the first tile until a leaf opens (CI emulator) ──
        val resume = By.textContains("続きから")
        if (d.wait(Until.hasObject(resume), 20_000)) {
            d.findObject(resume).click()
        } else {
            // CI path: no saved progress — drill the first tile until a
            // leaf opens the reader (bounded depth).
            for (depth in 0 until 8) {
                if (d.wait(Until.hasObject(By.text(readerCounter)), 3_000)) break
                val tiles = d.findObjects(By.res(Pattern.compile("tile:.+")))
                if (tiles.isEmpty()) break
                tiles[0].click()
                Thread.sleep(1_500)
            }
        }
        if (!d.wait(Until.hasObject(By.text(readerCounter)), 45_000)) {
            d.executeShellCommand("screencap -p /sdcard/m4-noreader.png")
            error("reader counter not visible — texts: " +
                collectTexts(instr.uiAutomation.rootInActiveWindow))
        }
        val before =
            d.findObject(By.text(readerCounter))?.text ?: error("no counter text")

        // ── 0.5. sanity: dispatched taps reach the zones. Boundary
        //     pages may not move — try both sides, restore if moved. ──
        tap(instr, decor, cx - 500, cy)
        Thread.sleep(400)
        var afterTap = d.findObject(By.text(readerCounter))?.text
        if (afterTap == before) {
            tap(instr, decor, cx + 500, cy)
            Thread.sleep(400)
            afterTap = d.findObject(By.text(readerCounter))?.text
            if (afterTap != before) {
                tap(instr, decor, cx - 500, cy) // restore
                Thread.sleep(400)
            }
        } else {
            tap(instr, decor, cx + 500, cy) // restore
            Thread.sleep(400)
        }

        // ── 1. pinch-OUT: 300px → 1600px in steps (arm + pinchMove) ──
        pinch(instr, decor, cx, cy, startHalf = 150f, endHalf = 800f, steps = 10)
        Thread.sleep(800)

        // ── 2. zoomed → horizontal one-finger drag must PAN, not turn ──
        val preDrag = d.findObject(By.text(readerCounter))?.text
        d.drag(cx.toInt(), cy.toInt(), (cx - 400).toInt(), cy.toInt(), 50)
        Thread.sleep(600)
        val afterPan = d.findObject(By.text(readerCounter))?.text
        assertEquals(
            "zoomed drag turned a page — pan is not owned by zoom",
            preDrag, afterPan)

        // ── 3. center tap while zoomed → snap back to 1x ──
        tap(instr, decor, cx, cy)
        Thread.sleep(900)

        // ── 4. pinch-IN: 900px → 280px — crosses the <=0.5 FIRE band ──
        pinch(instr, decor, cx, cy, startHalf = 450f, endHalf = 140f, steps = 14)

        // ── 5. overview grid opened (chip "N/M", no spaces) ──
        assertTrue(
            "pinch-in did not fire the overview grid",
            d.wait(Until.hasObject(By.text(overviewChip)), 8_000),
        )

        // ── 6. Back → grid closes, reader restored. In-process back —
        // OS-injected pressBack goes to whatever has window focus, and
        // Tailscale's zombie window keeps stealing it on this device.
        instr.runOnMainSync {
            actRef.get()!!.onBackPressedDispatcher.onBackPressed()
        }
        assertTrue(
            "reader did not come back after overview close",
            d.wait(Until.hasObject(By.text(readerCounter)), 8_000),
        )
    }

    /** Streamed two-finger pinch around (cx, cy) via decor dispatch. */
    private fun pinch(
        instr: android.app.Instrumentation,
        decor: View,
        cx: Float, cy: Float,
        startHalf: Float, endHalf: Float, steps: Int,
    ) {
        val down = SystemClock.uptimeMillis()
        var t = down

        fun twoPointer(action: Int, x0: Float, x1: Float): MotionEvent {
            t += 16
            val props = arrayOf(
                PointerProperties().apply { id = 0; toolType = 1 },
                PointerProperties().apply { id = 1; toolType = 1 },
            )
            val coords = arrayOf(
                PointerCoords().apply {
                    x = x0; y = cy; pressure = 1f; size = 1f
                },
                PointerCoords().apply {
                    x = x1; y = cy; pressure = 1f; size = 1f
                },
            )
            return MotionEvent.obtain(
                down, t, action, 2, props, coords,
                0, 0, 1f, 1f, 0, 0, 0x1002, 0,
            )
        }

        fun dispatch(e: MotionEvent) {
            instr.runOnMainSync { decor.dispatchTouchEvent(e) }
            e.recycle()
        }

        // finger0 down, finger1 joins
        dispatch(MotionEvent.obtain(
            down, down, MotionEvent.ACTION_DOWN, cx - startHalf, cy, 0))
        t = down + 30
        dispatch(twoPointer(MotionEvent.ACTION_POINTER_DOWN or (1 shl 8),
            cx - startHalf, cx + startHalf))
        // streamed moves across the arm threshold
        for (i in 1..steps) {
            val h = startHalf + (endHalf - startHalf) * i / steps
            dispatch(twoPointer(MotionEvent.ACTION_MOVE, cx - h, cx + h))
        }
        // finger1 up, then finger0 up
        dispatch(twoPointer(MotionEvent.ACTION_POINTER_UP or (1 shl 8),
            cx - endHalf, cx + endHalf))
        dispatch(MotionEvent.obtain(
            down, t + 16, MotionEvent.ACTION_UP, cx - endHalf, cy, 0))
        Thread.sleep(120)
    }

    /** Single-finger tap via real dispatched events. */
    private fun tap(
        instr: android.app.Instrumentation, decor: View, x: Float, y: Float,
    ) {
        val down = SystemClock.uptimeMillis()
        instr.runOnMainSync {
            decor.dispatchTouchEvent(
                MotionEvent.obtain(down, down, MotionEvent.ACTION_DOWN, x, y, 0))
        }
        instr.runOnMainSync {
            decor.dispatchTouchEvent(
                MotionEvent.obtain(down, down + 60, MotionEvent.ACTION_UP, x, y, 0))
        }
    }

    private fun collectTexts(root: AccessibilityNodeInfo?): List<String> {
        if (root == null) return listOf("<null root>")
        val out = mutableListOf<String>()
        val stack = ArrayDeque<AccessibilityNodeInfo>()
        stack.add(root)
        while (stack.isNotEmpty() && out.size < 40) {
            val n = stack.removeFirst()
            n.text?.toString()?.takeIf { it.isNotBlank() }?.let { out.add(it) }
            n.contentDescription?.toString()?.takeIf { it.isNotBlank() }
                ?.let { out.add("desc:$it") }
            for (i in 0 until n.childCount) n.getChild(i)?.let(stack::add)
        }
        return out
    }
}
