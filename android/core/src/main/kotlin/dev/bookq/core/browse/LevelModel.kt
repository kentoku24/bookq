package dev.bookq.core.browse

import dev.bookq.core.api.TreeNode
import dev.bookq.core.visibility.VisibilityState

/**
 * One browse level's render model — the row list `render.js` emits plus
 * cover resolution (`thumbs/resolveCover.js`). Pure: no UI, no IO.
 *
 * Row order (web contract): folder tiles first (visibility-filtered, tree
 * order), then one read-child tile per MIXED child, then — root only —
 * the single read-self tile for loose root images.
 */
object LevelModel {

    sealed interface Item {
        val path: String
        val node: TreeNode

        /** A real folder tile — drills down or opens the reader. */
        data class Folder(override val path: String, override val node: TreeNode) : Item

        /** "このフォルダを読む" — mixed child's own images (`data-read-child`)
         *  or the root loose-images tile (`data-read-self`, path ""). */
        data class ReadSelf(override val path: String, override val node: TreeNode) : Item
    }

    /** Cover for a tile — mirrors `renderTileImg`: sprite when
     *  `thumbs.size >= 2` and a layout exists, else `thumb` → `thumbs[0]`,
     *  else nothing (placeholder). */
    sealed interface Cover {
        data class Single(val name: String) : Cover
        data class Sprite(val names: List<String>, val spec: BrowseRules.ThumbSpec) : Cover
        data object None : Cover
    }

    fun coverOf(node: TreeNode): Cover {
        val thumbs = node.thumbs
        if (thumbs != null && thumbs.size >= 2) {
            val spec = BrowseRules.multiThumbSpec(thumbs.size)
            if (spec != null) return Cover.Sprite(thumbs.take(9), spec)
        }
        node.thumb?.let { return Cover.Single(it) }
        thumbs?.firstOrNull()?.let { return Cover.Single(it) }
        return Cover.None
    }

    /**
     * The row list for `path` ("" = root), visibility-filtered.
     * Fail-closed: without a visibility snapshot `vis.isVisible` denies
     * everything, so the caller must gate on `vis.blockedGrid()` first.
     */
    fun items(path: String, roots: List<TreeNode>, vis: VisibilityState): List<Item> {
        val folders = BrowseRules.foldersAt(path, roots)
            .filter { vis.isVisible(it.first) }
        val out = ArrayList<Item>(folders.size + 2)
        for ((p, n) in folders) out += Item.Folder(p, n)
        // Read-child tiles AFTER folder tiles, at every level — each mixed
        // child (direct images + subfolders) gets one for its own images.
        for ((p, n) in folders) {
            if (BrowseRules.isMixed(n)) out += Item.ReadSelf(p, n)
        }
        if (path.isEmpty()) {
            // Root loose images → ONE self tile, always last.
            if (roots.any { it.isImage }) {
                out += Item.ReadSelf("", TreeNode(name = "", type = "folder"))
            }
        }
        return out
    }

    /** In-level text filter on the row list (iOS `.searchable` parity:
     *  name or composed path, case-insensitive substring). */
    fun filterItems(items: List<Item>, query: String): List<Item> {
        val q = query.trim().lowercase()
        if (q.isEmpty()) return items
        return items.filter { item ->
            when (item) {
                is Item.ReadSelf ->
                    // "このフォルダを読む" tiles match their folder path only.
                    item.path.lowercase().contains(q)
                is Item.Folder -> {
                    val (leaf, _) = BrowseRules.splitDisplayName(item.node.name)
                    leaf.lowercase().contains(q) || item.path.lowercase().contains(q)
                }
            }
        }
    }
}
