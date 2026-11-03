package dev.bookq.reader.ui.reader

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalLayoutDirection
import androidx.compose.ui.unit.LayoutDirection
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import dev.bookq.core.reader.ExtDisplay
import dev.bookq.core.reader.ReaderWork
import dev.bookq.reader.reader.ReaderController

private const val GAP_INNER_PX = 5f

/**
 * The reader's external-display surface (issue #301) — the current
 * spread re-composed under the panel's own dimensions: pageMode from
 * the external viewport, spreads re-grouped, shared-height fit, RTL,
 * no chrome. The device keeps the interactive reader as the control
 * surface; every turn there lands here through [currentIdx].
 *
 * Mirrors iOS `ExternalReaderView` — pageMode is derived on this
 * surface, never copied from the device work.
 */
@Composable
fun ExternalReaderScreen(
    work: ReaderWork,
    currentIdx: Int,
    controller: ReaderController,
    landscapeSpread: Boolean,
    spreadOffset: Int,
    pageData: (Int) -> Any,
    imageLoader: coil3.ImageLoader,
) {
    BoxWithConstraints(
        Modifier.fillMaxSize().background(Color.Black),
        contentAlignment = Alignment.Center,
    ) {
        val density = LocalDensity.current
        val extW = with(density) { maxWidth.toPx() }
        val extH = with(density) { maxHeight.toPx() }
        val arMap by controller.pageAR.collectAsStateWithLifecycle()
        val (ext, spreadIdx) = remember(work, currentIdx, extW, extH,
            landscapeSpread, spreadOffset) {
            ExtDisplay.workForDisplay(work, currentIdx, extW, extH,
                landscapeSpread, spreadOffset)
        }
        val pages = ext.spreads.getOrElse(spreadIdx) { emptyList() }
        val ars = pages.map { arMap[it] ?: controller.lastAR }
        val (fitW, fitH) = remember(pages, ars, extW, extH) {
            ReaderWork.spreadFit(ars, GAP_INNER_PX, extW, extH)
        }
        CompositionLocalProvider(
            LocalLayoutDirection provides LayoutDirection.Rtl) {
            // The device reader's RTL contract: pages[0] sits rightmost.
            Row {
                for ((pos, idx) in pages.withIndex()) {
                    PageCell(
                        globalIdx = idx,
                        data = pageData(idx),
                        width = with(density) { (ars[pos] * fitH).toDp() },
                        height = with(density) { fitH.toDp() },
                        imageLoader = imageLoader,
                        isChapterStart = idx == pages.first() &&
                            spreadIdx in ext.chapterStartSpreads,
                        chapterName = ext.chapterInfo(idx).name,
                        onAR = { a -> controller.recordAR(idx, a) },
                        onLoaded = { controller.clearError(idx) },
                        onError = { controller.recordError(idx) },
                    )
                }
            }
        }
    }
}
