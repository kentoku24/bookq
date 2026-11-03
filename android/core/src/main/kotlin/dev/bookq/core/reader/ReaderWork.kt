package dev.bookq.core.reader

import dev.bookq.core.api.TreeNode
import dev.bookq.core.browse.BrowseRules

/**
 * Pure reader model — chapter assembly + spread construction, ported from
 * app.js `loadImages` (assembly), app.js `getSiblingLeafFolders` /
 * `getCurrentChapterInfo`, and viewer/spread.js `rebuildSpreads`.
 * Byte-faithful to iOS `ReaderWork` except where noted: web gates
 * cross-chapter autoload on `SIBLING_AUTOLOAD_MIN_DEPTH = 2` — a depth-2
 * path ("a/b") never merges sibling leaves; iOS drops that gate. Web wins.
 */
data class PageRef(
    val name: String,
    /** Leaf folder path this page lives in. */
    val folder: String,
    val isSpacer: Boolean = false,
) {
    companion object {
        val SPACER = PageRef("__spacer__", "", isSpacer = true)
    }
}

data class ReaderChapter(
    val name: String,
    val fullPath: String,
    /** Real (non-spacer) pages. */
    val images: List<PageRef>,
)

enum class ReaderPageMode { SINGLE, SPREAD }

/** Tap-zone verdict — app.js `zoneActionAt`. Edge 30% zones turn pages in
 *  the direction [tapForwardLeft] selects (web default: left = forward),
 *  the center band toggles chrome. */
sealed interface ZoneAction {
    /** +1 forward, -1 back. */
    data class Turn(val delta: Int) : ZoneAction
    data object ToggleChrome : ZoneAction
}

fun zoneAction(frac: Float, tapForwardLeft: Boolean): ZoneAction = when {
    frac < 0.30f -> ZoneAction.Turn(if (tapForwardLeft) 1 else -1)
    frac > 0.70f -> ZoneAction.Turn(if (tapForwardLeft) -1 else 1)
    else -> ZoneAction.ToggleChrome
}

data class ReaderWork(
    val clickedPath: String,
    /** Global, including spacers. */
    val images: List<PageRef>,
    val chapters: List<ReaderChapter>,
    /** Global index where each chapter starts. */
    val boundaries: List<Int>,
    /** -1 for spacers. */
    val globalToReal: List<Int>,
    val realCount: Int,
    /** Per-spread page indices (non-spacer only). */
    val spreads: List<List<Int>>,
    /** Global idx → spread index (-1 spacer). */
    val idxToSpread: List<Int>,
    /** Spreads flagged chapterStart (gap labels). */
    val chapterStartSpreads: Set<Int>,
    val pageMode: ReaderPageMode,
    /** Current pairing offset (0 or 1) — kept for rebuild labels. */
    val spreadOffset: Int = 0,
) {
    val isCrossChapter: Boolean get() = chapters.size > 1

    /** Which chapter a global index belongs to — `getCurrentChapterInfo`. */
    fun chapterInfo(globalIdx: Int): ChapterInfo {
        var ci = 0
        if (boundaries.size > 1) {
            for (i in 1 until boundaries.size) {
                if (globalIdx >= boundaries[i]) ci = i else break
            }
        }
        val b = boundaries.getOrElse(ci) { 0 }
        val name = chapters.getOrElse(ci) { null }?.name ?: ""
        return ChapterInfo(ci, name, globalIdx - b)
    }

    data class ChapterInfo(val chapterIdx: Int, val name: String, val localIdx: Int)

    /** Display index (real pages only) — `globalToRealIdx`. */
    fun realIndex(globalIdx: Int): Int =
        globalToReal.getOrElse(globalIdx) { -1 }

    /** Chapter-local page index clamped to that chapter's image range
     *  (`saved.page` semantics — always a real page). */
    fun resumeGlobalIndex(chapterIdx: Int, localIdx: Int): Int {
        val ci = if (chapterIdx in boundaries.indices) chapterIdx else 0
        val b = boundaries[ci]
        val count = chapters[ci].images.size
        val local = if (localIdx >= 0 && localIdx < count) localIdx else 0
        return b + local
    }

    /** `spreadOf` — spread containing global index. */
    fun spreadOf(idx: Int): Int =
        idxToSpread.getOrElse(idx) { -1 }.coerceAtLeast(0)

    /** Next/prev spread navigation — `goNext`/`goPrev`. */
    fun nextSpreadFirstPage(afterGlobalIdx: Int): Int? {
        val s = spreadOf(afterGlobalIdx)
        return spreads.getOrNull(s + 1)?.first()
    }

    fun prevSpreadFirstPage(beforeGlobalIdx: Int): Int? {
        val s = spreadOf(beforeGlobalIdx)
        return if (s > 0) spreads.getOrNull(s - 1)?.first() else null
    }

    /** Spread range of the chapter containing [globalIdx] — the web's
     *  `getChapterSpreadRange` (no-cross turns clamp here): lo is the
     *  spread of the chapter's first image, hi the spread of its last
     *  (a boundary image can share a spread with the previous chapter). */
    fun chapterSpreadRange(globalIdx: Int): IntRange {
        if (spreads.isEmpty()) return 0..0
        val ci = chapterInfo(globalIdx).chapterIdx
        if (boundaries.size <= 1) return 0 until spreads.size
        val start = boundaries[ci]
        val end = if (ci + 1 < boundaries.size) boundaries[ci + 1] else images.size
        var firstReal = -1
        var lastReal = -1
        for (i in start until end) {
            if (!images[i].isSpacer) {
                if (firstReal < 0) firstReal = i
                lastReal = i
            }
        }
        if (firstReal < 0) {
            val s = spreadOf(minOf(start, (images.size - 1).coerceAtLeast(0)))
            return s..s
        }
        return spreadOf(firstReal)..spreadOf(lastReal)
    }

    /** New work after a mode/offset change (runtime toggle — no re-pad,
     *  matching the web where spacers are fixed at load). */
    fun withMode(mode: ReaderPageMode, offset: Int): ReaderWork =
        copy(pageMode = mode, spreadOffset = offset).rebuildSpreads(offset)

    private fun rebuildSpreads(offset: Int): ReaderWork {
        val groups = mutableListOf<List<Int>>()
        val n = images.size
        if (pageMode == ReaderPageMode.SPREAD) {
            var i = 0
            while (i < offset && i < n) { groups.add(listOf(i)); i++ }
            while (i < n) {
                val g = if (i + 1 < n) listOf(i, i + 1) else listOf(i)
                groups.add(g)
                i += g.size
            }
        } else {
            for (i in 0 until n) groups.add(listOf(i))
        }
        val spreads = mutableListOf<List<Int>>()
        val i2s = MutableList(n) { -1 }
        val starts = mutableSetOf<Int>()
        for (g in groups) {
            val pages = g.filter { !images[it].isSpacer }
            if (pages.isEmpty()) continue
            val s = spreads.size
            for (p in pages) i2s[p] = s
            if (boundaries.size > 1 && boundaries.indexOf(pages[0]) > 0) {
                starts.add(s)
            }
            spreads.add(pages)
        }
        return copy(spreads = spreads, idxToSpread = i2s,
            chapterStartSpreads = starts)
    }

    companion object {
        /** app.js `SIBLING_AUTOLOAD_MIN_DEPTH` — sibling autoload only
         *  applies to paths at depth ≥ 3 (e.g. `manga/series/v01`). */
        const val SIBLING_AUTOLOAD_MIN_DEPTH = 2

        /** Sibling leaf folders of [path] — `getSiblingLeafFolders`.
         *  Returns the parent's `hasOnlyImages` children with composed
         *  paths, or null when autoload does not apply (shallow path,
         *  root-level, or fewer than 2 leaf siblings). */
        fun siblingLeaves(path: String, roots: List<TreeNode>): List<Pair<TreeNode, String>>? {
            if (path.split('/').size - 1 < SIBLING_AUTOLOAD_MIN_DEPTH) return null
            // findParentOf(tree, fullPath): locate the node whose child
            // path-composition equals `path`.
            fun walk(nodes: List<TreeNode>, parent: String): List<Pair<TreeNode, String>>? {
                for (n in nodes.filter { it.isFolder }) {
                    val full = BrowseRules.fullPath(parent, n.name)
                    for (c in n.children ?: emptyList()) {
                        if (BrowseRules.fullPath(full, c.name) == path) {
                            return (n.children ?: emptyList()).map { it to BrowseRules.fullPath(full, it.name) }
                        }
                    }
                    walk(n.children ?: emptyList(), full)?.let { return it }
                }
                return null
            }
            val siblings = walk(roots, "") ?: return null
            val leaves = siblings.filter { (n, _) -> n.isFolder && BrowseRules.hasOnlyImages(n) }
            return if (leaves.size >= 2) leaves else null
        }

        /** Chapter list construction — mirrors loadImages' sibling/mixed/
         *  fallback branching. The plain fallback returns a chapter with
         *  empty `images`; the caller fills it from `/api/v1/images`. */
        fun chapters(path: String, roots: List<TreeNode>): Pair<List<ReaderChapter>, Boolean> {
            val selfNode = BrowseRules.nodeAt(path, roots)
            val isSelfMixed = selfNode?.let { BrowseRules.isMixed(it) } == true
            val leaves = if (isSelfMixed) null else siblingLeaves(path, roots)

            if (leaves != null) {
                return leaves.map { (node, full) ->
                    ReaderChapter(
                        name = node.name,
                        fullPath = full,
                        images = (node.children ?: emptyList())
                            .filter { it.isImage }
                            .map { PageRef(it.name, full) },
                    )
                } to true
            }

            val leafName = BrowseRules.splitDisplayName(path).first
            // Leaf (images only) or mixed folder: read the direct image
            // children — web reads mixed folders' direct images, never
            // merging sibling leaves for them.
            if (selfNode != null &&
                (selfNode.children ?: emptyList()).any { it.isImage }) {
                return listOf(ReaderChapter(leafName, path,
                    (selfNode.children ?: emptyList())
                        .filter { it.isImage }
                        .map { PageRef(it.name, path) })) to false
            }
            // Plain fallback — caller fills images from /api/v1/images.
            return listOf(ReaderChapter(leafName, path, emptyList())) to false
        }

        /** Assemble the work: boundaries + spacer padding (cross-chapter
         *  only, at load like the web) + real-index maps + spreads. */
        fun assemble(
            clickedPath: String,
            chapters: List<ReaderChapter>,
            pageMode: ReaderPageMode,
            spreadOffset: Int,
        ): ReaderWork {
            val images = mutableListOf<PageRef>()
            val boundaries = mutableListOf<Int>()
            if (chapters.size > 1) {
                for (ch in chapters) {
                    boundaries.add(images.size)
                    images.addAll(ch.images)
                }
                // Deliberate divergence from app.js: web computes
                // `startIdx = boundaries[c] + spacersSoFar` — double-
                // counting earlier pads so later chapters can land on
                // odd boundaries (and offset=1 leaves c0 unpadded).
                // Here every chapter boundary pads to pair alignment on
                // the POST-shift index — a chapter always opens on its
                // own right page, which is the intent of the spacers.
                val spreadOff = if (pageMode == ReaderPageMode.SPREAD) spreadOffset else 0
                for (c in chapters.indices) {
                    val startIdx = boundaries[c]
                    val pad = (startIdx - spreadOff + 2) % 2
                    if (pad > 0) {
                        images.add(boundaries[c], PageRef.SPACER)
                        for (j in c + 1 until boundaries.size) boundaries[j] += pad
                        boundaries[c] += pad
                    }
                }
            } else {
                images.addAll(chapters.firstOrNull()?.images ?: emptyList())
                boundaries.add(0)
            }

            val g2r = mutableListOf<Int>()
            var spacerCount = 0
            for (img in images) {
                if (img.isSpacer) {
                    g2r.add(-1); spacerCount++
                } else {
                    g2r.add(g2r.size - spacerCount)
                }
            }

            return ReaderWork(
                clickedPath = clickedPath,
                images = images,
                chapters = chapters,
                boundaries = boundaries,
                globalToReal = g2r,
                realCount = images.size - spacerCount,
                spreads = emptyList(),
                idxToSpread = emptyList(),
                chapterStartSpreads = emptySet(),
                pageMode = pageMode,
                spreadOffset = spreadOffset,
            ).rebuildSpreads(spreadOffset)
        }

        /** Page-mode rule — `updateOrientation`: landscape viewport +
         *  spread enabled → SPREAD. */
        fun pageMode(viewW: Float, viewH: Float, landscapeSpread: Boolean): ReaderPageMode =
            if (viewW >= viewH && landscapeSpread) ReaderPageMode.SPREAD else ReaderPageMode.SINGLE

        /** `spreadMetrics` — fit a spread into the viewport: pages share
         *  one height, each width = ar*h (+gap). Returns (w, h). */
        fun spreadFit(ars: List<Float>, gap: Float, viewW: Float, viewH: Float): Pair<Float, Float> {
            val arSum = ars.sum().coerceAtLeast(0.001f)
            var h = viewH
            var w = arSum * h + gap * (ars.size - 1)
            if (w > viewW) {
                h = (viewW - gap * (ars.size - 1)) / arSum
                w = viewW
            }
            return w to h
        }
    }
}
