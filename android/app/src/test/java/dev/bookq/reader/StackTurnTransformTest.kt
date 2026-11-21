package dev.bookq.reader

import dev.bookq.reader.ui.reader.stackTurnScrim
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
 *
 * The covered page stays opaque under a black scrim rect; `off` doubles
 * as the covered fraction, so deeper parked pages saturate at max scrim.
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

    @Test fun `moving pages get no scrim`() {
        assertEquals(0f, stackTurnScrim(-0.4f), 1e-6f)
        assertEquals(0f, stackTurnScrim(-1.4f), 1e-6f)
        assertEquals(0f, stackTurnScrim(0f), 1e-6f)
    }

    @Test fun `scrim shrinks as the page is uncovered`() {
        // Forward: covered fraction = off; early turn the next page is
        // still mostly covered → heavy scrim, late → light.
        assertTrue(stackTurnScrim(0.9f) > stackTurnScrim(0.1f))
        assertEquals(0.035f, stackTurnScrim(0.1f), 1e-6f)
        assertEquals(0.315f, stackTurnScrim(0.9f), 1e-6f)
    }

    @Test fun `fully covered page gets max scrim`() {
        // Deep parked pages (off>1) sit fully under the page above.
        assertEquals(0.35f, stackTurnScrim(1f), 1e-6f)
        assertEquals(0.35f, stackTurnScrim(2f), 1e-6f)
    }
}
