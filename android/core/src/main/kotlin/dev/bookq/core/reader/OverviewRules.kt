package dev.bookq.core.reader

/**
 * Overview grid index mapping — overview.js buildGrid. The grid is a
 * flat item stream in reading order: a header entry per chapter
 * (cross-chapter works only) followed by the chapter's pages. Page
 * entries carry the GLOBAL image index; header entries carry the
 * chapter index. Chapter c's images sit at `boundaries[c] + local`
 * and are all real — spacers precede boundaries, they own no cell.
 */
object OverviewRules {
    /**
     * Flat items: `(isHeader, chapterIdx | globalIdx)` in grid order.
     * Headers exist only when [ReaderWork.isCrossChapter].
     */
    fun flatItems(work: ReaderWork): List<Pair<Boolean, Int>> {
        val out = ArrayList<Pair<Boolean, Int>>(work.images.size)
        work.chapters.forEachIndexed { ci, ch ->
            if (work.isCrossChapter) out.add(true to ci)
            val start = work.boundaries.getOrElse(ci) { 0 }
            for (l in ch.images.indices) out.add(false to start + l)
        }
        return out
    }

    /**
     * Flat index of the page cell nearest [currentIdx] — web
     * `nearestRealPos`: the first real page at/after the index; a
     * trailing position maps to the last cell. -1 for an empty grid.
     */
    fun nearestItem(flat: List<Pair<Boolean, Int>>, currentIdx: Int): Int {
        var best = -1
        for (i in flat.indices) {
            val (h, v) = flat[i]
            if (h) continue
            if (v >= currentIdx) return i
            best = i
        }
        return best
    }

    /** Real (non-spacer) global indices — overview.js `realList()`. */
    fun realIndices(work: ReaderWork): List<Int> =
        work.images.indices.filter { !work.images[it].isSpacer }

    /**
     * IMG_WINDOW band around [currentIdx] — overview.js `±200 real
     * pages`: returns the [lo, hi] inclusive slice of [realIndices].
     * Empty input → -1..-1.
     */
    fun imgWindow(
        reals: List<Int>,
        currentIdx: Int,
        radius: Int = 200,
    ): IntRange {
        if (reals.isEmpty()) return -1..-1
        val pos = reals.indexOfFirst { it >= currentIdx }
            .let { if (it < 0) reals.lastIndex else it }
        val lo = (pos - radius).coerceAtLeast(0)
        val hi = (pos + radius).coerceAtMost(reals.lastIndex)
        return lo..hi
    }
}
