package dev.bookq.core

import dev.bookq.core.api.TreeNode
import dev.bookq.core.reader.PageRef
import dev.bookq.core.reader.ReaderChapter
import dev.bookq.core.reader.ReaderPageMode
import dev.bookq.core.reader.ReaderWork
import dev.bookq.core.reader.ZoneAction
import dev.bookq.core.reader.zoneAction
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Mirrors the spread/chapter semantics of app.js `loadImages`,
 * `getSiblingLeafFolders`, `getCurrentChapterInfo` and spread.js
 * `rebuildSpreads` — the same cases as iOS ReaderModelTests, adjusted for
 * web's SIBLING_AUTOLOAD_MIN_DEPTH gate (siblings merge only at depth ≥ 3).
 */
class ReaderWorkTest {

    private fun pages(folder: String, names: List<String>) =
        names.map { PageRef(it, folder) }

    private fun chapter(name: String, path: String, n: Int) =
        ReaderChapter(name, path, pages(path, (1..n).map { "p$it.png" }))

    private fun node(name: String, children: List<TreeNode> = emptyList()) =
        TreeNode(name, "folder", children = children)

    private fun img(name: String) = TreeNode(name, "image")

    // MARK: - spreads

    @Test fun singleModeOnePagePerSpread() {
        val w = ReaderWork.assemble("a", listOf(chapter("a", "a", 5)),
            ReaderPageMode.SINGLE, 0)
        assertEquals(listOf(listOf(0), listOf(1), listOf(2), listOf(3), listOf(4)), w.spreads)
        assertEquals(5, w.realCount)
    }

    @Test fun spreadModeOffset0Pairs() {
        val w = ReaderWork.assemble("a", listOf(chapter("a", "a", 5)),
            ReaderPageMode.SPREAD, 0)
        assertEquals(listOf(listOf(0, 1), listOf(2, 3), listOf(4)), w.spreads)
    }

    @Test fun spreadModeOffset1CoverSolo() {
        val w = ReaderWork.assemble("a", listOf(chapter("a", "a", 5)),
            ReaderPageMode.SPREAD, 1)
        assertEquals(listOf(listOf(0), listOf(1, 2), listOf(3, 4)), w.spreads)
    }

    @Test fun spreadOfAndNavigation() {
        val w = ReaderWork.assemble("a", listOf(chapter("a", "a", 5)),
            ReaderPageMode.SPREAD, 0)
        assertEquals(1, w.spreadOf(3))
        assertEquals(2, w.nextSpreadFirstPage(1))
        assertEquals(0, w.prevSpreadFirstPage(2))
        assertNull(w.nextSpreadFirstPage(4))
        assertNull(w.prevSpreadFirstPage(0))
    }

    // MARK: - chapters + spacers

    @Test fun crossChapterBoundariesEvenNoSpacers() {
        // chapter0 has 4 images → chapter1 already aligned (even) in offset0
        val w = ReaderWork.assemble("w/01",
            listOf(chapter("01", "w/01", 4), chapter("02", "w/02", 3)),
            ReaderPageMode.SPREAD, 0)
        assertEquals(listOf(0, 4), w.boundaries)
        assertEquals(7, w.images.size)
        assertEquals(0, w.images.count { it.isSpacer })
        assertEquals(listOf(listOf(0, 1), listOf(2, 3), listOf(4, 5), listOf(6)), w.spreads)
        assertEquals(setOf(2), w.chapterStartSpreads)
    }

    @Test fun crossChapterSpacerPadsOddBoundary() {
        // chapter0 has 3 → chapter1 starts at idx 3 (odd) → 1 spacer pad
        val w = ReaderWork.assemble("w/01",
            listOf(chapter("01", "w/01", 3), chapter("02", "w/02", 4)),
            ReaderPageMode.SPREAD, 0)
        assertEquals(8, w.images.size)          // +1 spacer
        assertTrue(w.images[3].isSpacer)
        assertEquals(listOf(0, 4), w.boundaries)
        assertEquals(listOf(listOf(0, 1), listOf(2), listOf(4, 5), listOf(6, 7)), w.spreads)
        assertEquals(7, w.realCount)
        assertEquals(3, w.realIndex(4))
        assertEquals(setOf(2), w.chapterStartSpreads)
    }

    @Test fun crossChapterOffset1Chapter0GetsLeadingSpacer() {
        // Intentional divergence from web (not a quirk port): app.js
        // double-counts earlier spacers in its startIdx, so offset=1
        // leaves c0 unpadded and can park later chapters on odd
        // boundaries. Here every boundary aligns post-shift — chapters
        // always open on their own right page (iOS agrees).
        val w = ReaderWork.assemble("w/01",
            listOf(chapter("01", "w/01", 3), chapter("02", "w/02", 2)),
            ReaderPageMode.SPREAD, 1)
        // c0: startIdx=0 → pad 1; c1: startIdx=4 → pad (4-1+2)%2=1
        // images: [S, r0, r1, r2, S, s0, s1]
        assertTrue(w.images[0].isSpacer)
        assertTrue(w.images[4].isSpacer)
        assertEquals(listOf(1, 5), w.boundaries)
        // groups: [0]=spacer dropped, (1,2), (3,4)→4 spacer→[3], (5,6)
        assertEquals(listOf(listOf(1, 2), listOf(3), listOf(5, 6)), w.spreads)
        assertEquals(setOf(2), w.chapterStartSpreads)
    }

    @Test fun chapterInfoLocalIndex() {
        val w = ReaderWork.assemble("w/01",
            listOf(chapter("01", "w/01", 3), chapter("02", "w/02", 4)),
            ReaderPageMode.SPREAD, 0)
        // boundaries [0,4] (1 spacer); global 5 → chapter1 local 1
        val info = w.chapterInfo(5)
        assertEquals(1, info.chapterIdx)
        assertEquals("02", info.name)
        assertEquals(1, info.localIdx)
        // global 2 → chapter0 local 2
        assertEquals(2, w.chapterInfo(2).localIdx)
    }

    @Test fun resumeGlobalIndexChapterLocal() {
        val w = ReaderWork.assemble("w/01",
            listOf(chapter("01", "w/01", 3), chapter("02", "w/02", 4)),
            ReaderPageMode.SPREAD, 0)
        // saved.page=2 on chapter1 → boundaries[1]+2 = 6
        assertEquals(6, w.resumeGlobalIndex(1, 2))
        // out-of-range clamps to chapter start
        assertEquals(4, w.resumeGlobalIndex(1, 99))
        assertEquals(0, w.resumeGlobalIndex(0, -1))
    }

    // MARK: - chapter spread range (getChapterSpreadRange)

    @Test fun chapterSpreadRangeClampsWithinChapter() {
        val w = ReaderWork.assemble("w/01",
            listOf(chapter("01", "w/01", 3), chapter("02", "w/02", 4)),
            ReaderPageMode.SPREAD, 0)
        // spreads [0,1][2][4,5][6,7]; ch0 spans globals 0..2 → spreads 0..1
        assertEquals(0..1, w.chapterSpreadRange(0))
        // ch1 spans globals 4..7 → spreads 2..3
        assertEquals(2..3, w.chapterSpreadRange(5))
    }

    // MARK: - siblingLeaves + chapters (depth ≥ 3 — the web gate)

    @Test fun siblingLeavesFindsParentChildren() {
        // parent "w/series" holds leaf folders 01/02 plus a non-leaf sub
        val tree = listOf(node("w", listOf(
            node("series", listOf(
                node("01", listOf(img("p1.png"), img("p2.png"))),
                node("02", listOf(img("p1.png"))),
                node("sub", listOf(node("x", listOf(img("a.png"))))),
            )),
        )))
        val sibs = ReaderWork.siblingLeaves("w/series/01", tree)
        assertEquals(listOf("w/series/01", "w/series/02"), sibs?.map { it.second })
        val (list, cross) = ReaderWork.chapters("w/series/01", tree)
        assertTrue(cross)
        assertEquals(listOf("w/series/01", "w/series/02"), list.map { it.fullPath })
        assertEquals(2, list[0].images.size)
    }

    @Test fun depthTwoPathNeverMerges() {
        // web SIBLING_AUTOLOAD_MIN_DEPTH: "series/01" is depth 2 → solo
        // even though leaf siblings exist (iOS drops this gate; web wins).
        val tree = listOf(node("series", listOf(
            node("01", listOf(img("p1.png"))),
            node("02", listOf(img("p2.png"))),
        )))
        assertNull(ReaderWork.siblingLeaves("series/01", tree))
        val (list, cross) = ReaderWork.chapters("series/01", tree)
        assertFalse(cross)
        assertEquals(1, list.size)
        assertEquals(1, list[0].images.size)
    }

    @Test fun topLevelNodeHasNoSiblings() {
        val tree = listOf(node("a", listOf(img("p.png"))), node("b", listOf(img("q.png"))))
        assertNull(ReaderWork.siblingLeaves("a", tree))
        val (list, cross) = ReaderWork.chapters("a", tree)
        assertFalse(cross)
        assertEquals(1, list.size)
        assertEquals(1, list[0].images.size)
    }

    @Test fun mixedSelfFolderDisablesCross() {
        // "w/s/01" holds an image AND a subfolder → mixed → single chapter
        val tree = listOf(node("w", listOf(
            node("s", listOf(
                node("01", listOf(img("p1.png"), node("extra", listOf(img("e.png"))))),
                node("02", listOf(img("p1.png"))),
            )),
        )))
        val (list, cross) = ReaderWork.chapters("w/s/01", tree)
        assertFalse(cross)
        assertEquals(1, list[0].images.size)   // direct images only
    }

    @Test fun plainFallbackYieldsEmptyChapter() {
        // Node missing from tree → caller fills images from /api/v1/images.
        val (list, cross) = ReaderWork.chapters("gone/archive.cbz", emptyList())
        assertFalse(cross)
        assertEquals(1, list.size)
        assertTrue(list[0].images.isEmpty())
    }

    // MARK: - chapter labels

    @Test fun chapterStartFlaggedOnEveryBoundary() {
        // 3 chapters (4p,4p,2p), spread mode — every chapter-start spread
        // except chapter0's gets the label flag (labels "follow" turns).
        val w = ReaderWork.assemble("w/01",
            listOf(chapter("01", "w/01", 4), chapter("02", "w/02", 4),
                chapter("03", "w/03", 2)),
            ReaderPageMode.SPREAD, 0)
        assertEquals(listOf(0, 4, 8), w.boundaries)
        assertTrue(w.chapterStartSpreads.contains(w.spreadOf(4)))
        assertTrue(w.chapterStartSpreads.contains(w.spreadOf(8)))
        assertFalse(w.chapterStartSpreads.contains(w.spreadOf(0)))
        assertFalse(w.chapterStartSpreads.contains(w.spreadOf(2)))
    }

    // MARK: - tap zones

    @Test fun zoneActionLeftMode() {
        assertEquals(ZoneAction.Turn(1), zoneAction(0.10f, true))
        assertEquals(ZoneAction.Turn(-1), zoneAction(0.90f, true))
    }

    @Test fun zoneActionRightMode() {
        assertEquals(ZoneAction.Turn(-1), zoneAction(0.10f, false))
        assertEquals(ZoneAction.Turn(1), zoneAction(0.90f, false))
    }

    @Test fun zoneActionCenterTogglesChrome() {
        assertEquals(ZoneAction.ToggleChrome, zoneAction(0.50f, true))
        assertEquals(ZoneAction.ToggleChrome, zoneAction(0.50f, false))
    }

    /** Boundary taps land on the declared 30%/70% edges — a tap exactly on
     *  the line goes to the outer side of the band it belongs to. */
    @Test fun zoneActionBoundaries() {
        assertEquals(ZoneAction.Turn(1), zoneAction(0.29f, true))
        assertEquals(ZoneAction.Turn(-1), zoneAction(0.71f, true))
        assertEquals(ZoneAction.ToggleChrome, zoneAction(0.30f, true))
        assertEquals(ZoneAction.ToggleChrome, zoneAction(0.70f, true))
    }

    // MARK: - page mode + spreadFit

    @Test fun pageModeRule() {
        assertEquals(ReaderPageMode.SPREAD,
            ReaderWork.pageMode(1920f, 1200f, landscapeSpread = true))
        assertEquals(ReaderPageMode.SINGLE,
            ReaderWork.pageMode(1920f, 1200f, landscapeSpread = false))
        assertEquals(ReaderPageMode.SINGLE,
            ReaderWork.pageMode(1200f, 1920f, landscapeSpread = true))
    }

    @Test fun spreadFitHeightBound() {
        // Two 0.7-ar pages in a 2000x1000 viewport: h=1000 → w=1405 < viewW.
        val (w, h) = ReaderWork.spreadFit(listOf(0.7f, 0.7f), 5f, 2000f, 1000f)
        assertEquals(1000f, h, 0.01f)
        assertEquals(1405f, w, 0.01f)
    }

    @Test fun spreadFitWidthBound() {
        // Two 0.7-ar pages in a 1200x1920 viewport: natural w=2693 > viewW
        // → clamped to width.
        val (w, h) = ReaderWork.spreadFit(listOf(0.7f, 0.7f), 5f, 1200f, 1920f)
        assertEquals(1200f, w, 0.01f)
        assertEquals((1200f - 5f) / 1.4f, h, 0.01f)
    }
}
