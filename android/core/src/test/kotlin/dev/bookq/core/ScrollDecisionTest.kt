package dev.bookq.core

import dev.bookq.core.browse.ScrollDecider
import dev.bookq.core.browse.ScrollDecider.Cause
import dev.bookq.core.browse.ScrollDecider.Via
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** Port of `browse/scroll.js decide()` — the per-level anchor table. */
class ScrollDecisionTest {

    private fun decider() = ScrollDecider()

    @Test fun causeTopAlwaysWins() {
        val d = decider().decide("lvl", Cause.TOP, hasSaved = true, childPaths = listOf("a"), progressFolders = setOf("a"))
        assertEquals(ScrollDecider.Decision(Via.TOP, null), d)
    }

    @Test fun rootLevelUsesSavedOrTop() {
        var d = decider().decide(null, Cause.RESTORE, hasSaved = true, childPaths = emptyList(), progressFolders = emptySet())
        assertEquals(Via.SAVED, d.via)
        d = decider().decide(null, Cause.RESTORE, hasSaved = false, childPaths = emptyList(), progressFolders = emptySet())
        assertEquals(Via.TOP, d.via)
    }

    @Test fun openPicksLastReadChild() {
        // List order = server natural order; the LAST read child is the
        // highest-numbered read episode.
        val d = decider().decide(
            "w", Cause.OPEN, hasSaved = false,
            childPaths = listOf("w/ch1", "w/ch2", "w/ch3"),
            progressFolders = setOf("w/ch1", "w/ch3"),
        )
        assertEquals(ScrollDecider.Decision(Via.PROGRESS, "w/ch3"), d)
    }

    @Test fun openWithoutReadChildIsTop() {
        val d = decider().decide("w", Cause.OPEN, false, listOf("w/ch1"), emptySet())
        assertEquals(ScrollDecider.Decision(Via.TOP, null), d)
    }

    @Test fun openIntentIsIdempotentForSameLevel() {
        val sd = decider()
        sd.decide("w", Cause.OPEN, false, listOf("w/ch1", "w/ch2"), setOf("w/ch2"))
        // A second decide for the same level (restore path) must not lose
        // to a stale saved position — the recorded intent answers.
        val d2 = sd.decide("w", Cause.RESTORE, hasSaved = true, listOf("w/ch1", "w/ch2"), emptySet())
        assertEquals(ScrollDecider.Decision(Via.PROGRESS, "w/ch2"), d2)
    }

    @Test fun intentExpiresOnLevelChange() {
        val sd = decider()
        sd.decide("w", Cause.OPEN, false, listOf("w/ch1"), setOf("w/ch1"))
        val d = sd.decide("other", Cause.RESTORE, hasSaved = false, listOf("other/x"), emptySet())
        assertEquals(Via.TOP, d.via) // intent expired, no saved, no progress
    }

    @Test fun viewerReturnAnchorSurvivesRestore() {
        val sd = decider()
        sd.noteViewerReturn("w", "w/ch2")
        val d = sd.decide("w", Cause.RESTORE, hasSaved = true, listOf("w/ch1", "w/ch2"), emptySet())
        assertEquals(ScrollDecider.Decision(Via.VIEWER, "w/ch2"), d)
    }

    @Test fun anchorMissingFromFilteredListFallsThrough() {
        val sd = decider()
        sd.noteViewerReturn("w", "w/ch9")
        // w/ch9 filtered out → intent dropped, saved wins.
        val d = sd.decide("w", Cause.RESTORE, hasSaved = true, listOf("w/ch1"), emptySet())
        assertEquals(Via.SAVED, d.via)
        assertNull(sd.peekViewerReturn())
    }

    @Test fun savedWinsOverProgressFallback() {
        val d = decider().decide(
            "w", Cause.RESTORE, hasSaved = true,
            childPaths = listOf("w/ch1"), progressFolders = setOf("w/ch1"),
        )
        assertEquals(Via.SAVED, d.via)
    }

    @Test fun progressFallbackWhenNoSaved() {
        val d = decider().decide(
            "w", Cause.RESTORE, hasSaved = false,
            childPaths = listOf("w/ch1", "w/ch2"), progressFolders = setOf("w/ch1"),
        )
        assertEquals(ScrollDecider.Decision(Via.PROGRESS, "w/ch1"), d)
    }
}
