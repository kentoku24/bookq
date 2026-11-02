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
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import coil3.ImageLoader
import coil3.compose.SubcomposeAsyncImage
import coil3.request.ImageRequest
import dev.bookq.core.reader.OverviewRules
import dev.bookq.core.reader.ReaderWork
import dev.bookq.data.BookqApi
import dev.bookq.reader.R
import kotlin.math.abs

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
@Composable
fun OverviewOverlay(
    work: ReaderWork,
    currentIdx: Int,
    api: BookqApi,
    imageLoader: ImageLoader,
    onJump: (Int) -> Unit,
    onClose: () -> Unit,
) {
    BackHandler { onClose() }
    val context = LocalContext.current

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

    // Prefetch the ±IMG_WINDOW thumb band once on open (huge-book rule:
    // images hydrate only near the current position).
    LaunchedEffect(work, curKey) {
        if (curKey < 0) return@LaunchedEffect
        for (p in OverviewRules.imgWindow(reals, curKey)) {
            val im = work.images[reals[p]]
            imageLoader.enqueue(ImageRequest.Builder(context)
                .data(api.thumbUrl(im.folder, im.name, THUMB_W).toString())
                .build())
        }
    }

    // scrollCurToThird — land the current cell's top on the 1/3 line.
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
        gridState.scroll { scrollBy(cell.offset.y - viewport / 3f) }
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
            // Eat taps so nothing leaks to the reader's tap zones below.
            .pointerInput(Unit) { detectTapGestures { } },
    ) {
        Column(Modifier.fillMaxSize()) {
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
                imageLoader = imageLoader,
                gridState = gridState,
                attachImage = if (huge) {
                    { g: Int -> abs(posOf[g] - band) <= IMG_WINDOW }
                } else null,
                onJump = onJump,
                onExit = onClose,
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
    imageLoader: ImageLoader,
    gridState: LazyGridState,
    attachImage: ((Int) -> Boolean)?,
    onJump: (Int) -> Unit,
    onExit: () -> Unit,
) {
    Box(
        Modifier
            .fillMaxSize()
            // Pinch-out → exit at the cell under the fingers
            // (overview.js attach). Initial pass so the two-finger
            // tracking pre-empts the grid's own scroll claim.
            .pointerInput(work, curKey) {
                awaitEachGesture {
                    awaitFirstDown(requireUnconsumed = false,
                        pass = PointerEventPass.Initial)
                    var acc = 1f
                    var multi = false
                    var mid = Offset.Zero
                    while (true) {
                        val ev = awaitPointerEvent(PointerEventPass.Initial)
                        val pressed = ev.changes.filter { it.pressed }
                        if (pressed.size >= 2) {
                            multi = true
                            val a = pressed[0]
                            val b = pressed[1]
                            val cur = (a.position - b.position).getDistance()
                            val prev = (a.previousPosition -
                                b.previousPosition).getDistance()
                            if (prev > 0f) acc *= cur / prev
                            mid = (a.position + b.position) / 2f
                            a.consume(); b.consume()
                        }
                        if (ev.changes.none { it.pressed }) break
                    }
                    if (multi && acc > PINCH_OUT) {
                        // elementFromPoint → the grid cell under lastMid.
                        val hit = gridState.layoutInfo.visibleItemsInfo
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
            contentPadding = PaddingValues(PAD),
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
                        api = api,
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
    api: BookqApi,
    imageLoader: ImageLoader,
    onJump: (Int) -> Unit,
) {
    val context = LocalContext.current
    val im = work.images[g]
    val request = remember(im) {
        ImageRequest.Builder(context)
            .data(api.thumbUrl(im.folder, im.name, THUMB_W).toString())
            .build()
    }
    val info = work.chapterInfo(g)
    Box(
        Modifier
            .aspectRatio(CELL_AR)
            .background(Color(0xFF161616))
            .then(
                if (isCur) Modifier.border(2.dp,
                    MaterialTheme.colorScheme.primary)
                else Modifier,
            )
            .clickable { onJump(g) },
        contentAlignment = Alignment.Center,
    ) {
        if (attachImage) {
            SubcomposeAsyncImage(
                model = request,
                imageLoader = imageLoader,
                contentDescription = null,
                contentScale = ContentScale.Crop,
                modifier = Modifier.fillMaxSize(),
                loading = {},
                error = { Text("⚠", color = Color(0xFF808080)) },
            )
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
