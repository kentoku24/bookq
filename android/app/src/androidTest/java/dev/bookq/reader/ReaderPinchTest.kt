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
import org.junit.After
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
    // Registered lifecycle callbacks — torn down even when a step
    // fails, or a stale callback keeps dispatching into dead refs.
    private var callbacks: Application.ActivityLifecycleCallbacks? = null

    @After fun tearDown() {
        val cb = callbacks ?: return
        val instr = InstrumentationRegistry.getInstrumentation()
        val app = instr.targetContext.applicationContext as Application
        instr.runOnMainSync { app.unregisterActivityLifecycleCallbacks(cb) }
        callbacks = null
    }

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
        val cbs = object : Application.ActivityLifecycleCallbacks {
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
        }
        callbacks = cbs
        instr.runOnMainSync { app.registerActivityLifecycleCallbacks(cbs) }

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

        // ── 0.5. sanity: a center tap toggles the chrome — proves
        //     dispatched taps reach the zone layer no matter which page
        //     we landed on (edge-zone taps can't be verified at chapter
        //     boundaries: a turn may legitimately be a no-op). ──
        tap(instr, decor, cx, cy)
        assertTrue("center tap did not hide the reader chrome",
            waitUntil({ !anyText(readerCounter) }, 3_000))
        tap(instr, decor, cx, cy)
        assertTrue("second tap did not restore the reader chrome",
            waitUntil({ anyText(readerCounter) }, 3_000))

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
        //     Size-relative (cx*0.6): a fixed -200px goes off-screen on
        //     the CI emulator's small display; this stays in-bounds and
        //     inside the pan room, so it can't arm the edge-turn band. ──
        val swipeDx = minOf(200f, cx * 0.6f)
        swipe(instr, decor, cx, cx - swipeDx, cy)
        assertTrue(
            "zoomed swipe did not pan — the drag left the zoom layer",
            waitUntil({
                (zoomScale() ?: 0f) > 1.05f && abs(panX() ?: 0f) > 30f
            }, 2_500))

        // ── 2.5. edge-turn: a pan that STARTS at a clamped edge and
        //     keeps going past EDGE_PX arms a page turn (web
        //     commitSwipeTurn band). First drag rides the clamp out to
        //     an edge; the second drags on from there → turn. A book
        //     with a single page has no turn in either direction — the
        //     CI fixture is exactly that, so skip when total <= 1. ──
        val preEdge = counterText()
        val edgeTotal = preEdge?.substringAfterLast('/')
            ?.trim()?.toIntOrNull() ?: 0
        if (edgeTotal > 1) {
            val edgeX0 = 60f
            val edgeX1 = d.displayWidth - 60f
            var turned = false
            for (dir in intArrayOf(1, -1)) {
                val (x0, x1) =
                    if (dir > 0) edgeX0 to edgeX1 else edgeX1 to edgeX0
                swipe(instr, decor, x0, x1, cy, steps = 14)
                Thread.sleep(300)
                swipe(instr, decor, x0, x1, cy, steps = 14)
                turned = waitUntil(
                    { counterText() != preEdge || (zoomScale() ?: 9f) <= 1f },
                    5_000)
                if (turned) break
            }
            assertTrue("edge-drag turn never moved the page " +
                "(counter=$preEdge → ${counterText()}, zoom=${zoomScale()})",
                turned)
            // The settle reset the layer to 1x — re-zoom for the
            // reset-tap check below.
            var reZoomed = false
            for (attempt in 0..1) {
                doubleTap(instr, decor, cx, cy)
                if (waitUntil(
                        { (zoomScale() ?: 1f) > 1.05f }, 2_500)) {
                    reZoomed = true; break
                }
            }
            assertTrue("re-zoom after edge-turn failed", reZoomed)
            Thread.sleep(400)
        }

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
        swipe(instr, decor, cx, cx - swipeDx, cy)
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

        // ── 4. pinch-IN — start span capped on-screen; the end span is
        //     ratio-relative (0.25x — always crosses the <=0.5 FIRE
        //     band). A fixed-px end span can exceed startHalf on small
        //     displays: the "pinch-in" becomes a pinch-out AND the
        //     fingers travel under ARM_PX, so it never even arms.
        //     Retry once — this ROM intermittently drops pointer events. ──
        val pinchInStart = minOf(450f, cx * 0.83f)
        for (attempt in 0..1) {
            pinch(instr, decor, cx, cy,
                startHalf = pinchInStart,
                endHalf = pinchInStart * 0.25f, steps = 14)
            if (waitForTag("overviewGrid", 6_000)) break
        }

        // ── 5. overview grid opened ──
        if (!waitForTag("overviewGrid", 8_000)) {
            d.executeShellCommand("screencap -p /sdcard/m4-noov.png")
            val dbg = d.executeShellCommand("logcat -d -t 300 -s ZoomDbg:D")
            error("pinch-in did not fire the overview grid — " +
                "scale=${zoomScale()} dbg=$dbg texts=" +
                nodes().mapNotNull { textOf(it) }.take(40))
        }

        // ── 5.5. cell tap → exits to that cell's page: the .pg badge
        //     carries the chapter-local page number the counter must
        //     then show. ──
        val cellNode = nodes().lastOrNull {
            tagOf(it)?.startsWith("ovcell:") == true
        } ?: run {
            d.executeShellCommand("screencap -p /sdcard/m4-nocell.png")
            error("no overview cell tagged")
        }
        val badge = flatten(cellNode).mapNotNull { textOf(it) }
            .firstOrNull { t -> t.isNotEmpty() && t.all { c -> c.isDigit() } }
        tapTag(tagOf(cellNode)!!)
        assertTrue("cell tap did not exit to the reader",
            waitUntil({
                nodes().none { tagOf(it) == "overviewGrid" } &&
                    anyText(readerCounter)
            }, 8_000))
        if (badge != null) {
            assertTrue(
                "cell tap landed on the wrong page " +
                    "(badge=$badge counter=${counterText()})",
                waitUntil({
                    counterText()?.trim()?.startsWith("$badge ") == true
                }, 5_000))
        }

        // ── 5.7. re-open, then pinch-OUT on the grid → exits at the
        //     cell under the finger midpoint (elementFromPoint). ──
        for (attempt in 0..1) {
            pinch(instr, decor, cx, cy,
                startHalf = pinchInStart,
                endHalf = pinchInStart * 0.25f, steps = 14)
            if (waitForTag("overviewGrid", 6_000)) break
        }
        assertTrue("overview did not re-open",
            waitForTag("overviewGrid", 8_000))
        // The cell under the pinch midpoint is the expected landing.
        val midCell = nodes().firstOrNull {
            tagOf(it)?.startsWith("ovcell:") == true &&
                it.boundsInWindow.let { b ->
                    cx >= b.left && cx <= b.right &&
                        cy >= b.top && cy <= b.bottom
                }
        }
        val midBadge = midCell?.let { n ->
            flatten(n).mapNotNull { textOf(it) }
                .firstOrNull { t ->
                    t.isNotEmpty() && t.all { c -> c.isDigit() }
                }
        }
        var pinchedOut = false
        for (attempt in 0..1) {
            pinch(instr, decor, cx, cy,
                startHalf = 150f,
                endHalf = minOf(cx * 0.85f, cx - 30f), steps = 12)
            pinchedOut = waitUntil({
                nodes().none { tagOf(it) == "overviewGrid" } &&
                    anyText(readerCounter)
            }, 6_000)
            if (pinchedOut) break
        }
        if (!pinchedOut) {
            d.executeShellCommand("screencap -p /sdcard/m4-nopinchout.png")
            val dbg = d.executeShellCommand("logcat -d -t 300 -s ZoomDbg:D")
            error("overview pinch-out did not exit — " +
                "gridOpen=${nodes().any { tagOf(it) == "overviewGrid" }} " +
                "counter=${counterText()} dbg=$dbg " +
                "tags=${nodes().mapNotNull { tagOf(it) }.take(20)}")
        }
        if (midBadge != null) {
            assertTrue(
                "pinch-out landed on a different cell " +
                    "(badge=$midBadge counter=${counterText()})",
                waitUntil({
                    counterText()?.trim()
                        ?.startsWith("$midBadge ") == true
                }, 5_000))
        }

        // ── 6. re-open + Back → grid closes, reader restored.
        //     In-process back — OS-injected pressBack goes to whatever
        //     has window focus, and Tailscale's zombie window keeps
        //     stealing it on this device. The tag-gone check matters:
        //     the reader chrome stays composed underneath the overlay,
        //     so its counter alone can't prove the overlay closed. ──
        for (attempt in 0..1) {
            pinch(instr, decor, cx, cy,
                startHalf = pinchInStart,
                endHalf = pinchInStart * 0.25f, steps = 14)
            if (waitForTag("overviewGrid", 6_000)) break
        }
        assertTrue("overview did not re-open for the Back check",
            waitForTag("overviewGrid", 8_000))
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
