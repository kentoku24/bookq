package dev.bookq.reader

import androidx.tracing.Trace

/**
 * App-level trace sections consumed by the Macrobenchmark gates in
 * :baselineprofile (issue #302 §5):
 *
 *  - `bookq.pageTurn`: turn committed (tap zone / settle of a raw swipe)
 *    → first draw of the target spread.
 *  - `bookq.readerOpen`: work tap in browse → first draw of the first
 *    reader spread.
 *
 * Sections are unbalanced-safe: a new [pageTurn] closes a stale open one
 * so rapid double-turns never leave a dangling begin. `Trace` calls are
 * cheap no-ops when no trace is captured, so they stay in release.
 */
object PerfTrace {

    private const val PAGE_TURN = "bookq.pageTurn"
    private const val READER_OPEN = "bookq.readerOpen"

    @Volatile private var pageTurnOpen = false
    @Volatile private var pageTurnTarget = -1
    @Volatile private var readerOpenOpen = false

    /** Begin on turn commit; closes on the TARGET spread's first draw
     *  (adjacent pager cells re-drawing mid-turn must not end it). */
    fun pageTurn(targetSpread: Int) {
        if (pageTurnOpen) Trace.endSection()
        Trace.beginSection(PAGE_TURN)
        pageTurnOpen = true
        pageTurnTarget = targetSpread
    }

    fun readerOpen() {
        if (readerOpenOpen) Trace.endSection()
        Trace.beginSection(READER_OPEN)
        readerOpenOpen = true
    }

    /** Spread cell's first draw: closes the open reader-open section on
     *  any spread, and the page-turn section only on its target. */
    fun onSpreadDrawn(spreadIdx: Int) {
        if (pageTurnOpen && spreadIdx == pageTurnTarget) {
            Trace.endSection()
            pageTurnOpen = false
        }
        if (readerOpenOpen) {
            Trace.endSection()
            readerOpenOpen = false
        }
    }

    /** Fallback close for pre-composed targets: with
     *  `beyondViewportPageCount` the adjacent spread is already composed
     *  and a snap only translates its layer — `drawWithContent` never
     *  re-fires, so [onSpreadDrawn] can't close the section. Called
     *  after `scrollToPage` returns: one frame boundary, then close
     *  whatever is still open. No-op when the draw hook already ran. */
    suspend fun closePageTurnAfterFrame() {
        if (!pageTurnOpen) return
        kotlinx.coroutines.withTimeoutOrNull(1_000) {
            androidx.compose.runtime.withFrameNanos {}
        }
        if (pageTurnOpen) {
            Trace.endSection()
            pageTurnOpen = false
        }
    }
}

/** Measurement-only tunables set from intent extras in builds that
 *  allow `serverURL` (debug/benchmark — never release; #302 A/B). */
object PerfTunables {

    /** HorizontalPager composition window override for the reader A/B
     *  sweep (`--ei bvp N`). null → the code default. */
    @Volatile var beyondViewportPageCount: Int? = null

    /** Process-uptime anchor for cold-start phase logging (ms). */
    @Volatile var bootT0: Long = 0L

    /** `PerfBoot` logcat marker: `bookq.boot <tag> t=<ms-since-onCreate>`.
     *  Measurement builds only — never logs in release, and no-ops on
     *  JVM unit tests where android.util.Log is unmocked. */
    fun mark(tag: String) {
        if (!BuildConfig.ALLOW_SERVER_URL_EXTRA) return
        runCatching {
            android.util.Log.i("PerfBoot",
                "$tag t=${android.os.SystemClock.uptimeMillis() - bootT0}")
        }
    }
}
