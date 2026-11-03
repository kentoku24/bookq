package dev.bookq.core

import dev.bookq.core.api.TreeNode
import dev.bookq.core.browse.LevelModel
import dev.bookq.core.visibility.VisibilityState
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Level row list (folders → read-child tiles → root self tile) and cover
 * resolution (sprite ≥2 thumbs → thumb → thumbs[0] → none) —
 * `render.js` / `resolveCover.js` parity.
 */
class LevelModelTest {

    private fun folder(
        name: String,
        children: List<TreeNode>? = null,
        thumb: String? = null,
        thumbs: List<String>? = null,
    ) = TreeNode(name = name, type = "folder", children = children,
        thumb = thumb, thumbs = thumbs)

    private fun img(name: String) = TreeNode(name = name, type = "image")

    private fun visibleAll(): VisibilityState = VisibilityState().apply {
        applySnapshot("u", 1, emptyList())
        setStatus(VisibilityState.Status.READY)
    }

    @Test fun foldersFirstThenReadChildrenThenRootSelf() {
        // mixed: images + subfolder → gets a read-child tile after folders.
        val mixed = folder("mixed", listOf(img("1.jpg"), folder("sub", listOf(img("a.jpg")))))
        val plain = folder("plain", listOf(folder("sub", listOf(img("b.jpg")))))
        val roots = listOf(mixed, plain, img("loose.jpg"))
        val items = LevelModel.items("", roots, visibleAll())
        // folders first, then the mixed folder's read-child tile, then
        // the root loose-image self tile — exactly render.js order.
        assertEquals(4, items.size)
        assertEquals("mixed", items[0].path)
        assertEquals("plain", items[1].path)
        assertTrue(items[2] is LevelModel.Item.ReadSelf)
        assertEquals("mixed", items[2].path)
        assertTrue(items[3] is LevelModel.Item.ReadSelf)
        assertEquals("", items[3].path)
        // Inside `mixed`: only its subfolder — the read-self tile for
        // `mixed` itself belongs to the parent level.
        val inner = LevelModel.items("mixed", roots, visibleAll())
        assertEquals(1, inner.size)
        assertTrue(inner[0] is LevelModel.Item.Folder)
        assertEquals("mixed/sub", inner[0].path)
    }

    @Test fun hiddenRootsFilteredFailClosed() {
        val roots = listOf(
            folder("Public", listOf(img("1.jpg"))),
            folder("Secret", listOf(img("1.jpg"))),
        )
        // No snapshot → fail-closed: nothing renders.
        val closed = VisibilityState()
        assertTrue(LevelModel.items("", roots, closed).isEmpty())
        // With snapshot, hidden root drops out.
        val vis = VisibilityState().apply {
            applySnapshot("u", 1, listOf("Secret"))
            setStatus(VisibilityState.Status.READY)
        }
        val items = LevelModel.items("", roots, vis)
        assertEquals(listOf("Public"), items.map { it.path })
    }

    @Test fun coverResolutionOrder() {
        assertEquals(
            LevelModel.Cover.Single("t.jpg"),
            LevelModel.coverOf(folder("f", thumb = "t.jpg")))
        assertEquals(
            LevelModel.Cover.Single("first.jpg"),
            LevelModel.coverOf(folder("f", thumbs = listOf("first.jpg"))))
        // ≥2 thumbs → sprite with the multi-thumb spec.
        val c = LevelModel.coverOf(
            folder("f", thumbs = listOf("a.jpg", "b.jpg", "c.jpg")))
        assertTrue(c is LevelModel.Cover.Sprite)
        assertEquals(listOf(1, 2), (c as LevelModel.Cover.Sprite).spec.rows)
        assertEquals(
            LevelModel.Cover.None,
            LevelModel.coverOf(folder("f")))
    }

    @Test fun filterMatchesNameOrPath() {
        val a = folder("Series", listOf(img("1.jpg")))
        val b = folder("Other", listOf(img("1.jpg")))
        val items = LevelModel.items("", listOf(a, b), visibleAll())
        assertEquals(listOf("Series"),
            LevelModel.filterItems(items, "ser").map { it.path })
        assertEquals(items.size, LevelModel.filterItems(items, "").size)
    }
}
