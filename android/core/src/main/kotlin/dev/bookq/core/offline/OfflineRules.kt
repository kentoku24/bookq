package dev.bookq.core.offline

import dev.bookq.core.api.TreeNode
import java.net.URLEncoder

/**
 * Offline-download rules — the Android port of `offline/storeAdapter.js`
 * (`collectLeafPaths`, `getOfflineBtnState`, `getPartialProgressCtx`,
 * `isFolderOfflineCtx`, `_leafMost`) plus `offline-core.js`
 * (`dedupedBytes`, `fmt`). Pure: no UI, no IO.
 */
object OfflineRules {

    /** Tile badge states — web `OfflineBtnState` verbatim plus LISTING
     *  (`_listingSet`, spinner while leaf listings are in flight). */
    enum class BtnState {
        IDLE, LISTING, DOWNLOADING, STOPPING,
        PARTIAL, COMPLETE, CONFIRM_DELETE, DELETING,
    }

    /** Per-leaf persisted state — the record fields btn-state derives read. */
    data class LeafState(val pageCount: Int, val cachedCount: Int)

    fun folderHasOnlyImages(node: TreeNode?): Boolean =
        node != null && !node.children.isNullOrEmpty() &&
            node.children.all { it.isImage }

    fun folderHasImages(node: TreeNode?): Boolean =
        node != null && node.children.orEmpty().any { it.isImage }

    /**
     * Locate the node at `path` — child paths are `parent/name`, and `name`
     * may itself contain "/" (single-child flattening), so descent compares
     * computed paths, never splits the target.
     */
    fun findNode(path: String, roots: List<TreeNode>): TreeNode? {
        fun walk(nodes: List<TreeNode>, prefix: String): TreeNode? {
            for (n in nodes) {
                val p = if (prefix.isEmpty()) n.name else "$prefix/${n.name}"
                if (p == path) return n
                if (path.startsWith("$p/") && n.isFolder) {
                    walk(n.children.orEmpty(), p)?.let { return it }
                }
            }
            return null
        }
        return if (path.isEmpty()) null else walk(roots, "")
    }

    /**
     * Leaf paths for `path` — `getLeafPathsFor`: an only-images folder is its
     * own leaf; a mixed folder contributes its leaf descendants AND itself
     * first when it directly owns images. Unknown nodes degrade to [path].
     */
    fun leafPaths(path: String, roots: List<TreeNode>): List<String> {
        val node = findNode(path, roots) ?: return listOf(path)
        if (folderHasOnlyImages(node)) return listOf(path)
        val tmp = mutableListOf<String>()
        for (c in node.children.orEmpty()) {
            if (c.isFolder) collectLeafPaths("$path/${c.name}", c, tmp)
        }
        if (tmp.isEmpty()) return listOf(path)
        val dedup = tmp.distinct()
        return if (folderHasImages(node)) listOf(path) + dedup else dedup
    }

    private fun collectLeafPaths(path: String, node: TreeNode, out: MutableList<String>) {
        if (folderHasOnlyImages(node)) { out += path; return }
        for (c in node.children.orEmpty()) {
            if (c.isFolder) collectLeafPaths("$path/${c.name}", c, out)
        }
    }

    /** Deepest leaves only — web `_leafMost`: drop any leaf that is an
     *  ancestor of another leaf (progress/size count children once). */
    fun leafMost(leaves: List<String>): List<String> {
        val ancestors = HashSet<String>()
        for (lp in leaves) {
            var s = lp.indexOf('/')
            while (s >= 0) {
                ancestors += lp.substring(0, s)
                s = lp.indexOf('/', s + 1)
            }
        }
        return leaves.filter { it !in ancestors }
    }

    data class Progress(val cachedCount: Int, val pageCount: Int) {
        val pct: Float get() = if (pageCount > 0) cachedCount.toFloat() / pageCount else 0f
    }

    /** `getPartialProgressCtx` — sums leaf-most records; falls back to a
     *  per-leaf count when no record carries page counts yet. */
    fun partialProgress(
        leaves: List<String>,
        recordOf: (String) -> LeafState?,
    ): Progress {
        val top = leafMost(leaves)
        var cached = 0
        var total = 0
        var cachedLeaves = 0
        for (lp in top) {
            val rec = recordOf(lp)
            if (rec != null) {
                cached += rec.cachedCount
                total += rec.pageCount
                if (rec.cachedCount > 0) cachedLeaves++
            }
        }
        if (total == 0) return Progress(cachedLeaves, top.size)
        return Progress(cached, total)
    }

    /** `isFolderOfflineCtx` — the folder itself, or every leaf, complete. */
    fun isFolderOffline(
        folder: String,
        leaves: List<String>,
        completeOf: (String) -> Boolean,
    ): Boolean {
        if (completeOf(folder)) return true
        return if (leaves.size > 1 || (leaves.size == 1 && leaves[0] != folder)) {
            leaves.isNotEmpty() && leaves.all(completeOf)
        } else {
            false
        }
    }

    /** `getOfflineBtnStateCtx` — transient flags win over record-derived
     *  PARTIAL/COMPLETE/IDLE (mirrors the web set lookups' precedence). */
    fun btnState(
        folder: String,
        leaves: List<String>,
        recordOf: (String) -> LeafState?,
        confirmArmed: Boolean,
        deleting: Boolean,
        stopping: Boolean,
        downloading: Boolean,
        listing: Boolean,
    ): BtnState = when {
        confirmArmed -> BtnState.CONFIRM_DELETE
        deleting -> BtnState.DELETING
        stopping -> BtnState.STOPPING
        downloading -> BtnState.DOWNLOADING
        listing -> BtnState.LISTING
        else -> {
            val prog = partialProgress(leaves, recordOf)
            when {
                prog.pageCount > 0 && prog.pct > 0f && prog.pct < 1f -> BtnState.PARTIAL
                isFolderOffline(folder, leaves) { lp ->
                    recordOf(lp)?.let { it.pageCount > 0 && it.cachedCount >= it.pageCount } == true
                } -> BtnState.COMPLETE
                else -> BtnState.IDLE
            }
        }
    }

    /** `dedupedBytes` — sum of record bytes, skipping records that have a
     *  descendant record (already counted via the child). */
    fun dedupedBytes(folders: List<String>, bytesOf: (String) -> Long): Long {
        val set = folders.toSet()
        var sum = 0L
        for (w in folders) {
            val isParent = set.any { it != w && it.startsWith("$w/") }
            if (!isParent) sum += bytesOf(w)
        }
        return sum
    }

    /** Eviction order — `getOldest`: `lastReadAt ?: addedAt` ascending. */
    fun evictOrder(folders: List<String>, readAtOf: (String) -> Long): List<String> =
        folders.sortedBy { readAtOf(it) }

    /** `fmt` — "123.4 MB" / "1.2 GB". */
    fun fmt(bytes: Long): String =
        if (bytes < 1_000_000_000L) "%.1f MB".format(bytes / 1e6)
        else "%.1f GB".format(bytes / 1e9)

    /** `enc(folder)` — URL-encode the whole path for the per-work dir name;
     *  injective so distinct folders can never collide on disk. */
    fun encFolder(folder: String): String =
        URLEncoder.encode(folder, Charsets.UTF_8.name())
}
