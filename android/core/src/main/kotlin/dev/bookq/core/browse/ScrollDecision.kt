package dev.bookq.core.browse

/**
 * Port of `app/static/browse/scroll.js` — the per-level scroll-position
 * decision table. JS reads the live DOM and localStorage; here the caller
 * supplies the visible child paths and the set of folders that have saved
 * reading progress, keeping the decision pure and unit-testable.
 *
 * Decision table (evaluated top to bottom):
 *   cause == TOP                       -> top
 *   level == null (root work list)     -> hasSaved ? saved : top
 *   cause == OPEN                      -> last-read child / top (records intent)
 *   anchorIntent matches level         -> same via/anchor, idempotent
 *   hasSaved                           -> saved
 *   a direct child has progress        -> progress (last in list order)
 *   else                               -> top
 *
 * The one-shot `anchorIntent` ({level, anchor, via}) makes the decision
 * idempotent for the same level: Android fires it on reader→browse return
 * and new-folder open; a second evaluation for the same level must not
 * lose to a stale saved position. Cleared on level change, TOP cause,
 * filter changes and reset-to-top (caller's responsibility).
 */
class ScrollDecider {

    enum class Via { TOP, SAVED, PROGRESS, VIEWER }
    enum class Cause { TOP, OPEN, RESTORE }

    data class Decision(val via: Via, val anchor: String?)

    private data class Intent(val level: String, val anchor: String?, val via: Via)

    private var intent: Intent? = null

    /** Record the viewer→browse return anchor (chapter path) for `level`. */
    fun noteViewerReturn(level: String?, chapterPath: String?) {
        if (level.isNullOrEmpty() || chapterPath.isNullOrEmpty()) return
        intent = Intent(level, chapterPath, Via.VIEWER)
    }

    /** Test/debug: the live viewer-return intent, or null. */
    fun peekViewerReturn(): Pair<String, String>? =
        intent?.takeIf { it.via == Via.VIEWER }?.let { it.level to (it.anchor ?: "") }

    fun clear() {
        intent = null
    }

    /**
     * @param level the browse level being rendered (null/"" = root)
     * @param cause TOP = explicit reset, OPEN = fresh drill-in, RESTORE = back/return
     * @param hasSaved caller-declared saved scroll position for this level
     * @param childPaths visible child _fullPaths in list order (post-filter)
     * @param progressFolders folders that have a saved reading position
     */
    fun decide(
        level: String?,
        cause: Cause,
        hasSaved: Boolean,
        childPaths: List<String>,
        progressFolders: Set<String>,
    ): Decision {
        if (cause == Cause.TOP) {
            clear()
            return Decision(Via.TOP, null)
        }
        // An intent for another level expires on sight.
        if (intent != null && intent!!.level != level) clear()
        if (level.isNullOrEmpty()) {
            return if (hasSaved) Decision(Via.SAVED, null) else Decision(Via.TOP, null)
        }

        if (cause == Cause.OPEN) {
            val last = lastReadChild(childPaths, progressFolders)
            intent = Intent(level, last, if (last != null) Via.PROGRESS else Via.TOP)
            return Decision(if (last != null) Via.PROGRESS else Via.TOP, last)
        }

        val i = intent
        if (i != null && i.level == level) {
            if (i.anchor == null || i.anchor in childPaths) return Decision(i.via, i.anchor)
            // Anchor not in the (filtered) list — drop it and fall through.
            clear()
        }
        if (hasSaved) return Decision(Via.SAVED, null)
        val fallback = lastReadChild(childPaths, progressFolders)
        if (fallback != null) return Decision(Via.PROGRESS, fallback)
        return Decision(Via.TOP, null)
    }

    /** The last (list-order) child that has a saved reading position —
     *  `_lastReadChild`: list order is the server's natural sort, so the
     *  last hit is the highest-numbered read episode. */
    private fun lastReadChild(paths: List<String>, progressFolders: Set<String>): String? {
        var found: String? = null
        for (p in paths) if (p in progressFolders) found = p
        return found
    }
}
