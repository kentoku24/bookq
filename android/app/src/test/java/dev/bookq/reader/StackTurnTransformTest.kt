package dev.bookq.reader

import dev.bookq.reader.ui.reader.stackTurnAlpha
import dev.bookq.reader.ui.reader.stackTurnTranslateX
import dev.bookq.reader.ui.reader.stackTurnZ
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Page-stack turn geometry (`ReaderScreen.kt`). `off` is the page's
 * forward-axis distance from the scroll position; in the RTL pager
 * +off sits left of center (next side), −off right (prev side). Only
 * off<0 pages travel — parked pages hold the viewport center.
 */
class StackTurnTransformTest {

    // ── translation ──────────────────────────────────────────────

    @Test fun `moving side keeps natural position`() {
        // off<0 pages translate nothing: the pager's natural placement
        // already makes them exit right (forward) / enter right (back).
        assertEquals(0f, stackTurnTranslateX(-0.4f), 1e-6f)
        assertEquals(0f, stackTurnTranslateX(-1.3f), 1e-6f)
    }

    @Test fun `next side parks at viewport center`() {
        // off>0 pages shift +off widths: from their natural left slot
        // to centered, covered by the current page.
        assertEquals(0.7f, stackTurnTranslateX(0.7f), 1e-6f)
        assertEquals(1.0f, stackTurnTranslateX(1.0f), 1e-6f)
        assertEquals(2.3f, stackTurnTranslateX(2.3f), 1e-6f)
    }

    @Test fun `settled page is not translated`() {
        assertEquals(0f, stackTurnTranslateX(0f), 1e-6f)
    }

    // ── z order ──────────────────────────────────────────────────

    @Test fun `moving page draws above the stack`() {
        // Forward (fraction>0): the departing page (off<0) tops the
        // parked next page (off>0). Reverse: the incoming page tops
        // the covered current.
        assertTrue(stackTurnZ(-0.5f) > stackTurnZ(0.5f))
        assertTrue(stackTurnZ(-1.5f) > stackTurnZ(0.5f))
        assertTrue(stackTurnZ(0f) > stackTurnZ(1f))
    }

    @Test fun `parked pages stack nearest-first`() {
        // Under a covered page the nearer next sits above next-next.
        assertTrue(stackTurnZ(1f) > stackTurnZ(2f))
    }

    // ── covered-page scrim ───────────────────────────────────────

    @Test fun `moving pages are never dimmed`() {
        assertEquals(1f, stackTurnAlpha(-0.4f, 0.4f), 1e-6f)
        assertEquals(1f, stackTurnAlpha(-1.4f, -0.4f), 1e-6f)
    }

    @Test fun `forward reveal brightens as uncovered`() {
        // fraction 0→1: next page goes fully covered → fully revealed.
        val early = stackTurnAlpha(0.9f, 0.1f)
        val late = stackTurnAlpha(0.1f, 0.9f)
        assertTrue(early < late)
        assertEquals(1f, late, 0.36f)  // ~1 within scrim depth
        assertTrue(early >= 0.65f)     // never darker than 1 - SCRIM
    }

    @Test fun `reverse cover dims as covered`() {
        // fraction 0→−1: current page goes uncovered → fully covered.
        val early = stackTurnAlpha(0.1f, -0.1f)
        val late = stackTurnAlpha(0.9f, -0.9f)
        assertTrue(early > late)
        assertTrue(late >= 0.65f)
    }

    @Test fun `fully covered page is maximally dimmed`() {
        // frac=0: parked pages sit fully under the opaque current slot —
        // dim is max but invisible either way.
        assertEquals(0.65f, stackTurnAlpha(1f, 0f), 1e-6f)
    }
}
