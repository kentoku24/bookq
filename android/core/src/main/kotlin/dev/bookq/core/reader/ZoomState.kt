package dev.bookq.core.reader

import kotlin.math.abs

/**
 * Absolute-zoom state machine — viewer/zoom.js port. The transform lives
 * in CONTAINER-LOCAL px (origin top-left), applied to the whole spread
 * layer: `translate(tx,ty) scale(zoom)`.
 *
 * Bands (absolute target = startZoom × ratio):
 *  - >1: zoomed, midpoint anchored, pan-clamped even mid-pinch.
 *  - 0.5..1: shrink — scale follows fingers, center drifts to the
 *    overview cell target.
 *  - ≤0.5: fire overview once; re-arms above 0.7 while fingers stay down.
 * Release: >1.15 stays zoomed, [1,1.15] snaps to 1, <1 bounces to 1.
 */
object ZoomK {
    const val MIN = 0.35f
    const val MAX = 4f
    const val FIRE = 0.5f
    const val REARM = 0.7f
    const val SNAP = 1.15f
    const val BOUNCE_MS = 190
    const val SLIDE_MS = 300
    const val EDGE_PX = 80f
    const val RUBBER = 0.35f
    const val ARM_PX = 10f
    const val PALM_EDGE = 20f
    const val PAN_DECIDE_PX = 8f
    /** Android convention (no web analog) — double-tap zooms in at the
     *  tapped point. */
    const val DOUBLE_TAP = 2.5f
    const val DOUBLE_TAP_MS = 300L
    const val DOUBLE_TAP_PX = 48f
}

private fun clamp(v: Float, lo: Float, hi: Float) =
    if (v < lo) lo else if (v > hi) hi else v

/** The spread's resting rect in layer-local px + container size — the
 *  Android equivalent of zoom.js `measureBox` (the layer is inset:0 in a
 *  full-viewport container, so local == viewport coords). */
data class ZoomBox(
    val x: Float, val y: Float,
    val w: Float, val h: Float,
    val viewW: Float, val viewH: Float,
) {
    /** Centered box for a fitted spread — our layout always centers the
     *  spread row, so this is the whole geometry. */
    companion object {
        fun centered(fitW: Float, fitH: Float, viewW: Float, viewH: Float) =
            ZoomBox((viewW - fitW) / 2f, (viewH - fitH) / 2f, fitW, fitH, viewW, viewH)
    }
}

sealed interface ZoomEvent {
    /** Apply (zoom, tx, ty). */
    data class Apply(val s: Float, val x: Float, val y: Float) : ZoomEvent
    /** Target dipped ≤ FIRE — open the overview (once per gesture). */
    data object FireOverview : ZoomEvent
}

enum class ZoomEnd { PERSIST, SNAP_TO_ONE, BOUNCE_TO_ONE }

class ZoomState {
    var zoom = 1f; private set
    var tx = 0f; private set
    var ty = 0f; private set

    var box = ZoomBox(0f, 0f, 0f, 0f, 0f, 0f)
        set(b) { field = b }

    // pinch bookkeeping
    private var startZoom = 1f
    private var startTx = 0f
    private var startTy = 0f
    private var mid0x = 0f
    private var mid0y = 0f
    private var fired = false

    // pan bookkeeping
    private var panTx0 = 0f
    private var panTy0 = 0f
    private var noRoom = 0
    var panArmed = false; private set
    var panArmedDir = 0; private set
    var panHorizontal = false; private set
    var panDecided = false; private set

    val isZoomed get() = zoom > 1f

    fun apply(s: Float, x: Float, y: Float) {
        zoom = s; tx = x; ty = y
    }

    fun reset() { apply(1f, 0f, 0f); panReset() }

    /** `clampPan` — keep the scaled spread covering the viewport; a
     *  narrower axis centers instead. */
    fun clampPan(nx: Float, ny: Float, s: Float = zoom): FloatArray {
        val b = box
        val sw = b.w * s
        val sh = b.h * s
        val x: Float
        val y: Float
        if (sw <= b.viewW) x = (b.viewW - sw) / 2f - b.x * s
        else x = clamp(nx, b.viewW - (b.x * s + sw), -b.x * s)
        if (sh <= b.viewH) y = (b.viewH - sh) / 2f - b.y * s
        else y = clamp(ny, b.viewH - (b.y * s + sh), -b.y * s)
        return floatArrayOf(x, y)
    }

    // ── pinch ──

    fun pinchStart(mid0x: Float, mid0y: Float) {
        startZoom = zoom; startTx = tx; startTy = ty
        this.mid0x = mid0x; this.mid0y = mid0y
        fired = false
        panReset()
    }

    fun pinchMove(ratio: Float, midX: Float, midY: Float,
                  driftX: Float? = null, driftY: Float? = null): ZoomEvent {
        val target = clamp(startZoom * ratio, ZoomK.MIN, ZoomK.MAX)
        if (!fired && target <= ZoomK.FIRE) {
            fired = true
            return ZoomEvent.FireOverview
        } else if (fired && target > ZoomK.REARM) {
            fired = false
        }
        if (target >= 1f) {
            // midpoint anchor — the layer point under mid0 stays under mid
            val k = target / startZoom
            val nx = midX - k * (mid0x - startTx)
            val ny = midY - k * (mid0y - startTy)
            val c = clampPan(nx, ny, target)
            apply(target, c[0], c[1])
        } else {
            // shrink band — scale follows, center drifts toward the
            // overview cell. p goes 0→1 as target goes 1→0.5.
            val W = box.viewW
            val H = box.viewH
            val c0x = W / 2f
            val c0y = H / 2f
            val p = clamp((1f - target) / 0.5f, 0f, 1f)
            val cx = if (driftX != null) c0x + p * (driftX - c0x) else c0x
            val cy = if (driftY != null) c0y + p * (driftY - c0y) else c0y
            apply(target, cx - target * W / 2f, cy - target * H / 2f)
        }
        return ZoomEvent.Apply(zoom, tx, ty)
    }

    /** Release verdict — does NOT apply the target: the caller animates
     *  from the current transform, then calls [apply] (a pre-applied snap
     *  would make the slide a no-op jump). */
    fun pinchEnd(): ZoomEnd {
        fired = false
        return when {
            zoom > ZoomK.SNAP -> { panSeed(); ZoomEnd.PERSIST }
            zoom >= 1f -> { panSeed(); ZoomEnd.SNAP_TO_ONE }
            else -> { panSeed(); ZoomEnd.BOUNCE_TO_ONE }
        }
    }

    /** 2→1 handoff — the still-down finger becomes a pan finger. */
    private fun panSeed() {
        if (zoom > 1f) panStart() else panReset()
    }

    fun panReset() {
        noRoom = 0; panArmed = false; panArmedDir = 0
        panHorizontal = false; panDecided = false
    }

    // ── 1-finger pan while zoomed ──

    /** -1: left edge at viewport left → drag right turns NEXT.
     *  +1: right edge at viewport right → drag left turns PREVIOUS.
     *   2: centered axis → both directions count. 0: room both ways. */
    fun noRoomDirAtStart(): Int {
        val b = box
        val sw = b.w * zoom
        if (sw <= b.viewW) return 2
        val maxX = -b.x * zoom
        val minX = b.viewW - (b.x * zoom + sw)
        val eps = 0.5f
        if (tx >= maxX - eps) return -1
        if (tx <= minX + eps) return 1
        return 0
    }

    fun panStart() {
        panTx0 = tx; panTy0 = ty
        noRoom = noRoomDirAtStart()
        panArmed = false; panArmedDir = 0
        panDecided = false; panHorizontal = false
    }

    /** Returns (x, y, decidedFlag). Direction decides at PAN_DECIDE_PX. */
    fun panMove(dx: Float, dy: Float): FloatArray {
        if (!panDecided) {
            if (abs(dx) < ZoomK.PAN_DECIDE_PX && abs(dy) < ZoomK.PAN_DECIDE_PX)
                return floatArrayOf(tx, ty, 0f)
            panDecided = true
            panHorizontal = abs(dx) > abs(dy)
        }
        val c = clampPan(panTx0 + dx, panTy0 + dy)
        var cx = c[0]
        val edgeDir = if (panHorizontal &&
            (noRoom == 2 || (noRoom == -1 && dx > 0) || (noRoom == 1 && dx < 0))) {
            if (dx > 0) 1 else -1
        } else 0
        if (edgeDir != 0) {
            cx += (if (dx > 0) 1f else -1f) * abs(dx) * ZoomK.RUBBER
            if (abs(dx) >= ZoomK.EDGE_PX) {
                panArmed = true; panArmedDir = edgeDir
            }
        }
        // Disarm OUTSIDE the edge branch — reversing far enough cancels.
        if (edgeDir == 0 || abs(dx) < ZoomK.EDGE_PX) panArmed = false
        apply(zoom, cx, c[1])
        return floatArrayOf(cx, c[1], 1f)
    }

    /** Release — returns the verdict; caller turns or settles. */
    fun panEnd(): Pair<Boolean, Int> =
        (panArmed && panArmedDir != 0) to panArmedDir

    /** Settle after a turn or a plain pan — re-clamp the band. */
    fun panSettle() {
        val c = clampPan(tx, ty)
        apply(zoom, c[0], c[1])
    }

    // ── double-tap ──

    /** Zoom-in target for a double-tap at (px, py) in container coords —
     *  the tapped layer point stays fixed (same anchor math as pinch),
     *  pan-clamped like a settle. Caller animates then [apply]s. */
    fun doubleTapTarget(px: Float, py: Float): FloatArray {
        val t = ZoomK.DOUBLE_TAP
        val k = t / zoom
        val c = clampPan(px - k * (px - tx), py - k * (py - ty), t)
        return floatArrayOf(t, c[0], c[1])
    }
}
