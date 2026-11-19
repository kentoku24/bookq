package dev.bookq.reader.ui.reader

import android.util.Log
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.CubicBezierEasing
import androidx.compose.animation.core.tween
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.input.pointer.PointerEventPass
import androidx.compose.ui.input.pointer.PointerId
import androidx.compose.ui.input.pointer.changedToDownIgnoreConsumed
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.input.pointer.positionChange
import dev.bookq.core.reader.ZoomEnd
import dev.bookq.core.reader.ZoomEvent
import dev.bookq.core.reader.ZoomK
import dev.bookq.core.reader.ZoomState
import dev.bookq.reader.BuildConfig
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch

/** Plain mutable reference — lets the gesture layer and its caller share
 *  one cancellable animation job without dragging Compose state into
 *  [ZoomState]-level plumbing. */
class MutableRef<T>(var value: T)

private const val ZTAG = "ZoomDbg"
private inline fun zlog(msg: () -> String) {
    if (BuildConfig.DEBUG) Log.d(ZTAG, msg())
}

/**
 * Compose wiring for [ZoomState] — the touch side of viewer/zoom.js +
 * viewer/gesture.js:
 *  - PALM_EDGE: a finger landing within 20 px of the screen edge is a palm
 *    until it travels ARM_PX; palms never count toward pinch or pan.
 *  - Pinch arms when TWO active fingers have both moved ARM_PX from where
 *    they LANDED (dist0/mid0 from touchdown — arming travel is not
 *    charged to the user).
 *  - While zoomed, a lone finger pans with clamp + rubber edge-turn.
 *  - 2→1 handoff: the remaining finger becomes a pan finger.
 * Runs on the Initial pass so pinch/pan consumption pre-empts the pager's
 * drag claim (the pager sees only un-consumed residuals).
 */
fun Modifier.zoomGestures(
    zs: ZoomState,
    enabled: () -> Boolean,
    canTurn: (dir: Int) -> Boolean,
    apply: (s: Float, x: Float, y: Float) -> Unit,
    onOverviewFire: () -> Unit,
    onEdgeTurn: (dir: Int) -> Unit,
    onHaptic: () -> Unit,
    onSuppressTap: () -> Unit,
    driftTarget: () -> Pair<Float, Float>?,
    scope: CoroutineScope,
    /** Shared settle-animation slot — the caller cancels through the same
     *  reference so a programmatic reset can't race an in-flight bounce. */
    animSlot: MutableRef<Job?>,
): Modifier = pointerInput(zs) {
    fun settleTo(s: Float, x: Float, y: Float, ms: Int) {
        // Web order: apply the TARGET to the model first, then animate
        // the display toward it — a new gesture mid-slide reads the
        // destination, and the killTransition snap lands exactly there.
        val z0 = zs.zoom
        val x0 = zs.tx
        val y0 = zs.ty
        animSlot.value?.cancel()
        zs.apply(s, x, y)
        animSlot.value = scope.launch {
            Animatable(0f).animateTo(
                1f,
                tween(ms, easing = CubicBezierEasing(0.25f, 0.1f, 0.25f, 1f)),
            ) {
                apply(
                    z0 + (s - z0) * value,
                    x0 + (x - x0) * value,
                    y0 + (y - y0) * value,
                )
            }
            apply(s, x, y)
        }
    }

    awaitEachGesture {
        if (!enabled()) {
            // The overview owns the screen — drain touches without acting.
            awaitFirstDown(requireUnconsumed = false,
                pass = PointerEventPass.Initial)
            do {
                val e = awaitPointerEvent(PointerEventPass.Initial)
            } while (e.changes.any { it.pressed })
            return@awaitEachGesture
        }
        // gesture-local bookkeeping
        val starts = HashMap<PointerId, Offset>()
        val palms = HashSet<PointerId>()
        var pinching = false
        var dist0 = 0.0001f
        var panId: PointerId? = null
        var panOrigin = Offset.Zero
        var panVibrated = false
        var panMoved = false

        fun isPalmEdge(p: Offset): Boolean =
            p.x < ZoomK.PALM_EDGE || p.y < ZoomK.PALM_EDGE ||
                p.x > size.width - ZoomK.PALM_EDGE || p.y > size.height - ZoomK.PALM_EDGE

        fun seedPan(id: PointerId, at: Offset) {
            zs.panStart()
            panId = id
            panOrigin = at
            panVibrated = false
            panMoved = false
        }

        fun endPan() {
            val (armedTurn, dir) = zs.panEnd()
            zlog { "endPan armed=$armedTurn dir=$dir canTurn=${canTurn(dir)} tx=${zs.tx} zoom=${zs.zoom}" }
            panId = null
            if (armedTurn && canTurn(dir)) {
                onEdgeTurn(dir)
            } else if (zs.isZoomed) {
                // No turn — web clamps the rubber band back INSTANTLY
                // (zoom.js settle fallthrough); an animation here was a
                // same→same no-op anyway since panSettle pre-applied.
                zs.panSettle()
                apply(zs.zoom, zs.tx, zs.ty)
            }
            if (panMoved) onSuppressTap()
        }

        val down = awaitFirstDown(requireUnconsumed = false, pass = PointerEventPass.Initial)
        dev.bookq.reader.PerfTunables.mark("zg.down at=${down.position} enabled=${enabled()}")
        // killTransition — a new gesture kills any settle/zoom animation
        // and snaps the layer to the model's truth (web onStart).
        animSlot.value?.cancel()
        apply(zs.zoom, zs.tx, zs.ty)
        starts[down.id] = down.position
        if (isPalmEdge(down.position)) palms += down.id
        if (zs.isZoomed && down.id !in palms) seedPan(down.id, down.position)
        zlog { "gesture start down=${down.position} zoomed=${zs.isZoomed}" }

        while (true) {
            val ev = awaitPointerEvent(PointerEventPass.Initial)
            // The overview can take the screen mid-gesture (FireOverview).
            // Web's onMove/onEnd early-return while it is open — we keep
            // consuming events (nothing leaks to the pager) but apply
            // and settle nothing for the rest of this gesture.
            val live = enabled()
            for (c in ev.changes) {
                if (c.changedToDownIgnoreConsumed()) {
                    starts[c.id] = c.position
                    if (isPalmEdge(c.position)) palms += c.id
                    if (zs.isZoomed && !pinching && panId == null && c.id !in palms)
                        seedPan(c.id, c.position)
                }
                // First-seen seeding — a POINTER_DOWN that never reaches
                // this pass (coalesced/synthetic streams) still anchors
                // the pinch at its first observed position.
                if (c.pressed && c.id !in starts) starts[c.id] = c.position
            }
            // Palm promotion — a palm that moved ARM_PX becomes a real
            // touch; seeded mid-gesture like the 2→1 handoff.
            for (c in ev.changes) {
                if (c.pressed && c.id in palms) {
                    val s = starts[c.id] ?: c.position
                    if ((c.position - s).getDistance() >= ZoomK.ARM_PX) {
                        palms -= c.id
                        if (zs.isZoomed && !pinching && panId == null)
                            seedPan(c.id, c.position)
                    }
                }
            }
            val active = ev.changes.filter { it.pressed && it.id !in palms }

            if (pinching) {
                if (active.size >= 2) {
                    val a = active[0]
                    val b = active[1]
                    val d = (a.position - b.position).getDistance()
                    val mid = (a.position + b.position) / 2f
                    if (live) {
                        val dt = driftTarget()
                        when (val r = zs.pinchMove(
                            d / dist0, mid.x, mid.y, dt?.first, dt?.second)) {
                            is ZoomEvent.FireOverview -> {
                                zlog { "FireOverview d=$d ratio=${d / dist0}" }
                                onOverviewFire()
                            }
                            is ZoomEvent.Apply -> apply(r.s, r.x, r.y)
                        }
                    }
                    a.consume(); b.consume()
                } else {
                    val v = zs.pinchEnd()
                    zlog { "pinchEnd -> $v zoom=${zs.zoom}" }
                    if (live) {
                        when (v) {
                            ZoomEnd.PERSIST -> {}
                            ZoomEnd.SNAP_TO_ONE -> settleTo(1f, 0f, 0f, ZoomK.SLIDE_MS)
                            ZoomEnd.BOUNCE_TO_ONE -> settleTo(1f, 0f, 0f, ZoomK.BOUNCE_MS)
                        }
                    }
                    pinching = false
                    // 2→1: the still-down finger becomes the pan finger.
                    active.firstOrNull()?.let { r ->
                        if (live && zs.isZoomed) seedPan(r.id, r.position)
                    }
                }
            } else if (active.size >= 2) {
                val a = active[0]
                val b = active[1]
                val sa = starts[a.id] ?: a.position
                val sb = starts[b.id] ?: b.position
                if (live &&
                    (a.position - sa).getDistance() >= ZoomK.ARM_PX &&
                    (b.position - sb).getDistance() >= ZoomK.ARM_PX) {
                    dist0 = (sa - sb).getDistance().coerceAtLeast(0.0001f)
                    val mid0 = (sa + sb) / 2f
                    zs.pinchStart(mid0.x, mid0.y)
                    pinching = true
                    panId = null
                    zlog { "pinch armed dist0=$dist0 mid=$mid0 zoom=${zs.zoom}" }
                }
            } else if (active.size == 1 && panId != null) {
                val t = ev.changes.firstOrNull { it.id == panId && it.pressed }
                if (t != null && t.positionChange() != Offset.Zero) {
                    if (!live || !zs.isZoomed) {
                        panId = null
                    } else {
                        val dx = t.position.x - panOrigin.x
                        val dy = t.position.y - panOrigin.y
                        val r = zs.panMove(dx, dy)
                        if (r[2] == 1f) { // decided — pan is owned by zoom now
                            if (!panMoved) zlog { "pan decided dx=$dx dy=$dy tx=${zs.tx} armed=${zs.panArmed}" }
                            panMoved = true
                            apply(zs.zoom, r[0], r[1])
                            if (zs.panArmed && !panVibrated) {
                                panVibrated = true
                                onHaptic()
                            }
                            t.consume()
                        }
                    }
                }
            }
            // pan finger lifted while other touches remain → end that pan.
            if (panId != null &&
                ev.changes.any { it.id == panId && !it.pressed }) {
                if (live) endPan() else panId = null
            }
            if (ev.changes.none { it.pressed }) break
        }
        if (panId != null) {
            if (enabled()) endPan() else panId = null
        }
    }
}
