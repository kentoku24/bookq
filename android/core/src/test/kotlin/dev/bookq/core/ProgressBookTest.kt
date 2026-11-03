package dev.bookq.core

import dev.bookq.core.api.ReadingPosition
import dev.bookq.core.progress.ProgressBook
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Port of app.js `ReadingProgress` map semantics (50-cap, LWW merge). */
class ProgressBookTest {

    private fun pos(folder: String, page: Int = 0, total: Int = 10, ts: Long = 0) =
        ReadingPosition(folder, page, total, ts)

    @Test fun putAndGet() {
        val b = ProgressBook()
        b.put(pos("w/ch1", page = 3, ts = 100))
        assertEquals(3, b["w/ch1"]?.page)
        assertEquals(setOf("w/ch1"), b.folders())
    }

    @Test fun evictsOldestBeyond50() {
        val b = ProgressBook()
        for (i in 0 until 60) b.put(pos("f$i", ts = i.toLong()))
        assertEquals(50, b.all().size)
        assertNull(b["f0"]) // oldest evicted
        assertNull(b["f9"])
        assertEquals(59L, b["f59"]?.ts)
    }

    @Test fun mergeRemoteIsLastWriterWins() {
        val b = ProgressBook()
        b.put(pos("w", page = 5, ts = 100))
        // older remote is ignored
        assertEquals(false, b.mergeRemote(pos("w", page = 1, ts = 50)))
        assertEquals(5, b["w"]?.page)
        // newer remote lands verbatim (its own ts — no echo)
        assertEquals(true, b.mergeRemote(pos("w", page = 8, ts = 200)))
        assertEquals(8, b["w"]?.page)
        assertEquals(200L, b["w"]?.ts)
    }

    @Test fun resumeCandidateIsNewestVisible() {
        val b = ProgressBook()
        b.put(pos("vis/old", page = 1, ts = 10))
        b.put(pos("hid/new", page = 9, ts = 20))
        // without a visibility filter the newest wins
        assertEquals("hid/new", b.resumeCandidate()?.folder)
        // hidden-root history is skipped (records never deleted)
        assertEquals("vis/old", b.resumeCandidate { !it.startsWith("hid") }?.folder)
    }

    @Test fun resumeLabel() {
        assertEquals(
            "📖 続きから: ch3 p.6",
            ProgressBook.resumeLabel(pos("Series/arc/ch3", page = 5)),
        )
        // flattened name — last segment only (web split("/").pop())
        assertEquals(
            "📖 続きから: 巻2 p.1",
            ProgressBook.resumeLabel(pos("flat/巻2", page = 0)),
        )
    }
}
