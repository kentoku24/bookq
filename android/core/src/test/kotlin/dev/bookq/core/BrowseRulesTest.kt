package dev.bookq.core

import dev.bookq.core.api.TreeNode
import dev.bookq.core.browse.BrowseRules
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Browse rule parity with `app/static/browse/render.js` —
 *  port of iOS `BrowseModelTests.swift`. */
class BrowseRulesTest {

    private fun folder(
        name: String,
        children: List<TreeNode>? = null,
        thumb: String? = null,
        thumbs: List<String>? = null,
    ) = TreeNode(name = name, type = "folder", children = children, thumb = thumb, thumbs = thumbs)

    private fun image(name: String) = TreeNode(name = name, type = "image")

    // ── hasOnlyImages / mixed ──

    @Test fun hasOnlyImages() {
        assertTrue(BrowseRules.hasOnlyImages(folder("a", listOf(image("1.jpg")))))
        assertFalse(BrowseRules.hasOnlyImages(folder("a")))
        assertFalse(BrowseRules.hasOnlyImages(folder("a", emptyList())))
        assertFalse(BrowseRules.hasOnlyImages(folder("a", listOf(folder("b")))))
        assertFalse(BrowseRules.hasOnlyImages(folder("a", listOf(image("1"), folder("b")))))
    }

    @Test fun mixed() {
        assertTrue(BrowseRules.isMixed(folder("a", listOf(image("1"), folder("b")))))
        assertFalse(BrowseRules.isMixed(folder("a", listOf(image("1")))))
        assertFalse(BrowseRules.isMixed(folder("a", listOf(folder("b")))))
    }

    // ── node lookup — names with "/" are never split ──

    @Test fun nodeAtPath() {
        val tree = listOf(
            folder("Series/巻1", listOf(image("p1"), image("p2"))),
            folder("Series", listOf(folder("Sub", listOf(image("x"))))),
        )
        // flattened name resolves as a composed path
        assertEquals("Series/巻1", BrowseRules.nodeAt("Series/巻1", tree)?.name)
        // composed match on the flattened child too
        assertEquals("Sub", BrowseRules.nodeAt("Series/Sub", tree)?.name)
        assertNull(BrowseRules.nodeAt("Series/Nothing", tree))
        assertNull(BrowseRules.nodeAt("", tree))
    }

    @Test fun nodeAtDoesNotMatchNameSplit() {
        // path "r/wrap/x" must hit the flattened node "wrap/x" under "r",
        // not some deeper traversal.
        val tree = listOf(folder("r", listOf(folder("wrap/x", listOf(image("p"))))))
        assertEquals("wrap/x", BrowseRules.nodeAt("r/wrap/x", tree)?.name)
    }

    // ── level entries & self tiles ──

    @Test fun selfTilesMixedOnly() {
        val root = folder(
            "root",
            listOf(
                folder("mixed", listOf(image("1"), folder("sub"))),
                folder("leaf", listOf(image("2"))),
                folder("nested", listOf(folder("deep"))),
            ),
        )
        val tiles = BrowseRules.selfTiles("root", listOf(root))
        assertEquals(listOf("root/mixed"), tiles.map { it.first })
    }

    @Test fun selfTileAtRootForLooseImages() {
        assertEquals(1, BrowseRules.selfTiles("", listOf(image("a.jpg"))).size)
        assertEquals(0, BrowseRules.selfTiles("", listOf(folder("f"))).size)
    }

    @Test fun drillsDown() {
        assertFalse(BrowseRules.drillsDown(folder("leaf", listOf(image("1")))))
        assertTrue(BrowseRules.drillsDown(folder("mix", listOf(image("1"), folder("s")))))
        assertTrue(BrowseRules.drillsDown(folder("nest", listOf(folder("s")))))
    }

    // ── multiThumbSpec table parity ──

    @Test fun multiThumbSpec() {
        assertNull(BrowseRules.multiThumbSpec(0))
        assertNull(BrowseRules.multiThumbSpec(1))
        assertEquals(BrowseRules.ThumbSpec(listOf(2), 2), BrowseRules.multiThumbSpec(2))
        assertEquals(BrowseRules.ThumbSpec(listOf(1, 2), 2), BrowseRules.multiThumbSpec(3))
        assertEquals(BrowseRules.ThumbSpec(listOf(2, 2), 2), BrowseRules.multiThumbSpec(4))
        assertEquals(BrowseRules.ThumbSpec(listOf(2, 3), 6), BrowseRules.multiThumbSpec(5))
        assertEquals(BrowseRules.ThumbSpec(listOf(2, 2, 2), 2), BrowseRules.multiThumbSpec(6))
        assertEquals(BrowseRules.ThumbSpec(listOf(2, 3, 2), 6), BrowseRules.multiThumbSpec(7))
        assertEquals(BrowseRules.ThumbSpec(listOf(3, 2, 3), 6), BrowseRules.multiThumbSpec(8))
        assertEquals(BrowseRules.ThumbSpec(listOf(3, 3, 3), 3), BrowseRules.multiThumbSpec(9))
        // >9 clamps to 9
        assertEquals(BrowseRules.multiThumbSpec(9), BrowseRules.multiThumbSpec(12))
    }

    // ── display name / filter ──

    @Test fun splitDisplayName() {
        assertEquals("abc" to null, BrowseRules.splitDisplayName("abc"))
        assertEquals("子" to "親", BrowseRules.splitDisplayName("親/子"))
        // last "/" only — deeper names keep their full parent chain
        assertEquals("c" to "a/b", BrowseRules.splitDisplayName("a/b/c"))
    }

    @Test fun filter() {
        val entries = listOf("a/One" to folder("One"), "a/Two" to folder("Two"))
        assertEquals(1, BrowseRules.filter(entries, "one") { it.name }.size)
        assertEquals(1, BrowseRules.filter(entries, "ONE") { it.name }.size) // case-insensitive
        assertEquals(2, BrowseRules.filter(entries, "a/") { it.name }.size)  // path match
        assertEquals(2, BrowseRules.filter(entries, "") { it.name }.size)
        assertEquals(0, BrowseRules.filter(entries, "zzz") { it.name }.size)
    }

    // ── leafPathsFor (storeAdapter.js computeLeafPathsFor) ──

    @Test fun leafPathsForLeafDescendants() {
        val tree = listOf(
            folder("work", listOf(folder("ch1", listOf(image("1"))), folder("ch2", listOf(image("1"))))),
            folder("mixed", listOf(image("m1"), folder("sub", listOf(image("s1"))))),
        )
        assertEquals(listOf("work/ch1", "work/ch2"), BrowseRules.leafPathsFor(tree, "work"))
        // mixed folder is itself a leaf for its own images, unshifted first
        assertEquals(listOf("mixed", "mixed/sub"), BrowseRules.leafPathsFor(tree, "mixed"))
        assertEquals(listOf("work/ch1"), BrowseRules.leafPathsFor(tree, "work/ch1"))
    }

    @Test fun leafPathsForFolderWithoutLeafDescendants() {
        // A folder with only non-image-bearing subfolders resolves to itself.
        val tree = listOf(folder("w", listOf(folder("empty", emptyList()))))
        assertEquals(listOf("w"), BrowseRules.leafPathsFor(tree, "w"))
        // Unknown path falls back to the path itself.
        assertEquals(listOf("ghost"), BrowseRules.leafPathsFor(tree, "ghost"))
    }

    // ── progress pct (app.js progressBarHtml) ──

    @Test fun progressPct() {
        assertNull(BrowseRules.progressPct(null, 10))
        assertNull(BrowseRules.progressPct(0, 0))
        assertEquals(10, BrowseRules.progressPct(0, 10))
        assertEquals(50, BrowseRules.progressPct(4, 10))
        assertEquals(100, BrowseRules.progressPct(9, 10))
        assertEquals(100, BrowseRules.progressPct(12, 10)) // clamps
    }
}
