package dev.bookq.core.browse

import dev.bookq.core.api.TreeNode

/**
 * Pure browse/lookup rules mirroring `app/static/browse/render.js` and
 * `app/static/offline-core.js` (port of iOS `BrowseModel.swift`).
 * All functions are free of UI state.
 */
object BrowseRules {

    /** Composed absolute path for a node (`parent + "/" + name`, JS `_fullPath`). */
    fun fullPath(parent: String, name: String): String =
        if (parent.isEmpty()) name else "$parent/$name"

    /** `folderHasOnlyImages`: children exist and every child is type "image" —
     *  the predicate that decides whether a tile opens the reader or drills down. */
    fun hasOnlyImages(node: TreeNode): Boolean =
        !node.children.isNullOrEmpty() && node.children.all { it.isImage }

    /** `folderHasImages`: at least one direct image child. Mixed folders
     *  (images + subfolders) are true here, false in [hasOnlyImages]. */
    fun hasImages(node: TreeNode): Boolean =
        node.children?.any { it.isImage } == true

    /** Mixed folder = drills to subfolders but also has its own images
     *  (those get a "read self" tile). */
    fun isMixed(node: TreeNode): Boolean = hasImages(node) && !hasOnlyImages(node)

    /** Look up a node by composed path — mirrors `findNodeByPath`:
     *  node names may contain "/" (flattening) and are NEVER split;
     *  match on the composed full path only. */
    fun nodeAt(path: String, roots: List<TreeNode>): TreeNode? {
        if (path.isEmpty()) return null
        for (node in roots) {
            if (node.name == path) return node
        }
        for (node in roots) {
            if (path.startsWith(node.name + "/")) {
                find(path, node.children ?: emptyList(), node.name)?.let { return it }
            }
        }
        return null
    }

    private fun find(path: String, nodes: List<TreeNode>, parent: String): TreeNode? {
        for (node in nodes) {
            val full = fullPath(parent, node.name)
            if (full == path) return node
            if (path.startsWith("$full/")) {
                find(path, node.children ?: emptyList(), full)?.let { return it }
            }
        }
        return null
    }

    /** Folder children of a level (drill targets, in tree order). */
    fun foldersAt(path: String, roots: List<TreeNode>): List<Pair<String, TreeNode>> {
        val children = if (path.isEmpty()) roots else (nodeAt(path, roots)?.children ?: emptyList())
        return children.filter { it.isFolder }.map { fullPath(path, it.name) to it }
    }

    /** "このフォルダを読む" tiles for a level: each MIXED child gets a
     *  read-self tile (data-read-child), plus at root ONE tile covering
     *  loose root images (data-read-self, path ""). */
    fun selfTiles(path: String, roots: List<TreeNode>): List<Pair<String, TreeNode>> {
        if (path.isEmpty()) {
            val hasLoose = roots.any { it.isImage }
            return if (hasLoose) listOf("" to TreeNode(name = "", type = "folder")) else emptyList()
        }
        return foldersAt(path, roots).filter { isMixed(it.second) }
    }

    /** Whether tapping this folder drills down vs opens the reader. */
    fun drillsDown(node: TreeNode): Boolean = !hasOnlyImages(node)

    /** Label shown on read-self / read-child tiles (render.js `_readChildLabel`). */
    const val READ_SELF_LABEL = "このフォルダを読む"

    // ── Multi-thumb sprite layout (render.js `multiThumbSpec`) ──

    data class ThumbSpec(val rows: List<Int>, val cols: Int)

    /** Row layout for a multi-thumb tile — same table the server sprite uses. */
    fun multiThumbSpec(count: Int): ThumbSpec? = when (minOf(count, 9)) {
        2 -> ThumbSpec(rows = listOf(2), cols = 2)
        3 -> ThumbSpec(rows = listOf(1, 2), cols = 2)
        4 -> ThumbSpec(rows = listOf(2, 2), cols = 2)
        5 -> ThumbSpec(rows = listOf(2, 3), cols = 6)
        6 -> ThumbSpec(rows = listOf(2, 2, 2), cols = 2)
        7 -> ThumbSpec(rows = listOf(2, 3, 2), cols = 6)
        8 -> ThumbSpec(rows = listOf(3, 2, 3), cols = 6)
        9 -> ThumbSpec(rows = listOf(3, 3, 3), cols = 3)
        else -> null
    }

    /** Split "parent/leaf" display names (flattened folders show parent dimmed).
     *  Splits at the LAST "/" only — the leaf keeps everything after it. */
    fun splitDisplayName(name: String): Pair<String, String?> {
        val i = name.lastIndexOf('/')
        return if (i == -1) name to null else name.substring(i + 1) to name.substring(0, i)
    }

    /** In-level filter: lowercase substring on name OR full path
     *  (iOS `BrowseModel.filter`; web has no equivalent — iOS `.searchable`). */
    fun <T> filter(entries: List<Pair<String, T>>, query: String, name: (T) -> String): List<Pair<String, T>> {
        val q = query.lowercase()
        if (q.isEmpty()) return entries
        return entries.filter { (path, node) ->
            name(node).lowercase().contains(q) || path.lowercase().contains(q)
        }
    }

    /**
     * Leaf folders for offline/download enumeration — port of
     * `storeAdapter.js computeLeafPathsFor` (NOT iOS `leafPaths`, which
     * drops a mixed folder's own images):
     *  - only-images node → [folder]
     *  - else strictly-only-images folder descendants; a mixed folder
     *    (direct images + subfolders) unshifts ITSELF as the leaf for its
     *    own images; no leaf descendants at all → [folder].
     */
    fun leafPathsFor(roots: List<TreeNode>, folder: String): List<String> {
        val node = nodeAt(folder, roots)
        if (node != null) {
            if (hasOnlyImages(node)) return listOf(folder)
            val tmp = mutableListOf<String>()
            for (c in node.children.orEmpty()) {
                if (c.isFolder) collectLeafPaths(c, fullPath(folder, c.name), tmp)
            }
            if (tmp.isEmpty()) return listOf(folder)
            val dedup = tmp.distinct()
            val hasDirectImages = node.children?.any { it.isImage } == true
            return if (hasDirectImages) listOf(folder) + dedup else dedup
        }
        // Node not found — web falls back to a whole-tree prefix walk.
        val leaves = mutableListOf<String>()
        fun walk(nodes: List<TreeNode>, parent: String) {
            for (n in nodes) {
                val full = fullPath(parent, n.name)
                if (full == folder || full.startsWith("$folder/")) {
                    if (hasOnlyImages(n)) leaves.add(full)
                }
                walk(n.children ?: emptyList(), full)
            }
        }
        walk(roots, "")
        return if (leaves.isEmpty()) listOf(folder) else leaves.distinct()
    }

    private fun collectLeafPaths(node: TreeNode, full: String, out: MutableList<String>) {
        if (hasOnlyImages(node)) {
            out.add(full)
            return
        }
        node.children?.forEach { c ->
            if (c.isFolder) collectLeafPaths(c, fullPath(full, c.name), out)
        }
    }

    /** Progress bar percentage — app.js `progressBarHtml`:
     *  `min(100, round((page+1)/total * 100))`, null when no saved position. */
    fun progressPct(page: Int?, total: Int?): Int? {
        if (page == null || total == null || total <= 0) return null
        return minOf(100, Math.round((page + 1) / total.toFloat() * 100f))
    }
}
