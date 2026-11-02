package dev.bookq.reader

import android.app.Activity
import android.app.Application
import android.os.Bundle
import android.os.SystemClock
import android.view.MotionEvent
import android.view.MotionEvent.PointerCoords
import android.view.MotionEvent.PointerProperties
import android.view.View
import android.view.ViewGroup
import androidx.compose.ui.semantics.SemanticsNode
import androidx.compose.ui.semantics.SemanticsOwner
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.semantics.SemanticsPropertyKey
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.uiautomator.UiDevice
import dev.bookq.reader.ui.reader.ReaderZoomPanX
import dev.bookq.reader.ui.reader.ReaderZoomScale
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import java.util.regex.Pattern
import kotlin.math.abs
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
 *  - while zoomed, a one-finger swipe is a PAN owned by the zoom
 *    layer (scale stays >1 and tx moves; a pager turn would reset
 *    the layer to 1x/0)
 *  - center tap while zoomed snaps back to 1x
 *  - pinch-in through the <=0.5 band fires the overview grid
 *  - Back closes the grid and returns to the reader
 *
 * Zoom state is read from the reader's own semantics
 * ([ReaderZoomScale]/[ReaderZoomPanX]) — UiDevice gestures can be
 * lost to a zombie window on this ROM, and a lost drag is
 * indistinguishable from a pan that "didn't turn".
 */
@RunWith(AndroidJUnit4::class)
class ReaderPinchTest {

    private val readerCounter = Pattern.compile("\\d+ / \\d+")

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
        // Semantics assertions — read the ACTIVITY's own Compose tree
        // in-process. UiDevice/rootInActiveWindow is unusable on this
        // ROM: Tailscale's zombie window keeps the a11y-active window
        // even while bookq is visibly on top, so every By.* lookup can
        // land on an empty tree while the UI is perfectly fine.
        fun nodes(): List<SemanticsNode> {
            val out = AtomicReference<List<SemanticsNode>>(emptyList())
            instr.runOnMainSync {
                val owner = semanticsOwnerOf(
                    actRef.get()!!.window.decorView) ?: return@runOnMainSync
                out.set(flatten(owner.rootSemanticsNode))
            }
            return out.get()
        }
        fun <T> conf(n: SemanticsNode, k: SemanticsPropertyKey<T>): T? =
            if (k in n.config) n.config[k] else null
        fun textOf(n: SemanticsNode): String? =
            conf(n, SemanticsProperties.Text)?.joinToString("") { it.text }
        fun tagOf(n: SemanticsNode): String? =
            conf(n, SemanticsProperties.TestTag)
        fun anyText(re: Pattern): Boolean =
            nodes().any { textOf(it)?.let { t -> re.matcher(t).find() } == true }
        fun waitUntil(cond: () -> Boolean, ms: Long): Boolean {
            val deadline = SystemClock.uptimeMillis() + ms
            do { if (cond()) return true; Thread.sleep(150) }
            while (SystemClock.uptimeMillis() < deadline)
            return false
        }
        fun waitForText(re: Pattern, ms: Long): Boolean =
            waitUntil({ anyText(re) }, ms)
        fun waitForTag(tag: String, ms: Long): Boolean =
            waitUntil({ nodes().any { tagOf(it) == tag } }, ms)
        fun counterText(): String? =
            nodes().mapNotNull { textOf(it) }
                .firstOrNull { readerCounter.matcher(it).find() }
        // Zoom state straight from the reader's semantics — no
        // inference from counters (a UiDevice drag that never reached
        // the app looks identical to a pan that "didn't turn").
        fun zoomScale(): Float? =
            nodes().mapNotNull { conf(it, ReaderZoomScale) }
                .firstOrNull()
        fun panX(): Float? =
            nodes().mapNotNull { conf(it, ReaderZoomPanX) }
                .firstOrNull()
        fun tapTag(tag: String, prefix: Boolean = false): Boolean {
            val n = nodes().firstOrNull {
                val t = tagOf(it)
                if (prefix) t?.startsWith(tag) == true else t == tag
            } ?: return false
            val b = n.boundsInWindow
            tap(instr, decor, b.center.x, b.center.y)
            return true
        }

        // ── 0. reach the reader: resume card (device, seeded progress)
        //    or drill the first tile until a leaf opens (CI emulator) ──
        if (waitForTag("resumePill", 20_000)) {
            tapTag("resumePill")
        } else {
            // CI path: no saved progress — drill the first tile until a
            // leaf opens the reader (bounded depth).
            for (depth in 0 until 8) {
                if (waitForText(readerCounter, 3_000)) break
                if (!tapTag("tile:", prefix = true)) break
                Thread.sleep(1_500)
            }
        }
        if (!waitForText(readerCounter, 45_000)) {
            d.executeShellCommand("screencap -p /sdcard/m4-noreader.png")
            error("reader counter not visible — texts: " +
                nodes().mapNotNull { textOf(it) }.take(40))
        }
        val before = counterText() ?: error("no counter text")

        // ── 0.5. sanity: dispatched taps reach the zones. Boundary
        //     pages may not move — try both sides, restore if moved.
        //     Edge taps sit 80px in from the screen edge so they land
        //     in the outer zones on any display width. ──
        val edgeL = 80f
        val edgeR = d.displayWidth - 80f
        tap(instr, decor, edgeL, cy)
        Thread.sleep(400)
        var afterTap = counterText()
        if (afterTap == before) {
            tap(instr, decor, edgeR, cy)
            Thread.sleep(400)
            afterTap = counterText()
            if (afterTap != before) {
                tap(instr, decor, edgeL, cy) // restore
                Thread.sleep(400)
            }
        } else {
            tap(instr, decor, edgeR, cy) // restore
            Thread.sleep(400)
        }

        // ── 1. pinch-OUT — verdict read straight from the layer:
        //     scale > 1 means it armed (no counter inference — a
        //     UiDevice drag lost to a zombie window looks identical to
        //     a pan that "didn't turn"). Half-span capped on-screen. ──
        var zoomed = false
        for (attempt in 0..1) {
            pinch(instr, decor, cx, cy,
                startHalf = minOf(150f, cx * 0.28f),
                endHalf = cx * 0.85f, steps = 10)
            if (waitUntil(
                    { (zoomScale() ?: 1f) > 1.05f }, 2_500)) {
                zoomed = true; break
            }
        }
        assertTrue("pinch-out did not arm the zoom layer", zoomed)

        // ── 2. zoomed swipe → the layer pans: scale stays >1 and tx
        //     moves (any pager turn would reset the layer to 1x/0).
        //     -200px stays inside the pan room at every scale used
        //     here, so it can't arm the edge-turn rubber band. ──
        swipe(instr, decor, cx, cx - 200f, cy)
        assertTrue(
            "zoomed swipe did not pan — the drag left the zoom layer",
            waitUntil({
                (zoomScale() ?: 0f) > 1.05f && abs(panX() ?: 0f) > 30f
            }, 2_500))

        // ── 3. center tap while zoomed → snaps back to 1x. Wait out
        //     the 400ms pan-decided tap suppression first, and wait
        //     for EXACTLY 1x — a tap at 1.01x mid-animation is still a
        //     "zoomed" tap (reset), which would eat the double-tap's
        //     first click and leave chrome toggled off. ──
        Thread.sleep(500)
        tap(instr, decor, cx, cy)
        assertTrue("zoomed center tap did not reset to 1x",
            waitUntil({ (zoomScale() ?: 9f) <= 1f }, 3_000))

        // ── 3.5. center double-tap → zooms in at the tap point
        //     (Android convention — ZoomK.DOUBLE_TAP). ──
        var dtZoomed = false
        for (attempt in 0..1) {
            doubleTap(instr, decor, cx, cy)
            if (waitUntil(
                    { (zoomScale() ?: 1f) > 1.05f }, 2_500)) {
                dtZoomed = true; break
            }
        }
        assertTrue("double-tap did not zoom", dtZoomed)
        // The slide-to-target animation still writes tx each frame —
        // let it finish or the pan below is overwritten mid-gesture.
        Thread.sleep(400)
        swipe(instr, decor, cx, cx - 200f, cy)
        assertTrue("double-tap zoom did not pan",
            waitUntil({
                (zoomScale() ?: 0f) > 1.05f && abs(panX() ?: 0f) > 30f
            }, 2_500))
        // Zoomed center tap → back to 1x for the pinch-in below
        // (past the pan-decided tap suppression window).
        Thread.sleep(500)
        tap(instr, decor, cx, cy)
        assertTrue("reset tap failed",
            waitUntil({ (zoomScale() ?: 9f) <= 1f }, 3_000))

        // ── 4. pinch-IN — start span capped on-screen; crosses the
        //     <=0.5 FIRE band at the ~0.28x end span. Retry once — this
        //     ROM intermittently drops pointer events, and a pinch that
        //     never arms is recoverable (fresh gesture re-seeds). ──
        for (attempt in 0..1) {
            pinch(instr, decor, cx, cy,
                startHalf = minOf(450f, cx * 0.83f),
                endHalf = 140f, steps = 14)
            if (waitForTag("overviewGrid", 6_000)) break
        }

        // ── 5. overview grid opened ──
        if (!waitForTag("overviewGrid", 8_000)) {
            d.executeShellCommand("screencap -p /sdcard/m4-noov.png")
            error("pinch-in did not fire the overview grid — texts: " +
                nodes().mapNotNull { textOf(it) }.take(40))
        }

        // ── 6. Back → grid closes, reader restored. In-process back —
        // OS-injected pressBack goes to whatever has window focus, and
        // Tailscale's zombie window keeps stealing it on this device.
        // The tag-gone check matters: the reader chrome stays composed
        // underneath the overlay, so its counter alone can't prove the
        // overlay actually closed.
        instr.runOnMainSync {
            actRef.get()!!.onBackPressedDispatcher.onBackPressed()
        }
        assertTrue("overview grid did not close on Back",
            waitUntil(
                { nodes().none { tagOf(it) == "overviewGrid" } }, 5_000))
        if (!waitForText(readerCounter, 8_000)) {
            d.executeShellCommand("screencap -p /sdcard/m4-noreader2.png")
            val ns = nodes()
            error("reader counter absent after overview close — " +
                "nodes=${ns.size} scale=${zoomScale()} " +
                "tags=${ns.mapNotNull { tagOf(it) }.take(20)} " +
                "texts=${ns.mapNotNull { textOf(it) }.take(40)}")
        }
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

    /** Single-finger fling via real dispatched events — real-time
     *  paced so the velocity tracker sees a genuine swipe. At 1x this
     *  would turn the pager; zoomed, the layer owns it as a pan. */
    private fun swipe(
        instr: android.app.Instrumentation, decor: View,
        x0: Float, x1: Float, y: Float, steps: Int = 10,
    ) {
        val down = SystemClock.uptimeMillis()
        var t = down
        instr.runOnMainSync {
            decor.dispatchTouchEvent(
                MotionEvent.obtain(down, down, MotionEvent.ACTION_DOWN, x0, y, 0))
        }
        for (i in 1..steps) {
            Thread.sleep(10)
            t = down + i * 10
            val x = x0 + (x1 - x0) * i / steps
            instr.runOnMainSync {
                decor.dispatchTouchEvent(
                    MotionEvent.obtain(down, t, MotionEvent.ACTION_MOVE, x, y, 0))
            }
        }
        instr.runOnMainSync {
            decor.dispatchTouchEvent(
                MotionEvent.obtain(down, t + 10, MotionEvent.ACTION_UP, x1, y, 0))
        }
    }

    /** Double-tap — all four events dispatched in one main-thread block
     *  so the inter-tap gap stays ~1ms even on a loaded emulator (the
     *  app-side detector reads real uptimeMillis, not eventTime). */
    private fun doubleTap(
        instr: android.app.Instrumentation, decor: View, x: Float, y: Float,
    ) {
        instr.runOnMainSync {
            val d0 = SystemClock.uptimeMillis()
            decor.dispatchTouchEvent(MotionEvent.obtain(
                d0, d0, MotionEvent.ACTION_DOWN, x, y, 0))
            decor.dispatchTouchEvent(MotionEvent.obtain(
                d0, d0 + 60, MotionEvent.ACTION_UP, x, y, 0))
            decor.dispatchTouchEvent(MotionEvent.obtain(
                d0, d0 + 90, MotionEvent.ACTION_DOWN, x, y, 0))
            decor.dispatchTouchEvent(MotionEvent.obtain(
                d0, d0 + 150, MotionEvent.ACTION_UP, x, y, 0))
        }
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

    /** The activity's SemanticsOwner — AndroidComposeView is internal,
     *  so reach its public getSemanticsOwner() by name. */
    private fun semanticsOwnerOf(v: View): SemanticsOwner? {
        if (v.javaClass.name == "androidx.compose.ui.platform.AndroidComposeView") {
            return v.javaClass.getMethod("getSemanticsOwner")
                .invoke(v) as? SemanticsOwner
        }
        if (v is ViewGroup) {
            for (i in 0 until v.childCount) {
                semanticsOwnerOf(v.getChildAt(i))?.let { return it }
            }
        }
        return null
    }

    private fun flatten(n: SemanticsNode): List<SemanticsNode> =
        listOf(n) + n.children.flatMap { flatten(it) }
}
