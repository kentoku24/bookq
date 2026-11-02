package dev.bookq.reader

import dev.bookq.core.api.TreeNode
import dev.bookq.core.reader.ReaderPageMode
import dev.bookq.reader.reader.ReaderController
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException

/**
 * `ReaderController.open` — the `loadImages` assembly glue: chapter set
 * from tree nodes, `/api/v1/images` fallback per empty chapter, resume
 * index, and session-only page mode. Core spread/chapter math itself is
 * covered by `:core` ReaderWorkTest.
 */
class ReaderControllerTest {

    private fun node(name: String, children: List<TreeNode> = emptyList()) =
        TreeNode(name, "folder", children = children)

    private fun img(name: String) = TreeNode(name, "image")

    private fun leaf(name: String, n: Int) =
        node(name, (1..n).map { img("p$it.png") })

    /** `manga/series/v01..v03` — depth-3, sibling-autoload eligible. */
    private fun series() = listOf(
        node("manga", listOf(
            node("series", listOf(leaf("v01", 4), leaf("v02", 6), leaf("v03", 5))),
        )),
    )

    private suspend fun openReady(
        c: ReaderController, path: String, page: Int = 0,
        roots: List<TreeNode>, w: Float = 1200f, h: Float = 1920f,
        lister: suspend (String) -> List<String> = { emptyList() },
    ): ReaderController.LoadState.Ready {
        c.open(path, page, roots, lister, w, h)
        return c.load.value as ReaderController.LoadState.Ready
    }

    @Test fun leafChapterFromTree() = runTest {
        val c = ReaderController()
        val r = openReady(c, "a/b/leaf", roots = listOf(
            node("a", listOf(node("b", listOf(leaf("leaf", 7)))))))
        assertEquals(1, r.work.chapters.size)
        assertEquals(7, r.work.realCount)
        assertEquals(0, r.startIdx)
        // Portrait → single page mode (web updateOrientation).
        assertEquals(ReaderPageMode.SINGLE, r.work.pageMode)
    }

    @Test fun resumeSavedPage() = runTest {
        val c = ReaderController()
        val r = openReady(c, "a/b/leaf", page = 3, roots = listOf(
            node("a", listOf(node("b", listOf(leaf("leaf", 7)))))))
        assertEquals(3, r.startIdx)
    }

    @Test fun crossChapterResumeOnClickedChapter() = runTest {
        val c = ReaderController()
        // Clicked v02 → its chapter (boundary after v01's 4 images + pad).
        val r = openReady(c, "manga/series/v02", page = 2, roots = series())
        assertEquals(3, r.work.chapters.size)
        assertEquals("v02", r.work.chapterInfo(r.startIdx).name)
        assertEquals(2, r.work.chapterInfo(r.startIdx).localIdx)
    }

    @Test fun fallbackFillsFromImageLister() = runTest {
        val c = ReaderController()
        // Path absent from the tree (archive without scanned children).
        val r = openReady(c, "manga/series/v01.cbz", roots = series(),
            lister = { f -> listOf("x1.png", "x2.png", "x3.png") })
        assertEquals(3, r.work.realCount)
        assertEquals("x1.png", r.work.images[0].name)
        assertEquals("manga/series/v01.cbz", r.work.images[0].folder)
    }

    @Test fun emptyWhenNoImagesAnywhere() = runTest {
        val c = ReaderController()
        c.open("a/b/leaf", 0, emptyList(), { emptyList() }, 1200f, 1920f)
        assertTrue(c.load.value is ReaderController.LoadState.Empty)
    }

    @Test fun errorWhenListerThrows() = runTest {
        val c = ReaderController()
        c.open("a/b/leaf", 0, emptyList(),
            { throw IOException("offline") }, 1200f, 1920f)
        assertTrue(c.load.value is ReaderController.LoadState.Error)
    }

    @Test fun landscapeStartsSpread() = runTest {
        val c = ReaderController()
        val r = openReady(c, "a/b/leaf", roots = listOf(
            node("a", listOf(node("b", listOf(leaf("leaf", 7)))))),
            w = 1920f, h = 1200f)
        assertEquals(ReaderPageMode.SPREAD, r.work.pageMode)
        // offset 0 pairs [0,1]… — same contract as core tests.
        assertEquals(listOf(0, 1), r.work.spreads.first())
    }

    @Test fun landscapeSpreadOffForcesSingle() = runTest {
        val c = ReaderController()
        c.toggleLandscapeSpread() // session-only toggle (web Q17)
        val r = openReady(c, "a/b/leaf", roots = listOf(
            node("a", listOf(node("b", listOf(leaf("leaf", 7)))))),
            w = 1920f, h = 1200f)
        assertEquals(ReaderPageMode.SINGLE, r.work.pageMode)
    }

    @Test fun remodeRotatesBetweenModes() = runTest {
        val c = ReaderController()
        val r = openReady(c, "a/b/leaf", roots = listOf(
            node("a", listOf(node("b", listOf(leaf("leaf", 7)))))),
            w = 1920f, h = 1200f)
        val rotated = c.remode(r.work, 1200f, 1920f)
        assertEquals(ReaderPageMode.SINGLE, rotated.pageMode)
        assertEquals(r.work.images, rotated.images) // order preserved
        val back = c.remode(rotated, 1920f, 1200f)
        assertEquals(ReaderPageMode.SPREAD, back.pageMode)
        assertEquals(r.work.spreads, back.spreads)
    }

    @Test fun recordARUpdatesMapAndFallback() = runTest {
        val c = ReaderController()
        c.recordAR(0, 0.71f)
        assertEquals(0.71f, c.arOf(0), 0.001f)
        assertEquals(0.71f, c.arOf(9), 0.001f) // lastAR fallback
        c.recordAR(1, -1f) // invalid — ignored
        assertEquals(0.71f, c.arOf(1), 0.001f)
    }
}
