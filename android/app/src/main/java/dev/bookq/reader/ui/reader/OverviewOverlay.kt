package dev.bookq.reader.ui.reader

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyGridState
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.rememberLazyGridState
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.WindowInsetsSides
import androidx.compose.foundation.layout.asPaddingValues
import androidx.compose.foundation.layout.navigationBars
import androidx.compose.foundation.layout.only
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.runtime.withFrameNanos
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.PointerEventPass
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.layout.boundsInRoot
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import coil3.ImageLoader
import coil3.compose.AsyncImage
import coil3.request.ImageRequest
import coil3.request.bitmapConfig
import dev.bookq.core.reader.OverviewRules
import dev.bookq.core.reader.ReaderWork
import dev.bookq.data.BookqApi
import dev.bookq.reader.R
import kotlin.math.abs

private const val OTAG = "ZoomDbg"
private inline fun olog(msg: () -> String) {
    if (dev.bookq.reader.BuildConfig.DEBUG) android.util.Log.d(OTAG, msg())
}

/** overview.js: pinch-out exits once the cumulative ratio exceeds 1.3. */
private const val PINCH_OUT = 1.3f
/** overview.js cell geometry: 3:4 cells, min column ~150 px. */
private const val CELL_AR = 3f / 4f
private val CELL_MIN = 150.dp
/** overview.js grid gap + section padding. */
private val GAP = 8.dp
private val PAD = 16.dp
/** overview.js thumb width for cells. */
private const val THUMB_W = 400
/** overview.js IMG_WINDOW — thumbs prefetch band around the scroll pos. */
private const val IMG_WINDOW = 200
/** overview.js HUGE — works larger than this only attach <img> inside
 *  the ±IMG_WINDOW band. */
private const val HUGE = 600

/**
 * The web `#overview` grid: every real page as a 3:4 cell, chapter
 * sections with headers (cross-chapter only), the current page
 * highlighted and scrolled to the 1/3 line, tap to jump, pinch-out to
 * exit at the cell under the fingers. LazyVerticalGrid supplies the
 * ±window virtualization natively; the IMG_WINDOW band is prefetched.
 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
fun OverviewOverlay(
    work: ReaderWork,
    currentIdx: Int,
    api: BookqApi,
    offline: dev.bookq.reader.offline.OfflineManager,
    imageLoader: ImageLoader,
    onJump: (Int) -> Unit,
    onClose: () -> Unit,
    onCellCenter: (Float, Float) -> Unit = { _, _ -> },
) {
    BackHandler { onClose() }
    val navBottom = WindowInsets.navigationBars.asPaddingValues()
        .calculateBottomPadding()

    // Flat item list — (header chapterIdx | page globalIdx) entries in
    // reading order (OverviewRules.flatItems — overview.js buildGrid).
    val flat = remember(work) { OverviewRules.flatItems(work) }
    // Highlight the nearest real page at/after currentIdx — a spacer has
    // no cell of its own (web `nearestRealPos`).
    val curItem = remember(work, currentIdx, flat) {
        OverviewRules.nearestItem(flat, currentIdx)
    }
    val curKey = if (curItem >= 0) flat[curItem].second else -1

    val reals = remember(work) { OverviewRules.realIndices(work) }
    val huge = reals.size > HUGE
    // globalIdx → position in `reals`, for O(1) band checks.
    val posOf = remember(work, reals) {
        IntArray(work.images.size).also { a ->
            reals.forEachIndexed { p, g -> a[g] = p }
        }
    }
    // Huge-book scroll-anchored <img> band (overview.js updates it from
    // the scroll position). Anchored at the current page on open.
    var band by remember { mutableIntStateOf(0) }
    LaunchedEffect(curKey) {
        if (curKey < 0) return@LaunchedEffect
        band = reals.indexOfFirst { it >= curKey }
            .let { if (it < 0) reals.lastIndex else it }
    }

    // Prefetch off-screen cells during scroll slack — page thumbs
    // otherwise subcompose mid-fling (90 Hz budget is 11 ms).
    // Default strategy: `nestedPrefetchItemCount` only governs lazy
    // layouts nested INSIDE a cell — on this flat grid it is a no-op.
    val gridState = rememberLazyGridState()
    LaunchedEffect(huge) {
        if (!huge) return@LaunchedEffect
        snapshotFlow {
            gridState.firstVisibleItemIndex
        }.collect { fi ->
            val item = flat.getOrNull(fi) ?: return@collect
            if (!item.first) band = posOf[item.second]
        }
    }

    // No bulk prefetch — web thumbs load via <img loading="lazy">, the
    // grid's own virtualization is the bound (the ±IMG_WINDOW attach
    // gate above is the huge-book rule, not a prefetch).

    // The grid's top-left in root coords — needed to hand the reader
    // a drift target in the zoom layer's coordinate space.
    val gridRoot = remember { MutableRef(Offset.Zero) }

    // scrollCurToThird — land the current cell's CENTER on the 1/3 line
    // (web: offsetTop - H/3 + h/2), then report its measured center so
    // the next pinch-in drifts toward it (web curCenter).
    LaunchedEffect(curItem) {
        if (curItem < 0) return@LaunchedEffect
        gridState.scrollToItem(curItem)
        // layoutInfo updates after the scroll's layout pass — wait a
        // frame before reading item offsets.
        withFrameNanos { }
        val info = gridState.layoutInfo
        val cell = info.visibleItemsInfo.firstOrNull { it.index == curItem }
            ?: return@LaunchedEffect
        val viewport = info.viewportEndOffset - info.viewportStartOffset
        gridState.scroll {
            scrollBy(cell.offset.y + cell.size.height / 2f - viewport / 3f)
        }
        withFrameNanos { }
        val info2 = gridState.layoutInfo
        val cell2 = info2.visibleItemsInfo.firstOrNull { it.index == curItem }
            ?: return@LaunchedEffect
        // LazyGridItemInfo.offset already includes the contentPadding —
        // root coords are grid root + offset + half (no extra padding).
        onCellCenter(
            gridRoot.value.x + cell2.offset.x + cell2.size.width / 2f,
            gridRoot.value.y + cell2.offset.y + cell2.size.height / 2f,
        )
    }

    // updateChip — leafName local+1/total [・ 通算 pos+1/reals].
    val chip = remember(work, curKey) {
        if (curKey < 0) "" else {
            val info = work.chapterInfo(curKey)
            val total = work.chapters.getOrNull(info.chapterIdx)?.images?.size
                ?: work.realCount
            buildString {
                if (work.isCrossChapter)
                    append(info.name.substringAfterLast('/')).append(' ')
                append(info.localIdx + 1).append('/').append(total)
                if (work.isCrossChapter)
                    append(" ・ 通算 ").append(work.realIndex(curKey) + 1)
                        .append('/').append(work.realCount)
            }
        }
    }

    Box(
        Modifier
            .fillMaxSize()
            .background(Color(0xF20D0D0D))
            .testTag("overviewGrid")
            // Eat taps so nothing leaks to the reader's tap zones below.
            .pointerInput(Unit) { detectTapGestures { } },
    ) {
        Column(
            Modifier
                .fillMaxSize()
                // Header under the status bar; the grid takes the nav
                // bar itself via its bottom content padding below.
                .windowInsetsPadding(WindowInsets.safeDrawing.only(
                    WindowInsetsSides.Top + WindowInsetsSides.Horizontal)),
        ) {
            Surface(color = Color(0xCC1A1A1A)) {
                Row(
                    Modifier.fillMaxWidth().padding(horizontal = 4.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    IconButton(onClick = onClose) {
                        Icon(
                            Icons.Filled.Close,
                            contentDescription = stringResource(R.string.back),
                            tint = Color.White,
                        )
                    }
                    Text(
                        work.clickedPath.ifEmpty { "bookq" },
                        color = Color.White,
                        style = MaterialTheme.typography.titleMedium,
                        maxLines = 1,
                        modifier = Modifier.weight(1f),
                    )
                    Text(
                        chip,
                        color = Color(0xFFB0B0B0),
                        style = MaterialTheme.typography.labelLarge,
                    )
                }
            }
            OverviewGrid(
                work = work,
                flat = flat,
                curKey = curKey,
                api = api,
                offline = offline,
                imageLoader = imageLoader,
                gridState = gridState,
                attachImage = if (huge) {
                    { g: Int -> abs(posOf[g] - band) <= IMG_WINDOW }
                } else null,
                navBottom = navBottom,
                onJump = onJump,
                onExit = onClose,
                onGridRoot = { gridRoot.value = it },
            )
        }
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun OverviewGrid(
    work: ReaderWork,
    flat: List<Pair<Boolean, Int>>,
    curKey: Int,
    api: BookqApi,
    offline: dev.bookq.reader.offline.OfflineManager,
    imageLoader: ImageLoader,
    gridState: LazyGridState,
    attachImage: ((Int) -> Boolean)?,
    navBottom: androidx.compose.ui.unit.Dp,
    onJump: (Int) -> Unit,
    onExit: () -> Unit,
    onGridRoot: (Offset) -> Unit,
) {
    Box(
        Modifier
            .fillMaxSize()
            .onGloballyPositioned { onGridRoot(it.boundsInRoot().topLeft) }
            // Pinch-out → exit at the cell under the fingers
            // (overview.js attach). Initial pass so the two-finger
            // tracking pre-empts the grid's own scroll claim. The key
            // must be stable — work/curKey can recompose mid-gesture
            // and a pointerInput restart drops the whole gesture.
            .pointerInput(Unit) {
                awaitEachGesture {
                    awaitFirstDown(requireUnconsumed = false,
                        pass = PointerEventPass.Initial)
                    var acc = 1f
                    var sawMulti = false
                    var mid = Offset.Zero
                    // Fire as soon as the ratio crosses (web exits live,
                    // not on release) — a cell's clickable can claim the
                    // stream mid-gesture, and a release-time check would
                    // then never reach the threshold.
                    var fired = false
                    while (true) {
                        val ev = awaitPointerEvent(PointerEventPass.Initial)
                        val pressed = ev.changes.filter { it.pressed }
                        if (pressed.size >= 2) {
                            sawMulti = true
                            val a = pressed[0]
                            val b = pressed[1]
                            val cur = (a.position - b.position).getDistance()
                            val prev = (a.previousPosition -
                                b.previousPosition).getDistance()
                            if (prev > 0f) acc *= cur / prev
                            mid = (a.position + b.position) / 2f
                            a.consume(); b.consume()
                            if (acc > PINCH_OUT) { fired = true; break }
                        }
                        if (ev.changes.none { it.pressed }) {
                            // Release-time fallback — on a starved emulator
                            // the stream can end before the mid-gesture
                            // check sees the crossing (web also checks at
                            // pointerup).
                            if (acc > PINCH_OUT) fired = true
                            break
                        }
                    }
                    olog { "gesture end sawMulti=$sawMulti acc=$acc fired=$fired mid=$mid" }
                    if (fired) {
                        // elementFromPoint → the grid cell under lastMid.
                        // The fillMaxSize Box and the grid share bounds,
                        // and item.offset already INCLUDES contentPadding —
                        // compare mid directly (subtracting again shifts
                        // the hit point up-left onto the wrong cell).
                        val info = gridState.layoutInfo
                        val hit = info.visibleItemsInfo
                            .firstOrNull { item ->
                                (item.key as? String)?.startsWith("g") == true &&
                                    mid.x >= item.offset.x &&
                                    mid.x < item.offset.x + item.size.width &&
                                    mid.y >= item.offset.y &&
                                    mid.y < item.offset.y + item.size.height
                            }
                        val g = (hit?.key as? String)
                            ?.removePrefix("g")?.toIntOrNull()
                        if (g != null) onJump(g) else onExit()
                    }
                }
            },
    ) {
        LazyVerticalGrid(
            state = gridState,
            columns = GridCells.Adaptive(CELL_MIN),
            // Last row scrolls clear of the nav bar (3-button/gesture).
            contentPadding = PaddingValues(
                start = PAD, top = PAD, end = PAD, bottom = PAD + navBottom),
            horizontalArrangement = Arrangement.spacedBy(GAP),
            verticalArrangement = Arrangement.spacedBy(GAP),
            modifier = Modifier.fillMaxSize(),
        ) {
            // Sections — sticky chapter headers like the web grid's
            // `.ov-chapter` bars (cross-chapter works only). Item order
            // matches `flat`, so curItem indexes land correctly.
            work.chapters.forEachIndexed { ci, ch ->
                if (work.isCrossChapter) {
                    stickyHeader(key = "h$ci", contentType = "header") {
                        Text(
                            ch.name.substringAfterLast('/'),
                            color = Color(0xFFB0B0B0),
                            style = MaterialTheme.typography.titleSmall,
                            modifier = Modifier
                                .fillMaxWidth()
                                .background(Color(0xE61A1A1A))
                                .padding(top = 12.dp, bottom = 4.dp),
                        )
                    }
                }
                val start = work.boundaries.getOrElse(ci) { 0 }
                items(
                    count = ch.images.size,
                    key = { "g${start + it}" },
                    contentType = { "cell" },
                ) { l ->
                    OverviewCell(
                        work = work,
                        g = start + l,
                        isCur = start + l == curKey,
                        attachImage = attachImage?.invoke(start + l) ?: true,
                        scrolling = gridState.isScrollInProgress,
                        api = api,
                        offline = offline,
                        imageLoader = imageLoader,
                        onJump = onJump,
                    )
                }
            }
        }
    }
}

@Composable
private fun OverviewCell(
    work: ReaderWork,
    g: Int,
    isCur: Boolean,
    attachImage: Boolean,
    scrolling: Boolean,
    api: BookqApi,
    offline: dev.bookq.reader.offline.OfflineManager,
    imageLoader: ImageLoader,
    onJump: (Int) -> Unit,
) {
    val context = LocalContext.current
    val im = work.images[g]
    val request = remember(im) {
        // RGB_565 like the browse tiles — halves the upload per cell.
        ImageRequest.Builder(context)
            .data(offline.thumbData(im.folder, im.name, THUMB_W))
            .bitmapConfig(android.graphics.Bitmap.Config.RGB_565)
            .build()
    }
    val info = work.chapterInfo(g)
    Box(
        Modifier
            .aspectRatio(CELL_AR)
            .background(Color(0xFF161616))
            .testTag("ovcell:$g")
            .then(
                if (isCur) Modifier.border(2.dp,
                    MaterialTheme.colorScheme.primary)
                else Modifier,
            )
            .clickable { onJump(g) },
        contentAlignment = Alignment.Center,
    ) {
        // Deferred attach while the grid scrolls: image delivery inside
        // a fling frame is the overviewFling tail (#302). Cells that
        // already painted keep their image; the rest warm the cache via
        // a plain enqueue and draw on settle.
        var painted by remember(request) { mutableStateOf(false) }
        if (attachImage && (painted || !scrolling)) {
            // AsyncImage (not Subcompose): a subcomposition per cell is
            // the dominant overviewFling frame cost. Error state is a
            // lightweight overlay below.
            var failed by remember { mutableStateOf(false) }
            AsyncImage(
                model = request,
                imageLoader = imageLoader,
                contentDescription = null,
                contentScale = ContentScale.Crop,
                modifier = Modifier.fillMaxSize(),
                onState = { s ->
                    if (s is coil3.compose.AsyncImagePainter.State.Success) {
                        painted = true
                    }
                    failed = s is coil3.compose.AsyncImagePainter.State.Error
                },
            )
            if (failed) Text("⚠", color = Color(0xFF808080))
        } else if (attachImage) {
            androidx.compose.runtime.LaunchedEffect(request) {
                imageLoader.enqueue(request)
            }
        }
        // web `.pg` badge — chapter-local page number.
        Text(
            "${info.localIdx + 1}",
            color = Color.White,
            style = MaterialTheme.typography.labelSmall,
            modifier = Modifier
                .align(Alignment.BottomEnd)
                .background(Color(0x99000000))
                .padding(horizontal = 4.dp, vertical = 1.dp),
        )
    }
}
