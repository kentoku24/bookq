package dev.bookq.core

import dev.bookq.core.reader.ZoomBox
import dev.bookq.core.reader.ZoomEnd
import dev.bookq.core.reader.ZoomEvent
import dev.bookq.core.reader.ZoomK
import dev.bookq.core.reader.ZoomState
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs

/** viewer/zoom.js semantics — bands, midpoint anchor, pan clamp, edge
 *  rubber-band turn, and the release verdicts. */
class ZoomStateTest {

    // 700x900 page in a 1200x1920 viewport — height-bound.
    private fun z() = ZoomState().apply {
        box = ZoomBox.centered(630f, 900f, 1200f, 1920f)
    }

    // ── pinch bands ──

    @Test fun pinchOutZoomsMidpointAnchored() {
        val s = z()
        s.pinchStart(600f, 960f)
        val ev = s.pinchMove(2f, 700f, 1000f) as ZoomEvent.Apply
        assertEquals(2f, ev.s, 0.001f)
        // nx = 700 - 2*(600-0) = -500 but the corner-clamp pulls it to
        // maxX = -570 (spread 630*2=1260 > 1200 covers; box.x=285).
        assertEquals(-570f, ev.x, 0.01f)
        // Height 900*2=1800 < 1920 can't cover → centered, not panned.
        assertEquals((1920f - 1800f) / 2f - 510f * 2f, ev.y, 0.5f)
        assertTrue(s.isZoomed)
    }

    @Test fun pinchClampsNearCorner() {
        // Zoom 3x: wild midpoint must clamp into the legal pan range.
        val s = z()
        s.pinchStart(600f, 960f)
        s.pinchMove(3f, 50f, 50f)
        val b = ZoomBox.centered(630f, 900f, 1200f, 1920f)
        val minX = b.viewW - (b.x * s.zoom + b.w * s.zoom)
        val maxX = -b.x * s.zoom
        assertTrue(s.tx in minX - 0.5f..maxX + 0.5f)
        val minY = b.viewH - (b.y * s.zoom + b.h * s.zoom)
        val maxY = -b.y * s.zoom
        assertTrue(s.ty in minY - 0.5f..maxY + 0.5f)
    }

    @Test fun pinchInShrinkBandDrifts() {
        val s = z()
        s.pinchStart(600f, 960f)
        // ratio 0.75 → target 0.75: in 0.5..1 shrink band.
        val ev = s.pinchMove(0.75f, 600f, 960f, driftX = 600f, driftY = 640f)
        assertTrue(ev is ZoomEvent.Apply)
        assertEquals(0.75f, s.zoom, 0.001f)
        // p = (1-0.75)/0.5 = 0.5 → cy halfway between center 960 and 640
        // → t.y = cy - s*H/2 = (960 - 0.5*(960-640)) - 0.75*960
        val cy = 960f + 0.5f * (640f - 960f)
        assertEquals(cy - 0.75f * 1920f / 2f, s.ty, 0.5f)
    }

    @Test fun pinchBelowFireFiresOnce() {
        val s = z()
        s.pinchStart(600f, 960f)
        assertTrue(s.pinchMove(0.4f, 600f, 960f) is ZoomEvent.FireOverview)
        // Second move at ≤FIRE must not re-fire.
        assertTrue(s.pinchMove(0.45f, 600f, 960f) is ZoomEvent.Apply)
        // Re-arm above 0.7, then ≤0.5 fires again.
        s.pinchMove(0.75f, 600f, 960f)
        assertTrue(s.pinchMove(0.45f, 600f, 960f) is ZoomEvent.FireOverview)
    }

    @Test fun pinchClampsToMinMax() {
        val s = z()
        s.pinchStart(600f, 960f)
        s.pinchMove(10f, 600f, 960f)
        assertEquals(ZoomK.MAX, s.zoom, 0.001f)
        s.pinchStart(600f, 960f)
        val ev = s.pinchMove(0.01f, 600f, 960f)
        // Below FIRE the gesture returns FireOverview WITHOUT applying —
        // the layer keeps its last transform (web leaves it for the
        // overview to own). Zoom stays at the pre-pinch value.
        assertTrue(ev is ZoomEvent.FireOverview)
        assertEquals(ZoomK.MAX, s.zoom, 0.001f)
    }

    // ── release verdicts ──

    @Test fun pinchEndPersistsAboveSnap() {
        val s = z()
        s.pinchStart(600f, 960f)
        s.pinchMove(1.3f, 700f, 960f)
        assertEquals(ZoomEnd.PERSIST, s.pinchEnd())
        assertTrue(s.zoom > 1f)
    }

    @Test fun pinchEndSnapsToOneBetweenOneAndSnap() {
        val s = z()
        s.pinchStart(600f, 960f)
        s.pinchMove(1.1f, 640f, 960f)
        assertEquals(ZoomEnd.SNAP_TO_ONE, s.pinchEnd())
        // pinchEnd doesn't apply — the caller slides, then apply(1,0,0).
        assertTrue(s.zoom > 1f)
    }

    @Test fun pinchEndBouncesBelowOne() {
        val s = z()
        s.pinchStart(600f, 960f)
        s.pinchMove(0.8f, 600f, 960f)
        assertEquals(ZoomEnd.BOUNCE_TO_ONE, s.pinchEnd())
        // bounce target = 1,0,0 (animation is the caller's job).
    }

    // ── zoomed pan + edge turn ──

    private fun zoomed(): ZoomState {
        val s = z()
        s.pinchStart(600f, 960f)
        s.pinchMove(2f, 600f, 960f)
        s.pinchEnd()
        s.panStart()
        return s
    }

    @Test fun panClampsToSpreadEdges() {
        val s = zoomed()
        s.panMove(5000f, 0f) // way past the left edge
        val b = s.box
        val maxX = -b.x * s.zoom
        assertTrue(s.tx <= maxX + 0.5f)
    }

    @Test fun panDecidesAtEightPx() {
        val s = zoomed()
        val r = s.panMove(4f, 0f)
        assertEquals(0f, r[2]) // undecided → no move yet
        s.panMove(9f, 0f)
        assertTrue(s.panDecided)
        assertTrue(s.panHorizontal)
    }

    @Test fun edgeRubberBandsAndArms() {
        val s = zoomed()
        // Pin the spread's LEFT edge at the viewport left → noRoom = -1.
        s.panMove(2000f, 0f)
        s.panStart()
        assertEquals(-1, s.noRoomDirAtStart())
        s.panMove(ZoomK.EDGE_PX + 20f, 0f) // keep pulling right, rubber
        assertTrue(s.panArmed)
        assertEquals(1, s.panArmedDir)
        val (turn, dir) = s.panEnd()
        assertTrue(turn); assertEquals(1, dir)
    }

    @Test fun edgeDisarmsOnReverse() {
        val s = zoomed()
        s.panMove(2000f, 0f)
        s.panStart()
        s.panMove(ZoomK.EDGE_PX + 20f, 0f)
        assertTrue(s.panArmed)
        s.panMove(ZoomK.EDGE_PX - 40f, 0f) // back under threshold
        assertFalse(s.panArmed)
        assertEquals(false, s.panEnd().first)
    }

    @Test fun centeredAxisCountsBothDirections() {
        // Page narrower than viewport even zoomed → noRoom = 2.
        val s = ZoomState().apply {
            box = ZoomBox.centered(400f, 600f, 1200f, 1920f)
            apply(1.5f, 0f, 0f) // 600x900 — width can't cover either way
        }
        s.panStart()
        assertEquals(2, s.noRoomDirAtStart())
    }
}
