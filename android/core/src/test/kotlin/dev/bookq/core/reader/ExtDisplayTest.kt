package dev.bookq.core.reader

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * External-display projection (issue #301): the projection decision
 * gates, max-resolution mode selection, and the external-viewport
 * spread regroup — CP08-J1 has no DP Alt mode, so this pure-state
 * coverage stands in for physical panel QA (same treatment as iOS).
 */
class ExtDisplayTest {

    private fun disp(id: Int = 7, vararg modes: ExtDisplay.ModeRef) =
        ExtDisplay.DisplayRef(id,
            if (modes.isEmpty()) listOf(ExtDisplay.ModeRef(1920, 1080, 60f))
            else modes.toList())

    private fun mode(w: Int, h: Int, hz: Float = 60f) =
        ExtDisplay.ModeRef(w, h, hz)

    // MARK: - Projection decision

    @Test
    fun `reader on display attached and enabled projects`() {
        val d = disp()
        assertEquals(d, ExtDisplay.target(
            readerActive = true, paired = false, userEnabled = true,
            displays = listOf(d)))
    }

    @Test
    fun `no reader no projection`() {
        assertNull(ExtDisplay.target(readerActive = false, paired = false,
            userEnabled = true, displays = listOf(disp())))
    }

    @Test
    fun `paired session never projects`() {
        assertNull(ExtDisplay.target(readerActive = true, paired = true,
            userEnabled = true, displays = listOf(disp())))
    }

    @Test
    fun `user toggle off returns to mirroring`() {
        assertNull(ExtDisplay.target(readerActive = true, paired = false,
            userEnabled = false, displays = listOf(disp())))
    }

    @Test
    fun `no display means idle`() {
        assertNull(ExtDisplay.target(readerActive = true, paired = false,
            userEnabled = true, displays = emptyList()))
    }

    @Test
    fun `first presentation display wins`() {
        val a = disp(3); val b = disp(9)
        assertEquals(a, ExtDisplay.target(readerActive = true,
            paired = false, userEnabled = true, displays = listOf(a, b)))
    }

    // MARK: - Max-resolution mode

    @Test
    fun `bestMode picks most pixels`() {
        val modes = listOf(mode(1920, 1080), mode(3840, 2160), mode(2560, 1440))
        assertEquals(1, ExtDisplay.bestModeIndex(modes))
    }

    @Test
    fun `bestMode refresh breaks pixel ties`() {
        // Two 4K modes: the 120 Hz one wins over 60 Hz.
        val modes = listOf(mode(3840, 2160, 60f), mode(3840, 2160, 120f),
            mode(1280, 720, 240f))
        assertEquals(1, ExtDisplay.bestModeIndex(modes))
    }

    @Test
    fun `bestMode is deterministic on full ties`() {
        // Identical candidates resolve to the LAST index — iOS
        // `modes.indices.max(by:)` parity.
        val modes = listOf(mode(1920, 1080), mode(1920, 1080), mode(1280, 720))
        assertEquals(1, ExtDisplay.bestModeIndex(modes))
    }

    @Test
    fun `bestMode empty`() {
        assertNull(ExtDisplay.bestModeIndex(emptyList()))
    }

    // MARK: - External-viewport layout

    private fun work(mode: ReaderPageMode, n: Int = 8) = ReaderWork.assemble(
        clickedPath = "w",
        chapters = listOf(ReaderChapter("w", "w",
            (1..n).map { PageRef("$it.jpg", "w") })),
        pageMode = mode, spreadOffset = 0)

    @Test
    fun `landscape panel re-spreads a portrait single-page work`() {
        // Device is portrait (SINGLE); the 4K landscape panel must regroup
        // into spreads on its own dimensions.
        val w = work(ReaderPageMode.SINGLE)
        val (ext, spreadIdx) = ExtDisplay.workForDisplay(w, currentIdx = 2,
            extW = 3840f, extH = 2160f, landscapeSpread = true, spreadOffset = 0)
        assertEquals(ReaderPageMode.SPREAD, ext.pageMode)
        // Global idx 2 pairs with 3 in spread [2,3] → spread index 1.
        assertEquals(listOf(2, 3), ext.spreads[spreadIdx])
        assertEquals(1, spreadIdx)
    }

    @Test
    fun `portrait panel stays single even for a spread work`() {
        val w = work(ReaderPageMode.SPREAD)
        val (ext, spreadIdx) = ExtDisplay.workForDisplay(w, currentIdx = 5,
            extW = 1080f, extH = 1920f, landscapeSpread = true, spreadOffset = 0)
        assertEquals(ReaderPageMode.SINGLE, ext.pageMode)
        assertEquals(listOf(5), ext.spreads[spreadIdx])
        assertEquals(5, spreadIdx)
    }

    @Test
    fun `landscapeSpread off keeps singles on any panel`() {
        val w = work(ReaderPageMode.SPREAD)
        val (ext, _) = ExtDisplay.workForDisplay(w, currentIdx = 0,
            extW = 3840f, extH = 2160f, landscapeSpread = false, spreadOffset = 0)
        assertEquals(ReaderPageMode.SINGLE, ext.pageMode)
    }

    @Test
    fun `offset toggle shifts external pairing`() {
        val w = work(ReaderPageMode.SINGLE)
        val (ext, spreadIdx) = ExtDisplay.workForDisplay(w, currentIdx = 2,
            extW = 3840f, extH = 2160f, landscapeSpread = true, spreadOffset = 1)
        assertEquals(ReaderPageMode.SPREAD, ext.pageMode)
        // offset=1: page 0 alone, then pairs — idx 2 pairs with idx 1.
        assertEquals(listOf(1, 2), ext.spreads[spreadIdx])
    }

    @Test
    fun `external spread fits the panel`() {
        // spreadFit into the EXTERNAL viewport — the regrouped spread
        // [0.7ar, 0.7ar] is height-bound on a 3840x2160 canvas.
        val (fw, fh) = ReaderWork.spreadFit(listOf(0.7f, 0.7f),
            gap = 5f, viewW = 3840f, viewH = 2160f)
        assertEquals(2160f, fh, 0.001f)
        assertEquals(0.7f * 2160f * 2 + 5f, fw, 0.001f)
    }
}
