package dev.bookq.core.offline

import dev.bookq.core.api.TreeNode
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class OfflineRulesTest {

    private fun img(name: String) = TreeNode(name = name, type = "image")
    private fun dir(name: String, children: List<TreeNode>? = null, thumb: String? = null) =
        TreeNode(name = name, type = "folder", children = children, thumb = thumb)

    private fun leaf(path: String, vararg names: String) =
        dir(path.substringAfterLast('/'), names.map(::img))

    // ── leaf enumeration (collectLeafPaths / getLeafPathsFor) ──

    @Test
    fun `only-images folder is its own leaf`() {
        val roots = listOf(dir("Series", listOf(
            leaf("v1", "1.jpg", "2.jpg"),
            leaf("v2", "1.jpg"))))
        assertEquals(listOf("Series/v1"),
            OfflineRules.leafPaths("Series/v1", roots))
    }

    @Test
    fun `mixed parent is a leaf for its own images, listed first`() {
        val roots = listOf(dir("Series", listOf(
            img("cover.jpg"),
            leaf("v1", "1.jpg"),
            leaf("v2", "1.jpg"))))
        assertEquals(listOf("Series", "Series/v1", "Series/v2"),
            OfflineRules.leafPaths("Series", roots))
    }

    @Test
    fun `folder with no images anywhere still resolves to itself`() {
        val roots = listOf(dir("Empty", listOf(dir("Sub", emptyList()))))
        assertEquals(listOf("Empty"), OfflineRules.leafPaths("Empty", roots))
    }

    @Test
    fun `unknown path degrades to itself`() {
        assertEquals(listOf("Nope"), OfflineRules.leafPaths("Nope", emptyList()))
    }

    @Test
    fun `flattened child names with slashes keep their path`() {
        // single-child flattening: "a/b" is ONE node whose name contains /
        val roots = listOf(dir("Series", listOf(
            dir("deep/nest", listOf(img("1.jpg"), img("2.jpg"))))))
        assertEquals(listOf("Series/deep/nest"),
            OfflineRules.leafPaths("Series", roots))
    }

    @Test
    fun `findNode descends through flattened names`() {
        val roots = listOf(dir("a", listOf(
            dir("b/c", listOf(img("1.jpg"))))))
        val n = OfflineRules.findNode("a/b/c", roots)
        assertTrue(OfflineRules.folderHasOnlyImages(n))
    }

    // ── leafMost / dedupedBytes ──

    @Test
    fun `leafMost drops ancestors of other leaves`() {
        val leaves = listOf("P", "P/v1", "P/v2", "Q")
        assertEquals(listOf("P/v1", "P/v2", "Q"), OfflineRules.leafMost(leaves))
    }

    @Test
    fun `dedupedBytes skips records having a descendant record`() {
        val recs = mapOf("P" to 100L, "P/v1" to 500L, "Q" to 50L)
        assertEquals(550L,
            OfflineRules.dedupedBytes(recs.keys.toList()) { recs[it]!! })
    }

    // ── progress + state machine ──

    private fun st(p: Int, c: Int) = OfflineRules.LeafState(p, c)

    @Test
    fun `partial progress sums leaf-most records`() {
        val recs = mapOf("P" to st(1, 1), "P/v1" to st(4, 2), "P/v2" to st(2, 0))
        val prog = OfflineRules.partialProgress(
            listOf("P", "P/v1", "P/v2")) { recs[it] }
        // leafMost drops P → 2/6 over the children
        assertEquals(2, prog.cachedCount)
        assertEquals(6, prog.pageCount)
    }

    @Test
    fun `partial progress falls back to leaf count without records`() {
        val prog = OfflineRules.partialProgress(listOf("a/b", "c/d")) { null }
        assertEquals(0, prog.cachedCount)
        assertEquals(2, prog.pageCount)
    }

    @Test
    fun `isFolderOffline needs every leaf complete`() {
        val recs = mapOf("P/v1" to st(2, 2), "P/v2" to st(2, 1))
        assertFalse(OfflineRules.isFolderOffline("P", listOf("P/v1", "P/v2")) {
            recs[it]?.let { r -> r.pageCount > 0 && r.cachedCount >= r.pageCount } == true
        })
        val recs2 = mapOf("P/v1" to st(2, 2), "P/v2" to st(2, 2))
        assertTrue(OfflineRules.isFolderOffline("P", listOf("P/v1", "P/v2")) {
            recs2[it]?.let { r -> r.pageCount > 0 && r.cachedCount >= r.pageCount } == true
        })
    }

    @Test
    fun `btnState precedence - transient flags win`() {
        val none: (String) -> OfflineRules.LeafState? = { null }
        val lvs = listOf("f")
        assertEquals(OfflineRules.BtnState.CONFIRM_DELETE, OfflineRules.btnState(
            "f", lvs, none, confirmArmed = true, deleting = true,
            stopping = true, downloading = true, listing = true))
        assertEquals(OfflineRules.BtnState.DELETING, OfflineRules.btnState(
            "f", lvs, none, confirmArmed = false, deleting = true,
            stopping = true, downloading = true, listing = true))
        assertEquals(OfflineRules.BtnState.STOPPING, OfflineRules.btnState(
            "f", lvs, none, confirmArmed = false, deleting = false,
            stopping = true, downloading = true, listing = true))
        assertEquals(OfflineRules.BtnState.DOWNLOADING, OfflineRules.btnState(
            "f", lvs, none, confirmArmed = false, deleting = false,
            stopping = false, downloading = true, listing = true))
        assertEquals(OfflineRules.BtnState.LISTING, OfflineRules.btnState(
            "f", lvs, none, confirmArmed = false, deleting = false,
            stopping = false, downloading = false, listing = true))
    }

    @Test
    fun `btnState derived - idle, partial, complete`() {
        val recs = mapOf("f" to st(4, 2), "g" to st(4, 4))
        val of = { f: String -> recs[f] }
        assertEquals(OfflineRules.BtnState.PARTIAL, OfflineRules.btnState(
            "f", listOf("f"), of, false, false, false, false, false))
        assertEquals(OfflineRules.BtnState.COMPLETE, OfflineRules.btnState(
            "g", listOf("g"), of, false, false, false, false, false))
        assertEquals(OfflineRules.BtnState.IDLE, OfflineRules.btnState(
            "x", listOf("x"), of, false, false, false, false, false))
    }

    // ── eviction / fmt / encoding ──

    @Test
    fun `evictOrder sorts by lastReadAt then addedAt`() {
        val read = mapOf("new" to 200L, "old" to 10L, "mid" to 100L)
        assertEquals(listOf("old", "mid", "new"),
            OfflineRules.evictOrder(read.keys.toList()) { read[it]!! })
    }

    @Test
    fun `fmt matches web`() {
        assertEquals("512.0 MB", OfflineRules.fmt(512L * 1000 * 1000))
        assertEquals("1.5 GB", OfflineRules.fmt(1_500_000_000L))
    }

    @Test
    fun `encFolder is injective and filesystem-safe`() {
        val a = OfflineRules.encFolder("Series/第1話")
        val b = OfflineRules.encFolder("Series%2F第1話")
        assertTrue(a != b)
        assertFalse(a.contains('/'))
    }
}
