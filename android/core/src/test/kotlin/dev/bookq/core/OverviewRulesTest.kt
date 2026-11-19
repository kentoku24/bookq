package dev.bookq.core

import dev.bookq.core.reader.OverviewRules
import dev.bookq.core.reader.PageRef
import dev.bookq.core.reader.ReaderChapter
import dev.bookq.core.reader.ReaderPageMode
import dev.bookq.core.reader.ReaderWork
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Overview grid mapping — overview.js buildGrid / nearestRealPos /
 * IMG_WINDOW: flat header+cell stream in reading order, spacer-free
 * cells, and the ±200 real-page hydration band.
 */
class OverviewRulesTest {

    private fun pages(folder: String, n: Int) =
        (1..n).map { PageRef("p$it.png", folder) }

    private fun chapter(name: String, path: String, n: Int) =
        ReaderChapter(name, path, pages(path, n))

    private fun workOf(vararg sizes: Int, mode: ReaderPageMode = ReaderPageMode.SINGLE) =
        ReaderWork.assemble("a/b/c0",
            sizes.mapIndexed { i, n -> chapter("c$i", "a/b/c$i", n) },
            mode, 0)

    @Test fun flatItemsSingleChapterNoHeader() {
        val w = workOf(4)
        val flat = OverviewRules.flatItems(w)
        assertEquals(listOf(0, 1, 2, 3), flat.map { it.second })
        assertTrue(flat.none { it.first })
    }

    @Test fun flatItemsCrossChapterHeaders() {
        val w = workOf(3, 2)
        val flat = OverviewRules.flatItems(w)
        // h0, c0 pages 0..2, h1, c1 pages — spacer at 3 pushes the
        // chapter-1 boundary to globals 4,5 (load-time padding).
        assertEquals(
            listOf(true to 0, false to 0, false to 1, false to 2,
                true to 1, false to 4, false to 5),
            flat)
    }

    @Test fun flatItemsSkipSpacers() {
        // Cross-chapter spread mode pads boundaries with spacers —
        // cells must map to REAL pages only.
        val w = workOf(3, 2, 2, mode = ReaderPageMode.SPREAD)
        val flat = OverviewRules.flatItems(w)
        val globals = flat.filter { !it.first }.map { it.second }
        assertTrue(globals.all { !w.images[it].isSpacer })
        assertEquals(w.realCount, globals.size)
    }

    @Test fun nearestItemFirstPage() {
        val w = workOf(3, 2)
        val flat = OverviewRules.flatItems(w)
        assertEquals(1, OverviewRules.nearestItem(flat, 0))
    }

    @Test fun nearestItemPastSpacer() {
        // Landing on a spacer before chapter 1 highlights that
        // chapter's first real page (nearest at/after).
        val w = workOf(3, 2, mode = ReaderPageMode.SPREAD)
        val flat = OverviewRules.flatItems(w)
        // ch0 = pages 0..2, spacer at 3, ch1 starts at 4.
        assertTrue(w.images[3].isSpacer)
        val i = OverviewRules.nearestItem(flat, 3)
        assertEquals(false, flat[i].first)
        assertEquals(4, flat[i].second)
    }

    @Test fun nearestItemPastEndClampsToLast() {
        val w = workOf(3, 2)
        val flat = OverviewRules.flatItems(w)
        val i = OverviewRules.nearestItem(flat, 999)
        assertEquals(false, flat[i].first)
        assertEquals(5, flat[i].second)
    }

    @Test fun realIndicesSkipSpacers() {
        val w = workOf(3, 2, mode = ReaderPageMode.SPREAD)
        val reals = OverviewRules.realIndices(w)
        assertTrue(3 !in reals) // the spacer
        assertEquals(w.realCount, reals.size)
    }

    @Test fun imgWindowCentersOnCurrent() {
        val w = workOf(1000)
        val reals = OverviewRules.realIndices(w)
        val r = OverviewRules.imgWindow(reals, 500, radius = 200)
        assertEquals(300, r.first)
        assertEquals(700, r.last)
    }

    @Test fun imgWindowClampsToEdges() {
        val w = workOf(300)
        val reals = OverviewRules.realIndices(w)
        val r = OverviewRules.imgWindow(reals, 5, radius = 200)
        assertEquals(0, r.first)
        assertEquals(205, r.last)
        val r2 = OverviewRules.imgWindow(reals, 299, radius = 200)
        assertEquals(299, r2.last)
    }

    @Test fun imgWindowEmpty() {
        assertEquals(-1..-1, OverviewRules.imgWindow(emptyList(), 0))
    }
}
