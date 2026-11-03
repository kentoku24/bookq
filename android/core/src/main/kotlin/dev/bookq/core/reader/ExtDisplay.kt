package dev.bookq.core.reader

/**
 * External-display projection model (issue #301) — the pure decision
 * layer the app's `DisplayManager` plumbing drives. Mirrors the iOS
 * `ExternalDisplayStore` semantics: the external surface re-derives
 * pageMode from ITS OWN viewport and re-groups spreads, the device
 * side keeps the interactive reader as the control surface.
 *
 * Projection gates (all must hold):
 *  - the reader is active (a work is loaded on screen),
 *  - the session is NOT paired (dual-reader owns the second seat),
 *  - the user tv-toggle is enabled,
 *  - a presentation-capable display is attached.
 */
object ExtDisplay {

    /** Testable mirror of `Display.Mode` (which needs a real Display). */
    data class ModeRef(
        val width: Int,
        val height: Int,
        val refreshHz: Float,
    ) {
        val pixelCount: Long get() = width.toLong() * height.toLong()
    }

    /** A `DISPLAY_CATEGORY_PRESENTATION` display, reduced to what the
     *  decision + mode selection need (the app fills this from
     *  `Display.getSupportedModes()`). */
    data class DisplayRef(
        val id: Int,
        val modes: List<ModeRef>,
    )

    /**
     * Highest-resolution mode — "最大解像度モードを選ぶ". Max pixel count;
     * a refresh-rate tie-break prefers the smoother mode, and equal
     * candidates resolve to the LAST index (iOS `max(by:)` parity,
     * deterministic).
     */
    fun bestModeIndex(modes: List<ModeRef>): Int? {
        if (modes.isEmpty()) return null
        var best = 0
        for (i in modes.indices) {
            val m = modes[i]
            val b = modes[best]
            if (m.pixelCount > b.pixelCount ||
                (m.pixelCount == b.pixelCount && m.refreshHz >= b.refreshHz)) {
                best = i
            }
        }
        return best
    }

    /**
     * The projection decision — which display the Presentation goes to,
     * or null. First attached presentation display wins (Android has no
     * meaningful multi-projection UI surface to pick between).
     */
    fun target(
        readerActive: Boolean,
        paired: Boolean,
        userEnabled: Boolean,
        displays: List<DisplayRef>,
    ): DisplayRef? =
        if (readerActive && !paired && userEnabled) displays.firstOrNull()
        else null

    /**
     * Regroup [work] for the external viewport — the same rule the
     * device applies (`updateOrientation`/`rebuildSpreads`), keyed on
     * the EXTERNAL size. Returns the re-spread work plus the spread
     * index carrying [currentIdx] (global order never changes on
     * remode, only grouping).
     */
    fun workForDisplay(
        work: ReaderWork,
        currentIdx: Int,
        extW: Float,
        extH: Float,
        landscapeSpread: Boolean,
        spreadOffset: Int,
    ): Pair<ReaderWork, Int> {
        val w = work.withMode(
            ReaderWork.pageMode(extW, extH, landscapeSpread), spreadOffset)
        return w to w.spreadOf(currentIdx)
    }
}
