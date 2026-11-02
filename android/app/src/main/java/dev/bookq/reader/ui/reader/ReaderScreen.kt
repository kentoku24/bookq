package dev.bookq.reader.ui.reader

import android.app.Activity
import androidx.activity.compose.BackHandler
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.CubicBezierEasing
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.foundation.background
import androidx.compose.foundation.focusable
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.pager.HorizontalPager
import androidx.compose.foundation.pager.rememberPagerState
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Slider
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.TransformOrigin
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.key
import androidx.compose.ui.input.key.onKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.platform.LocalLayoutDirection
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.SemanticsPropertyKey
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.LayoutDirection
import androidx.compose.ui.unit.dp
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.navigation.NavController
import coil3.compose.SubcomposeAsyncImage
import coil3.request.ImageRequest
import dev.bookq.core.browse.BrowseRules
import dev.bookq.core.reader.ReaderWork
import dev.bookq.core.reader.ZoomBox
import dev.bookq.core.reader.ZoomK
import dev.bookq.core.reader.ZoomState
import dev.bookq.core.reader.zoneAction
import dev.bookq.core.reader.ZoneAction
import dev.bookq.reader.AppGraph
import dev.bookq.reader.R
import dev.bookq.reader.reader.ReaderController
import dev.bookq.reader.ui.browse.RETURN_ANCHOR_KEY
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlin.math.abs
import kotlin.math.roundToInt
import kotlinx.coroutines.withTimeoutOrNull

/** spread.js `GAP_INNER`. */
private const val GAP_INNER_PX = 5f

/** Instrumentation hooks — the connected test reads zoom state
 *  directly from semantics instead of inferring it from page counters
 *  (the OS input pipeline is unreliable on some ROMs). */
val ReaderZoomScale = SemanticsPropertyKey<Float>("ReaderZoomScale")
val ReaderZoomPanX = SemanticsPropertyKey<Float>("ReaderZoomPanX")
/** Max wait for the full tree before assembling from what exists — the
 *  shallow window can race a reader-open tap (web gates on tree-ready). */
private const val TREE_WAIT_MS = 8_000L

/**
 * M3 reader — ports the web viewer's loadImages/spread/navigation
 * contract onto a native pager: RTL page order (pages[N] on the right,
 * next pages arrive from the left), 30%/center/70% tap zones, spread
 * pairing with offset, cross-chapter spacer alignment, chapter-scoped
 * slider, and per-chapter progress saves. Zoom lands in M4.
 */
@Composable
fun ReaderScreen(
    graph: AppGraph,
    nav: NavController,
    folder: String,
    page: Int,
) {
    val scope by graph.serverScope.collectAsStateWithLifecycle()
    val original by graph.settings.originalImages.collectAsStateWithLifecycle()
    val controller = remember { ReaderController() }
    val load by controller.load.collectAsStateWithLifecycle()

    // Session-only layout settings (web Q17 — never persist).
    val landscapeSpread by controller.landscapeSpread.collectAsStateWithLifecycle()
    val spreadOffset by controller.spreadOffset.collectAsStateWithLifecycle()
    val tapForwardLeft by controller.tapForwardLeft.collectAsStateWithLifecycle()

    var work by remember { mutableStateOf<ReaderWork?>(null) }
    var currentIdx by rememberSaveable { mutableIntStateOf(0) }
    var chrome by remember { mutableStateOf(true) }

    val sc = scope ?: return Box(Modifier.fillMaxSize()
        .background(Color(0xFF101010)))

    BoxWithConstraints(
        Modifier.fillMaxSize().background(Color(0xFF101010)),
    ) {
        val density = LocalDensity.current
        val viewW = with(density) { maxWidth.toPx() }
        val viewH = with(density) { maxHeight.toPx() }
        val viewKey = "${viewW.roundToInt()}x${viewH.roundToInt()}"

        // Assemble once the tree (or whatever we have after the wait)
        // is in — loadImages' entry point.
        LaunchedEffect(sc, folder, page) {
            withTimeoutOrNull(TREE_WAIT_MS) {
                sc.tree.state.first { it.treeLoaded }
            }
            controller.open(folder, page, sc.tree.state.value.nodes,
                { f -> sc.api.images(f) }, viewW, viewH)
        }

        when (val l = load) {
            is ReaderController.LoadState.Loading -> Box(
                Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                CircularProgressIndicator(color = Color.White)
            }
            is ReaderController.LoadState.Empty -> ReaderEmpty(
                onBack = { goBack(nav, folder) })
            is ReaderController.LoadState.Error -> ReaderError(
                l.message, onBack = { goBack(nav, folder) })
            is ReaderController.LoadState.Ready -> {
                // Orientation / layout toggles rebuild spreads and
                // re-anchor on the same global index — `updateOrientation`
                // + `rebuildSpreads` rolled together.
                LaunchedEffect(viewKey, landscapeSpread, spreadOffset) {
                    val w0 = work ?: l.work
                    val next = controller.remode(w0, viewW, viewH)
                    if (next !== w0) {
                        work = next
                        // currentIdx stays valid — global order never
                        // changes on remode, only grouping.
                    } else if (work == null) {
                        work = next
                    }
                }
                val w = work ?: l.work
                ReaderBody(
                    graph = graph,
                    sc = sc,
                    controller = controller,
                    work = w,
                    anchorIdx = if (work == null) l.startIdx else currentIdx,
                    landscapeSpread = landscapeSpread,
                    spreadOffset = spreadOffset,
                    original = original,
                    tapForwardLeft = tapForwardLeft,
                    chrome = chrome,
                    viewW = viewW,
                    viewH = viewH,
                    onToggleChrome = { chrome = !chrome },
                    onCurrent = { currentIdx = it },
                    onBack = { goBack(nav, w.clickedPath) },
                )
            }
        }
    }

    Immersive(chrome)
    // Keep the screen lit while reading (plan: FLAG_KEEP_SCREEN_ON).
    val view = LocalView.current
    DisposableEffect(Unit) {
        view.keepScreenOn = true
        onDispose { view.keepScreenOn = false }
    }
}

private fun goBack(nav: NavController, clickedPath: String) {
    nav.previousBackStackEntry
        ?.savedStateHandle
        ?.set(RETURN_ANCHOR_KEY, clickedPath)
    nav.popBackStack()
}

@Composable
private fun ReaderBody(
    graph: AppGraph,
    sc: dev.bookq.reader.ServerScope,
    controller: ReaderController,
    work: ReaderWork,
    anchorIdx: Int,
    landscapeSpread: Boolean,
    spreadOffset: Int,
    original: Boolean,
    tapForwardLeft: Boolean,
    chrome: Boolean,
    viewW: Float,
    viewH: Float,
    onToggleChrome: () -> Unit,
    onCurrent: (Int) -> Unit,
    onBack: () -> Unit,
) {
    val context = LocalContext.current
    val compose = rememberCoroutineScope()
    val pager = rememberPagerState(
        initialPage = work.spreadOf(anchorIdx),
        pageCount = { work.spreads.size },
    )
    var menu by remember { mutableStateOf(false) }
    val volumeKeys by graph.settings.volumeKeys.collectAsStateWithLifecycle()
    val focusRequester = remember { FocusRequester() }
    LaunchedEffect(Unit) { focusRequester.requestFocus() }

    // iOS `turn`: spread-granularity, cross-chapter; rapid taps coalesce
    // — the first lands now, follow-ups merge into one delayed target
    // so the pager plays a single animation.
    var pendingSpread by remember { mutableStateOf<Int?>(null) }
    var lastNavAt by remember { androidx.compose.runtime.mutableLongStateOf(0L) }
    var navJob by remember { mutableStateOf<kotlinx.coroutines.Job?>(null) }

    fun turn(delta: Int) {
        if (pager.pageCount == 0) return
        val base = pendingSpread ?: pager.settledPage
        val target = (base + delta).coerceIn(0, pager.pageCount - 1)
        val now = android.os.SystemClock.uptimeMillis()
        if (now - lastNavAt > 250) {
            lastNavAt = now
            pendingSpread = target
            // Instant strip — plan: tap turns snap, no animation.
            navJob = compose.launch {
                pager.scrollToPage(target)
                pendingSpread = null
            }
        } else {
            pendingSpread = target
            navJob?.cancel()
            navJob = compose.launch {
                kotlinx.coroutines.delay(160)
                pendingSpread?.let { t ->
                    lastNavAt = android.os.SystemClock.uptimeMillis()
                    pager.scrollToPage(t)
                    pendingSpread = null
                }
            }
        }
    }

    fun chapterEdge(end: Boolean) {
        val idx = work.spreads.getOrNull(pager.settledPage)?.first() ?: return
        val r = work.chapterSpreadRange(idx)
        compose.launch {
            pager.scrollToPage(if (end) r.last else r.first)
        }
    }

    // Re-anchor after a remode (rotation / spread toggles) — global order
    // never changes, so the same global index maps to the new spread.
    LaunchedEffect(work) {
        val target = work.spreadOf(anchorIdx)
        if (pager.currentPage != target) pager.scrollToPage(target)
    }

    // ── M4: pinch-zoom + overview (viewer/zoom.js + overview.js) ──
    val haptic = LocalHapticFeedback.current
    val zs = remember { ZoomState() }
    var zoomF by remember { mutableFloatStateOf(1f) }
    var txF by remember { mutableFloatStateOf(0f) }
    var tyF by remember { mutableFloatStateOf(0f) }
    var overviewOpen by remember { mutableStateOf(false) }
    var suppressTapUntil by remember { mutableLongStateOf(0L) }
    // pointerInput captures values at launch — keep live flags.
    val zoomedNow by rememberUpdatedState(zoomF > 1f)
    // One cancellable animation job shared with the gesture layer —
    // reset and bounce can never race.
    val zoomAnim = remember { MutableRef<Job?>(null) }

    fun applyZoom(s: Float, x: Float, y: Float) {
        zoomF = s; txF = x; tyF = y
    }

    fun resetZoom(animated: Boolean) {
        if (!animated || (zoomF == 1f && txF == 0f && tyF == 0f)) {
            zoomAnim.value?.cancel()
            zs.reset(); applyZoom(1f, 0f, 0f); return
        }
        // Snap-back — cubic-bezier(0.25, 0.1, 0.25, 1) over SLIDE_MS.
        val s0 = zoomF; val x0 = txF; val y0 = tyF
        zoomAnim.value?.cancel()
        zoomAnim.value = compose.launch {
            Animatable(0f).animateTo(
                1f,
                tween(ZoomK.SLIDE_MS,
                    easing = CubicBezierEasing(0.25f, 0.1f, 0.25f, 1f)),
            ) {
                applyZoom(s0 + (1f - s0) * value,
                    x0 * (1 - value), y0 * (1 - value))
            }
            zs.reset(); applyZoom(1f, 0f, 0f)
        }
    }

    // Double-tap zoom-in — same cubic slide as resetZoom, to an
    // arbitrary target (s, x, y) from the model.
    fun animateZoomTo(s: Float, x: Float, y: Float) {
        val s0 = zoomF; val x0 = txF; val y0 = tyF
        zoomAnim.value?.cancel()
        zoomAnim.value = compose.launch {
            Animatable(0f).animateTo(
                1f,
                tween(ZoomK.SLIDE_MS,
                    easing = CubicBezierEasing(0.25f, 0.1f, 0.25f, 1f)),
            ) {
                applyZoom(s0 + (s - s0) * value,
                    x0 + (x - x0) * value, y0 + (y - y0) * value)
            }
            zs.apply(s, x, y); applyZoom(s, x, y)
        }
    }

    fun curGlobalIdx() =
        work.spreads.getOrNull(pager.settledPage)?.first() ?: anchorIdx

    fun openOverview() {
        // web show(): the layer resets and the grid takes the screen.
        resetZoom(animated = false)
        overviewOpen = true
    }

    fun exitOverview(globalIdx: Int) {
        overviewOpen = false
        compose.launch { pager.scrollToPage(work.spreadOf(globalIdx)) }
    }

    // measureBox — the centered spread's resting rect, refreshed per
    // settled page / measured aspect ratios / viewport size.
    val arMap by controller.pageAR.collectAsStateWithLifecycle()
    LaunchedEffect(work, pager.settledPage, arMap, viewW, viewH) {
        val pages = work.spreads.getOrNull(pager.settledPage) ?: emptyList()
        val (fw, fh) = ReaderWork.spreadFit(
            pages.map { arMap[it] ?: controller.lastAR },
            GAP_INNER_PX, viewW, viewH)
        zs.box = ZoomBox.centered(fw, fh, viewW, viewH)
    }

    fun pageUrl(globalIdx: Int): String {
        val p = work.images[globalIdx]
        return sc.api.pageUrl(p.folder, p.name, original).toString()
    }

    // Commit on settle — navigation.js: the deferred finalize runs only
    // after the track settles; a newer settle invalidates the older.
    // `original` keys the URL scheme captured by pageUrl below.
    LaunchedEffect(work, pager, original) {
        snapshotFlow { pager.settledPage }.collect { s ->
            val idx = work.spreads.getOrNull(s)?.first() ?: return@collect
            onCurrent(idx)
            pendingSpread = null
            // A page settle resets the layer (web snapTo → Zoom.reset).
            if (zoomF != 1f || txF != 0f || tyF != 0f) {
                zs.reset(); applyZoom(1f, 0f, 0f)
            }
            val info = work.chapterInfo(idx)
            val ch = work.chapters.getOrNull(info.chapterIdx) ?: return@collect
            sc.progress.save(ch.fullPath, info.localIdx, ch.images.size)
            // spread.js ensureWindow: prefetch global idx −3 … +8,
            // non-spacer only — keeps the disk cache warm.
            val lo = (idx - 3).coerceAtLeast(0)
            val hi = (idx + 8).coerceAtMost(work.images.size - 1)
            for (i in lo..hi) {
                if (work.images[i].isSpacer) continue
                sc.imageLoader.enqueue(
                    ImageRequest.Builder(context).data(pageUrl(i)).build())
            }
        }
    }

    BackHandler { onBack() }

    Box(
        Modifier
            .fillMaxSize()
            .focusRequester(focusRequester)
            .focusable()
            .semantics {
                this[ReaderZoomScale] = zoomF
                this[ReaderZoomPanX] = txF
            }
            .onKeyEvent { e ->
                val down = e.type == KeyEventType.KeyDown
                if (overviewOpen) {
                    // The grid owns keys — G/Esc exit, everything else is
                    // swallowed so the reader below can't move.
                    return@onKeyEvent when (e.key) {
                        Key.G, Key.Escape -> {
                            if (down) exitOverview(curGlobalIdx()); true
                        }
                        else -> true
                    }
                }
                when (e.key) {
                    // iOS key convention (platform-native): → = forward,
                    // ← = back. Home/End jump to chapter bounds.
                    Key.DirectionRight -> { if (down) turn(1); true }
                    Key.DirectionLeft -> { if (down) turn(-1); true }
                    Key.MoveHome -> { if (down) chapterEdge(false); true }
                    Key.MoveEnd -> { if (down) chapterEdge(true); true }
                    Key.G -> { if (down) openOverview(); true }
                    Key.VolumeUp -> if (volumeKeys) { if (down) turn(-1); true } else false
                    Key.VolumeDown -> if (volumeKeys) { if (down) turn(1); true } else false
                    else -> false
                }
            }
            .zoomGestures(
                zs = zs,
                enabled = { !overviewOpen },
                canTurn = { d ->
                    pager.settledPage + d in 0 until pager.pageCount
                },
                apply = ::applyZoom,
                onOverviewFire = ::openOverview,
                onEdgeTurn = ::turn,
                onHaptic = {
                    haptic.performHapticFeedback(HapticFeedbackType.LongPress)
                },
                onSuppressTap = {
                    suppressTapUntil =
                        android.os.SystemClock.uptimeMillis() + 400
                },
                driftTarget = { (viewW / 2f) to (viewH / 3f) },
                scope = compose,
                animSlot = zoomAnim,
            )
            .pointerInput(tapForwardLeft, work) {
                var lastCenterTapAt = 0L
                var lastCenterTapX = 0f
                var lastCenterTapY = 0f
                detectTapGestures { off ->
                    // A decided pan leaves a trailing tap — the web's
                    // suppressClickFor(400).
                    val now = android.os.SystemClock.uptimeMillis()
                    if (now < suppressTapUntil)
                        return@detectTapGestures
                    when (val a = zoneAction(off.x / size.width, tapForwardLeft)) {
                        is ZoneAction.Turn -> if (!zoomedNow) turn(a.delta)
                        // Zoomed center tap drops back to 1x (plan).
                        ZoneAction.ToggleChrome -> when {
                            zoomedNow -> resetZoom(animated = true)
                            // Double-tap (Android convention — issue spec):
                            // the first tap toggled chrome instantly, so
                            // re-toggling restores it, then zoom in at
                            // the tapped point. Edge double-taps stay
                            // rapid turns — no added tap latency.
                            now - lastCenterTapAt < ZoomK.DOUBLE_TAP_MS &&
                                abs(off.x - lastCenterTapX) <
                                    ZoomK.DOUBLE_TAP_PX &&
                                abs(off.y - lastCenterTapY) <
                                    ZoomK.DOUBLE_TAP_PX -> {
                                lastCenterTapAt = 0L
                                onToggleChrome()
                                val t = zs.doubleTapTarget(off.x, off.y)
                                animateZoomTo(t[0], t[1], t[2])
                            }
                            else -> {
                                lastCenterTapAt = now
                                lastCenterTapX = off.x; lastCenterTapY = off.y
                                onToggleChrome()
                            }
                        }
                    }
                }
            },
    ) {
        // RTL: page 0 sits rightmost, later spreads arrive from the left —
        // the web track's negative-x layout, for free.
        CompositionLocalProvider(LocalLayoutDirection provides LayoutDirection.Rtl) {
            HorizontalPager(
                state = pager,
                beyondViewportPageCount = 1,
                // Zoomed → the zoom layer owns one-finger drags.
                userScrollEnabled = !zoomedNow,
                modifier = Modifier
                    .fillMaxSize()
                    // translate(tx,ty) scale(zoom) around the top-left —
                    // the web #zoom-layer transform, layer-local.
                    .graphicsLayer {
                        transformOrigin = TransformOrigin(0f, 0f)
                        scaleX = zoomF
                        scaleY = zoomF
                        translationX = txF
                        translationY = tyF
                    },
            ) { spreadIdx ->
                SpreadCell(
                    work = work,
                    spreadIdx = spreadIdx,
                    controller = controller,
                    viewW = viewW,
                    viewH = viewH,
                    urlOf = ::pageUrl,
                    imageLoader = sc.imageLoader,
                    onError = { controller.recordError(it) },
                )
            }
        }

        // ── chrome ──
        val info = work.chapterInfo(work.spreads.getOrNull(pager.settledPage)?.first() ?: 0)
        AnimatedVisibility(
            visible = chrome,
            modifier = Modifier.align(Alignment.TopCenter),
            enter = fadeIn() + slideInVertically { -it },
            exit = fadeOut() + slideOutVertically { -it },
        ) {
            Surface(color = Color(0xCC1A1A1A)) {
                Row(
                    Modifier.fillMaxWidth().padding(horizontal = 4.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack,
                            contentDescription = stringResource(R.string.back),
                            tint = Color.White)
                    }
                    Text(
                        info.name.ifEmpty {
                            BrowseRules.splitDisplayName(work.clickedPath).first
                        },
                        color = Color.White,
                        style = MaterialTheme.typography.titleMedium,
                        maxLines = 1,
                        modifier = Modifier.weight(1f),
                    )
                    Box {
                        IconButton(onClick = { menu = true }) {
                            Icon(Icons.Filled.MoreVert, null, tint = Color.White)
                        }
                        ReaderMenu(
                            expanded = menu,
                            landscape = viewW >= viewH,
                            spread = work.pageMode == dev.bookq.core.reader.ReaderPageMode.SPREAD,
                            landscapeSpread = landscapeSpread,
                            spreadOffset = spreadOffset,
                            tapForwardLeft = tapForwardLeft,
                            original = original,
                            onDismiss = { menu = false },
                            onOverview = ::openOverview,
                            onToggleSpread = { controller.toggleLandscapeSpread() },
                            onToggleOffset = { controller.toggleSpreadOffset() },
                            onToggleTap = { controller.toggleTapForward() },
                            onToggleOriginal = {
                                compose.launch { graph.settings.setOriginalImages(!original) }
                            },
                        )
                    }
                }
            }
        }

        AnimatedVisibility(
            visible = chrome,
            modifier = Modifier.align(Alignment.BottomCenter),
            enter = fadeIn() + slideInVertically { it },
            exit = fadeOut() + slideOutVertically { it },
        ) {
            Surface(color = Color(0xCC1A1A1A)) {
                val range = work.chapterSpreadRange(
                    work.spreads.getOrNull(pager.settledPage)?.first() ?: 0)
                val cur = work.spreadOf(
                    work.spreads.getOrNull(pager.settledPage)?.first() ?: 0)
                Row(
                    Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 4.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(12.dp),
                ) {
                    // Chapter-scoped slider (web `syncChapterSlider`),
                    // RTL to match the page order.
                    CompositionLocalProvider(
                        LocalLayoutDirection provides LayoutDirection.Rtl) {
                        val span = (range.last - range.first).coerceAtLeast(0)
                        Slider(
                            value = (cur - range.first).toFloat()
                                .coerceIn(0f, span.toFloat()),
                            onValueChange = { v ->
                                compose.launch {
                                    pager.scrollToPage(range.first + v.roundToInt())
                                }
                            },
                            valueRange = 0f..span.toFloat().coerceAtLeast(1f),
                            enabled = span > 0,
                            modifier = Modifier.weight(1f),
                        )
                    }
                    // iOS counterText: chapter-local when cross-chapter,
                    // else global real index. ⚠ marks a failed page in
                    // the current spread (web counter semantics).
                    val failed by controller.failedPages.collectAsStateWithLifecycle()
                    val gi = work.spreads.getOrNull(pager.settledPage)?.first() ?: 0
                    Text(
                        buildString {
                            if (work.isCrossChapter) {
                                val inf = work.chapterInfo(gi)
                                append(stringResource(R.string.reader_counter,
                                    inf.localIdx + 1,
                                    work.chapters.getOrNull(inf.chapterIdx)
                                        ?.images?.size ?: 0))
                            } else {
                                append(stringResource(R.string.reader_counter,
                                    work.realIndex(gi) + 1, work.realCount))
                            }
                            if (work.spreads.getOrNull(pager.settledPage)
                                    ?.any { it in failed } == true) append(" ⚠")
                        },
                        color = Color.White,
                        style = MaterialTheme.typography.labelLarge,
                    )
                }
            }
        }

        // ── M4 overview grid — drawn on top of the reader, consumes its
        // own gestures/keys; reader chrome sits inert underneath. ──
        if (overviewOpen) {
            OverviewOverlay(
                work = work,
                currentIdx = curGlobalIdx(),
                api = sc.api,
                imageLoader = sc.imageLoader,
                onJump = ::exitOverview,
                onClose = { exitOverview(curGlobalIdx()) },
            )
        }
    }
}

/** One pager page = one spread (1 or 2 cells, RTL order: pages[0]
 *  renders rightmost — the web's `row-reverse`). */
@Composable
private fun SpreadCell(
    work: ReaderWork,
    spreadIdx: Int,
    controller: ReaderController,
    viewW: Float,
    viewH: Float,
    urlOf: (Int) -> String,
    imageLoader: coil3.ImageLoader,
    onError: (Int) -> Unit,
) {
    val pages = work.spreads.getOrNull(spreadIdx) ?: return
    val ars by controller.pageAR.collectAsStateWithLifecycle()
    val density = LocalDensity.current
    val (fitW, fitH) = remember(pages, ars, viewW, viewH) {
        ReaderWork.spreadFit(pages.map { ars[it] ?: controller.lastAR },
            GAP_INNER_PX, viewW, viewH)
    }
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        // The pager's RTL LayoutDirection is inherited: the Row lays its
        // first child at the right edge — web's `row-reverse` for free.
        Row {
            for (idx in pages) {
                val ar = ars[idx] ?: controller.lastAR
                val w = with(density) { (ar * fitH).toDp() }
                val h = with(density) { fitH.toDp() }
                PageCell(
                    globalIdx = idx,
                    url = urlOf(idx),
                    width = w,
                    height = h,
                    imageLoader = imageLoader,
                    isChapterStart = idx == pages.first() &&
                        spreadIdx in work.chapterStartSpreads,
                    chapterName = work.chapterInfo(idx).name,
                    onAR = { a -> controller.recordAR(idx, a) },
                    onError = { onError(idx) },
                )
            }
        }
    }
}

@Composable
private fun PageCell(
    globalIdx: Int,
    url: String,
    width: androidx.compose.ui.unit.Dp,
    height: androidx.compose.ui.unit.Dp,
    imageLoader: coil3.ImageLoader,
    isChapterStart: Boolean,
    chapterName: String,
    onAR: (Float) -> Unit,
    onError: () -> Unit,
) {
    val context = LocalContext.current
    val request = remember(url) { ImageRequest.Builder(context).data(url).build() }
    Box(
        Modifier.width(width).height(height).background(Color(0xFF161616)),
        contentAlignment = Alignment.Center,
    ) {
        SubcomposeAsyncImage(
            model = request,
            imageLoader = imageLoader,
            contentDescription = null,
            contentScale = ContentScale.Fit,
            onSuccess = { state ->
                val s = state.painter.intrinsicSize
                if (s.width > 0f && s.height > 0f) onAR(s.width / s.height)
            },
            onError = { onError() },
            loading = {},
            error = {
                // Web's ⚠ marker on a failed page cell.
                Text("⚠", color = Color(0xFFB0B0B0),
                    style = MaterialTheme.typography.headlineMedium)
            },
        )
        if (isChapterStart && chapterName.isNotEmpty()) {
            // `spread.js` paints the chapter title on boundary spreads.
            Text(
                chapterName,
                color = Color(0x99FFFFFF),
                style = MaterialTheme.typography.labelMedium,
                modifier = Modifier
                    .align(Alignment.BottomStart)
                    .padding(4.dp),
            )
        }
    }
}

@Composable
private fun ReaderMenu(
    expanded: Boolean,
    landscape: Boolean,
    spread: Boolean,
    landscapeSpread: Boolean,
    spreadOffset: Int,
    tapForwardLeft: Boolean,
    original: Boolean,
    onDismiss: () -> Unit,
    onOverview: () -> Unit,
    onToggleSpread: () -> Unit,
    onToggleOffset: () -> Unit,
    onToggleTap: () -> Unit,
    onToggleOriginal: () -> Unit,
) {
    DropdownMenu(expanded = expanded, onDismissRequest = onDismiss) {
        // Web's overview button (g/#btn-overview) — the page grid.
        DropdownMenuItem(
            text = { Text(stringResource(R.string.reader_overview)) },
            onClick = { onDismiss(); onOverview() },
        )
        if (landscape) {
            DropdownMenuItem(
                text = {
                    Row(verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Text(stringResource(R.string.reader_spread_toggle),
                            Modifier.weight(1f))
                        Switch(checked = landscapeSpread, onCheckedChange = null)
                    }
                },
                onClick = { onToggleSpread() },
            )
            if (spread) {
                DropdownMenuItem(
                    text = { Text(stringResource(R.string.reader_spread_shift)) },
                    onClick = { onToggleOffset() },
                )
            }
        }
        DropdownMenuItem(
            text = {
                Text(stringResource(
                    if (tapForwardLeft) R.string.reader_tap_left_forward
                    else R.string.reader_tap_right_forward))
            },
            onClick = { onToggleTap() },
        )
        DropdownMenuItem(
            text = {
                Row(verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(stringResource(R.string.settings_original_images),
                        Modifier.weight(1f))
                    Switch(checked = original, onCheckedChange = null)
                }
            },
            onClick = { onToggleOriginal() },
        )
    }
}

@Composable
private fun ReaderEmpty(onBack: () -> Unit) {
    BackHandler { onBack() }
    Box(Modifier.fillMaxSize().background(Color(0xFF101010)),
        contentAlignment = Alignment.Center) {
        Text(stringResource(R.string.reader_empty), color = Color.White)
    }
}

@Composable
private fun ReaderError(message: String, onBack: () -> Unit) {
    BackHandler { onBack() }
    Box(Modifier.fillMaxSize().background(Color(0xFF101010)),
        contentAlignment = Alignment.Center) {
        Text(message, color = MaterialTheme.colorScheme.error)
    }
}

/** Hide system bars while chrome is hidden — reader chrome doubles as
 *  the system-bar toggle, matching the web's immersive behavior. */
@Composable
private fun Immersive(chrome: Boolean) {
    val view = LocalView.current
    DisposableEffect(chrome) {
        val window = (view.context as? Activity)?.window
            ?: return@DisposableEffect onDispose {}
        val c = WindowCompat.getInsetsController(window, view)
        c.systemBarsBehavior =
            WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
        if (chrome) c.show(WindowInsetsCompat.Type.systemBars())
        else c.hide(WindowInsetsCompat.Type.systemBars())
        onDispose { c.show(WindowInsetsCompat.Type.systemBars()) }
    }
}
