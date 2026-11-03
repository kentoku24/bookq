package dev.bookq.reader.ui.browse

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyGridPrefetchStrategy
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.rememberLazyGridState
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.grid.itemsIndexed as gridItemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.FilterList
import androidx.compose.material.icons.filled.List
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ElevatedCard
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.withStyle
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.runtime.withFrameNanos
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.navigation.NavBackStackEntry
import android.content.pm.PackageManager
import android.os.Build
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.material3.AlertDialog
import androidx.core.content.ContextCompat
import coil3.request.ImageRequest
import dev.bookq.core.offline.OfflineRules
import dev.bookq.data.offline.WorkRecord
import dev.bookq.reader.offline.OfflineManager
import dev.bookq.reader.ui.offline.OfflineBadge
import dev.bookq.core.browse.BrowseRules
import dev.bookq.core.browse.LevelModel
import dev.bookq.core.browse.ScrollDecider
import dev.bookq.core.progress.ProgressBook
import dev.bookq.core.visibility.VisibilityState
import dev.bookq.reader.AppGraph
import dev.bookq.reader.BrowseLayout
import dev.bookq.reader.PerfTunables
import dev.bookq.reader.R
import dev.bookq.reader.ServerScope
import dev.bookq.reader.browse.BrowseController
import androidx.compose.ui.platform.LocalContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

/** Saved-state key the reader writes its chapter anchor into. */
const val RETURN_ANCHOR_KEY = "bookq.returnAnchor"

/**
 * One browse level (folder grid/list/cards) — `renderBrowse` port.
 * Each level is a Navigation destination, so per-entry `rememberSaveable`
 * restores scroll for free; [BrowseController] keeps the cross-entry
 * decision table and the shallow-window nav queue.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun BrowseScreen(
    graph: AppGraph,
    entry: NavBackStackEntry,
    path: String,
    onNavigate: (String) -> Unit,
    onOpenReader: (String, Int) -> Unit,
    onSettings: () -> Unit,
    onBack: () -> Unit,
    onTop: () -> Unit,
    onOpenPair: () -> Unit,
) {
    val scopeState by graph.serverScope.collectAsStateWithLifecycle()
    val scope = scopeState ?: run {
        Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            CircularProgressIndicator()
        }
        return
    }
    val controller = remember(scope) { BrowseController() }
    BrowseBody(
        scope, controller, entry, path,
        onNavigate, onOpenReader, onSettings, onBack, onTop, onOpenPair, graph,
    )
}

@OptIn(ExperimentalMaterial3Api::class, ExperimentalFoundationApi::class)
@Composable
private fun BrowseBody(
    scope: ServerScope,
    controller: BrowseController,
    entry: NavBackStackEntry,
    path: String,
    onNavigate: (String) -> Unit,
    onOpenReader: (String, Int) -> Unit,
    onSettings: () -> Unit,
    onBack: () -> Unit,
    onTop: () -> Unit,
    onOpenPair: () -> Unit,
    graph: AppGraph,
) {
    val tree by scope.tree.state.collectAsStateWithLifecycle()
    val visVersion by scope.visibility.version.collectAsStateWithLifecycle()
    val progressVersion by scope.progress.version.collectAsStateWithLifecycle()
    val layout by graph.settings.browseLayout.collectAsStateWithLifecycle()
    val vis = scope.visibility.vis
    val book = scope.progress.book
    val coroutineScope = rememberCoroutineScope()
    val snackbar = remember { SnackbarHostState() }

    var filter by rememberSaveable { mutableStateOf("") }
    var filterActive by rememberSaveable { mutableStateOf(false) }
    var menuOpen by remember { mutableStateOf(false) }
    var refreshing by remember { mutableStateOf(false) }

    // ── search/facets + show-all (M6) ──
    val searchQuery by scope.search.query.collectAsStateWithLifecycle()
    val searchActive by scope.search.active.collectAsStateWithLifecycle()
    val searchResults by scope.search.results.collectAsStateWithLifecycle()
    val searching by scope.search.searching.collectAsStateWithLifecycle()
    val selArtists by scope.search.artists.collectAsStateWithLifecycle()
    val selTags by scope.search.tags.collectAsStateWithLifecycle()
    val facets by scope.search.facets.collectAsStateWithLifecycle()
    var searchOpen by rememberSaveable { mutableStateOf(false) }
    var pairSheet by remember { mutableStateOf(false) }
    var facetOpen by remember { mutableStateOf(false) }

    // Back while searching closes the search before unwinding the level —
    // query/results live on ServerScope, so a parent entry would otherwise
    // surface the result grid with no field to close it.
    BackHandler(enabled = searchOpen || searchActive) {
        scope.search.clear()
        filter = ""
        searchOpen = false
    }

    // Editable roots = current top-level tree paths (web setTopLevelPaths).
    LaunchedEffect(tree.nodes) {
        vis.setTopLevelPaths(BrowseRules.foldersAt("", tree.nodes).map { it.first })
    }

    // `_visibilityOnChange` parity: any snapshot/mode change re-runs an
    // active search (include_hidden + result visibility moved); leaving
    // show-all under a hidden root bounces to the top instead of
    // stranding on an empty level (web redirectHiddenToTop).
    LaunchedEffect(visVersion) {
        if (searchActive) scope.search.filtersChanged()
        if (!vis.showAll && path.isNotEmpty() &&
            vis.guardPath(path) == VisibilityState.Guard.HIDDEN) {
            onTop()
        }
    }

    // ── offline downloads (M5) ──
    val originalImages by graph.settings.originalImages.collectAsStateWithLifecycle()
    val context = LocalContext.current
    val quotaFailMsg = stringResource(R.string.offline_quota_fail)
    var quotaPrompt by remember { mutableStateOf<Pair<String, WorkRecord>?>(null) }
    // POST_NOTIFICATIONS on first download (API 33+); denial never blocks.
    var pendingDownload by remember { mutableStateOf<String?>(null) }
    val notifPerm = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { pendingDownload?.let { f ->
        coroutineScope.launch { startDownload(scope, f, originalImages, onQuota = { quotaPrompt = f to it }) }
    }; pendingDownload = null }

    fun startDownloadFlow(folder: String) {
        pendingDownload = folder
        if (Build.VERSION.SDK_INT >= 33 && ContextCompat.checkSelfPermission(
                context, android.Manifest.permission.POST_NOTIFICATIONS
            ) != PackageManager.PERMISSION_GRANTED) {
            notifPerm.launch(android.Manifest.permission.POST_NOTIFICATIONS)
        } else {
            coroutineScope.launch {
                startDownload(scope, folder, originalImages,
                    onQuota = { quotaPrompt = folder to it })
            }
            pendingDownload = null
        }
    }

    LaunchedEffect(scope) {
        scope.offline.quotaEvents.collect { folder ->
            scope.offline.evictablePreview()?.let { quotaPrompt = folder to it }
        }
    }

    if (quotaPrompt != null) {
        val (folder, victim) = quotaPrompt!!
        AlertDialog(
            onDismissRequest = { quotaPrompt = null },
            title = { Text(stringResource(R.string.offline_quota_title)) },
            text = {
                Text(stringResource(R.string.offline_quota_body,
                    victim.folder, OfflineRules.fmt(victim.bytes)))
            },
            confirmButton = {
                TextButton(onClick = {
                    quotaPrompt = null
                    coroutineScope.launch {
                        when (scope.offline.evictAndRetry(folder, originalImages)) {
                            is OfflineManager.RequestResult.Rejected ->
                                snackbar.showSnackbar(quotaFailMsg)
                            else -> Unit
                        }
                    }
                }) { Text(stringResource(R.string.offline_quota_ok)) }
            },
            dismissButton = {
                TextButton(onClick = { quotaPrompt = null }) {
                    Text(stringResource(R.string.settings_cancel))
                }
            },
        )
    }

    if (pairSheet) {
        dev.bookq.reader.ui.pair.PairSheet(
            pair = scope.pair,
            wsBaseUrl = scope.api.baseUrl.toString().removeSuffix("/"),
            onDismiss = { pairSheet = false },
        )
    }

    // Item derivation — visibility version + progress version are the
    // recompute triggers (their mutations bump the ticks).
    val allItems = remember(tree.nodes, visVersion, path) {
        LevelModel.items(path, tree.nodes, vis)
    }
    val items = remember(allItems, filter, selArtists, selTags) {
        LevelModel.filterItems(allItems, filter).let { base ->
            if (!dev.bookq.core.search.FacetRules.filtersActive(
                    selArtists.toList(), selTags.toList())) base
            else base.filter { it.node.subtreeFacets != null &&
                dev.bookq.core.search.FacetRules.nodeMatches(
                    it.node, selArtists.toList(), selTags.toList()) }
        }
    }
    val blocked = remember(visVersion) { vis.blockedGrid() }

    fun openTarget(p: String) {
        val page = book[p]?.page ?: 0
        // Paired leader picking a work → OPEN_WORK on the wire instead of
        // a local reader (web: leader tap while paired). `pickWork` fails
        // closed for followers/visibility veto → local open.
        val pair = scope.pair
        if (pair.role == dev.bookq.core.sync.PairRole.leader &&
            pair.paired) {
            coroutineScope.launch {
                if (!pair.pickWork(p)) onOpenReader(p, page)
            }
        } else {
            onOpenReader(p, page)
        }
    }

    val deniedMsg = stringResource(R.string.browse_hidden_denied)
    fun handle(action: BrowseController.Action) {
        when (action) {
            is BrowseController.Action.Navigate -> onNavigate(action.path)
            is BrowseController.Action.Open -> openTarget(action.path)
            BrowseController.Action.Denied ->
                coroutineScope.launch { snackbar.showSnackbar(deniedMsg) }
            BrowseController.Action.Queued -> Unit
            null -> Unit
        }
    }

    // Search-result tap → same tap path as a folder tile (nav/open). An
    // unresolvable node (shallow window / flattened mid-path) defers the
    // open-vs-navigate decision to the drain.
    fun openResult(meta: dev.bookq.core.api.ComicMetadata) {
        scope.search.clear()
        searchOpen = false
        val node = BrowseRules.nodeAt(meta.folderPath, tree.nodes)
        handle(controller.tap(meta.folderPath,
            node?.let { BrowseRules.drillsDown(it) }, tree, vis))
    }

    fun tap(item: LevelModel.Item) {
        when (item) {
            is LevelModel.Item.ReadSelf ->
                handle(controller.tapSelf(item.path, tree, vis))
            is LevelModel.Item.Folder ->
                handle(controller.tap(item.path,
                    BrowseRules.drillsDown(item.node), tree, vis))
        }
    }

    // Viewer→browse return anchor + scroll decision (one-shot per entry).
    // Two contract details:
    //  - childPaths is the FILTERED list actually rendered — indexOf must
    //    agree with the Lazy* index space or the anchor scrolls wrong.
    //  - the decision waits for a populated frame: cold start composes
    //    before the tree arrives, and deciding on an empty list would
    //    pin the scroll to TOP.
    val listState = rememberLazyListState()
    // Prefetch off-screen tiles during scroll slack — at the panel's
    // 90 Hz the 11 ms frame budget leaves no room for mid-fling
    // subcomposition (issue #302).
    val gridState = rememberLazyGridState(
        prefetchStrategy = LazyGridPrefetchStrategy(nestedPrefetchItemCount = 6))
    val renderedItems by rememberUpdatedState(items)
    LaunchedEffect(Unit) {
        val anchor = entry.savedStateHandle.get<String>(RETURN_ANCHOR_KEY)
        if (anchor != null) {
            entry.savedStateHandle.remove<String>(RETURN_ANCHOR_KEY)
            controller.decider.noteViewerReturn(path, anchor)
        }
        val rendered = snapshotFlow {
            Triple(renderedItems, visVersion, tree.treeLoaded)
        }.first { (l, _, loaded) ->
            l.isNotEmpty() || (loaded && vis.blockedGrid() == null)
        }.first
        // Startup gate: cold start → library displayed (timeToFullDisplay).
        // The marker must land INSIDE a Choreographer doFrame — early
        // (before the items' frame ends) crashes StartupTimingQuery, and
        // late (after the app goes idle) leaves no doFrame ending after
        // it. withFrameNanos resumes during the next frame's callback;
        // if the pipeline already went idle, a manual invalidate
        // schedules one more doFrame so the marker still gets bounded.
        val act = context as? ComponentActivity
        if (withTimeoutOrNull(1_500) { withFrameNanos {}; true } == null) {
            // Pipeline already idle — schedule a doFrame so the marker
            // still lands inside one (metric needs a slice ending
            // after reportFullyDrawn).
            act?.window?.decorView?.postInvalidate()
            withTimeoutOrNull(1_500) { withFrameNanos {} }
        }
        act?.reportFullyDrawn()
        PerfTunables.mark("fullyDrawn")
        val restored = listState.firstVisibleItemIndex > 0 ||
            listState.firstVisibleItemScrollOffset > 0 ||
            gridState.firstVisibleItemIndex > 0 ||
            gridState.firstVisibleItemScrollOffset > 0
        val childPaths = rendered.map { it.path }
        val decision = controller.decider.decide(
            level = path.ifEmpty { null },
            cause = if (anchor != null || restored) {
                ScrollDecider.Cause.RESTORE
            } else {
                ScrollDecider.Cause.OPEN
            },
            hasSaved = restored,
            childPaths = childPaths,
            progressFolders = book.folders() + setOf(""),
        )
        val index = decision.anchor?.let { a -> childPaths.indexOf(a) } ?: -1
        // Scroll only the state backing the ACTIVE layout — scrollToItem
        // on a Lazy* state whose container isn't composed suspends until
        // first layout, which for the inactive one can wedge the effect.
        suspend fun scrollTo(i: Int) {
            if (i < 0) return
            if (layout == BrowseLayout.LIST) listState.scrollToItem(i)
            else gridState.scrollToItem(i)
        }
        when (decision.via) {
            ScrollDecider.Via.VIEWER, ScrollDecider.Via.PROGRESS ->
                if (index >= 0) scrollTo(index)
            ScrollDecider.Via.TOP -> if (!restored) scrollTo(0)
            ScrollDecider.Via.SAVED -> Unit // framework already restored
        }
    }

    // Drain the shallow/PENDING navigation queue once the tree is usable —
    // `pending` keys the effect too: a queue-on-arrival tap (unknown node
    // on a loaded tree) must resolve without waiting for new state.
    val pendingAction by controller.pending.collectAsStateWithLifecycle()
    LaunchedEffect(tree.treeLoaded, visVersion, pendingAction) {
        controller.drain(tree.nodes, tree, vis)?.let(::handle)
    }

    val density = LocalDensity.current.density
    val coverW = coverWidth(density)

    // Visible-first cover prefetch — fires only once the scroll settles
    // (web: idle-scheduled, ≤50, never mid-gesture). Coil dedupes memory-
    // cached hits; `enqueued` survives the entry for the session.
    // `items` is read through rememberUpdatedState: a settle event must
    // prefetch the CURRENT list, not the empty one from first compose.
    val enqueued = remember { mutableSetOf<String>() }
    LaunchedEffect(gridState, listState, layout) {
        snapshotFlow {
            val settled: Boolean
            val first: Int
            if (layout == BrowseLayout.LIST) {
                settled = !listState.isScrollInProgress
                first = listState.layoutInfo.visibleItemsInfo
                    .firstOrNull()?.index ?: -1
            } else {
                settled = !gridState.isScrollInProgress
                first = gridState.layoutInfo.visibleItemsInfo
                    .firstOrNull()?.index ?: -1
            }
            settled to first
        }.collect { (settled, first) ->
            if (!settled || first < 0) return@collect
            // CARD renders covers at card width (400); others use coverW.
            val w = if (layout == BrowseLayout.CARD) 400 else coverW
            var sent = 0
            val current = renderedItems
            for (i in first until minOf(first + 50, current.size)) {
                if (sent >= 50) break
                val item = current[i]
                val url = when (val c = LevelModel.coverOf(item.node)) {
                    is LevelModel.Cover.Single ->
                        scope.api.thumbUrl(item.path, c.name, w).toString()
                    is LevelModel.Cover.Sprite ->
                        scope.api.thumbSpriteUrl(
                            item.path, c.names, c.spec.rows, w).toString()
                    LevelModel.Cover.None -> null
                } ?: continue
                if (enqueued.add(url)) {
                    scope.imageLoader.enqueue(
                        ImageRequest.Builder(context).data(url).build())
                    sent++
                }
            }
        }
    }

    // ── UI_SYNC — browse scroll mirror (web browse/pairSync.js, #300) ──
    // Armed while paired in phase=selecting: sends the local scroll
    // metrics at a bounded cadence and maps the peer's normalized
    // progress onto this grid's estimated scroll range.
    val pair = scope.pair
    val pairPresence by pair.presence.collectAsStateWithLifecycle()
    val pairState by pair.lastState.collectAsStateWithLifecycle()
    val syncOn = pair.bound && pairPresence.size >= 2 &&
        pairState?.phase == dev.bookq.core.sync.PairPhase.selecting &&
        !searchActive && !searchOpen

    /** Estimated px metrics of the ACTIVE Lazy* container — grid rows
     *  and list rows share the (index,offset)→top projection; columns
     *  and pitch come from the visible items (Adaptive grids vary). */
    fun activeMetrics(): dev.bookq.core.sync.UiSync.Metrics? {
        if (layout == BrowseLayout.LIST) {
            val info = listState.layoutInfo
            val vis = info.visibleItemsInfo
            if (vis.isEmpty()) return null
            val ys = vis.map { it.offset }.distinct().sorted()
            val pitch = if (ys.size > 1) (ys[1] - ys[0]).toFloat()
                else vis.map { it.size }.average().toFloat()
            return dev.bookq.core.sync.UiSync.estimateGridMetrics(
                firstIndex = listState.firstVisibleItemIndex,
                firstOffsetPx = listState.firstVisibleItemScrollOffset,
                columns = 1, cellPx = pitch,
                totalItems = info.totalItemsCount,
                viewportPx = info.viewportSize.height)
        }
        val info = gridState.layoutInfo
        val vis = info.visibleItemsInfo
        if (vis.isEmpty()) return null
        val rowY = vis.first().offset.y
        val cols = vis.count { it.offset.y == rowY }.coerceAtLeast(1)
        val ys = vis.map { it.offset.y }.distinct().sorted()
        val pitch = if (ys.size > 1) (ys[1] - ys[0]).toFloat()
            else vis.map { it.size.height }.average().toFloat()
        return dev.bookq.core.sync.UiSync.estimateGridMetrics(
            firstIndex = gridState.firstVisibleItemIndex,
            firstOffsetPx = gridState.firstVisibleItemScrollOffset,
            columns = cols, cellPx = pitch,
            totalItems = info.totalItemsCount,
            viewportPx = info.viewportSize.height)
    }

    var uiSyncUnlocked by remember { mutableStateOf(false) }
    var pendingUiSync by remember {
        mutableStateOf<dev.bookq.core.sync.UiSync.Frame?>(null) }
    var pendingUiSyncAt = remember { longArrayOf(0L) }
    // echo suppression — web _appliedHold: while parked at the position
    // a remote apply put us in, do not send it back (a clamped local
    // value would drag the peer). Cleared on the next real send.
    val appliedHoldTop = remember { doubleArrayOf(Double.NaN) }
    val lastLocalScrollMs = remember { longArrayOf(-1L) }

    fun localBusy(): Boolean {
        val busy = if (layout == BrowseLayout.LIST)
            listState.isScrollInProgress else gridState.isScrollInProgress
        val last = lastLocalScrollMs[0]
        return busy || (last >= 0 &&
            System.currentTimeMillis() - last < 400)
    }

    /** pitch + columns of the ACTIVE container (for remoteTop→index). */
    fun activeShape(): Pair<Int, Float> {
        if (layout == BrowseLayout.LIST) {
            val vis = listState.layoutInfo.visibleItemsInfo
            val ys = vis.map { it.offset }.distinct().sorted()
            return 1 to (if (ys.size > 1) (ys[1] - ys[0]).toFloat()
                else vis.map { it.size }.average().toFloat())
        }
        val vis = gridState.layoutInfo.visibleItemsInfo
        val rowY = vis.firstOrNull()?.offset?.y ?: return 1 to 0f
        val cols = vis.count { it.offset.y == rowY }.coerceAtLeast(1)
        val ys = vis.map { it.offset.y }.distinct().sorted()
        return cols to (if (ys.size > 1) (ys[1] - ys[0]).toFloat()
            else vis.map { it.size.height }.average().toFloat())
    }

    suspend fun applyRemoteScroll(f: dev.bookq.core.sync.UiSync.Frame) {
        val m = activeMetrics() ?: return
        val t = dev.bookq.core.sync.UiSync.remoteTop(
            localViewH = m.viewH, localScrollH = m.scrollH,
            top = f.top, viewH = f.viewH, scrollH = f.scrollH,
            follower = pair.role == dev.bookq.core.sync.PairRole.follower)
        val (cols, cellPx) = activeShape()
        if (cellPx <= 0f) return
        val (idx, off) = dev.bookq.core.sync.UiSync.targetIndexOffset(
            t, cols, cellPx, renderedItems.size)
        if (layout == BrowseLayout.LIST) listState.scrollToItem(idx, off)
        else gridState.scrollToItem(idx, off)
        appliedHoldTop[0] = t
    }

    // Receive → navigate or scroll.
    LaunchedEffect(syncOn) {
        if (!syncOn) return@LaunchedEffect
        pair.uiSync.collect { f ->
            val wantPath = f.path ?: ""
            if (wantPath != path ||
                (f.mode != null && f.mode != layout.name.lowercase())) {
                pendingUiSync = f
                pendingUiSyncAt[0] = System.currentTimeMillis()
                f.mode?.let { m ->
                    runCatching {
                        graph.settings.setBrowseLayout(
                            BrowseLayout.valueOf(m.uppercase()))
                    }
                }
                if (wantPath != path) onNavigate(wantPath)
            } else if (!localBusy()) {
                applyRemoteScroll(f)
                uiSyncUnlocked = true
            }
        }
    }
    // Post-nav pending apply — the frame stashed while the destination
    // composed; dropped when it ages out (web pendingMsg TTL ~2s).
    LaunchedEffect(path, layout, syncOn, renderedItems) {
        val f = pendingUiSync ?: return@LaunchedEffect
        if (!syncOn || f.path != (path.ifEmpty { null }) ||
            (f.mode != null && f.mode != layout.name.lowercase()) ||
            renderedItems.isEmpty() ||
            System.currentTimeMillis() - pendingUiSyncAt[0] > 2000) {
            if (System.currentTimeMillis() - pendingUiSyncAt[0] > 2000)
                pendingUiSync = null
            return@LaunchedEffect
        }
        pendingUiSync = null
        applyRemoteScroll(f)
        uiSyncUnlocked = true
    }
    // leader unlocks on arm; follower unlocks on first remote apply —
    // a follower sending stale position would drag the leader.
    LaunchedEffect(syncOn) {
        uiSyncUnlocked = pair.role == dev.bookq.core.sync.PairRole.leader
        if (!syncOn) return@LaunchedEffect
        var lastSentMs = 0L
        var lastSig = ""
        snapshotFlow {
            val m = activeMetrics()
            val busy = if (layout == BrowseLayout.LIST)
                listState.isScrollInProgress
            else gridState.isScrollInProgress
            m?.let { Triple(it, busy, System.currentTimeMillis()) }
        }.collectLatest { sm ->
            val (m, busy, now) = sm ?: return@collectLatest
            if (busy) lastLocalScrollMs[0] = now
            val sig = "${path}|${m.top.toLong()}|${m.viewH.toLong()}|" +
                "${m.scrollH.toLong()}|${layout.name}"
            val held = kotlin.math.abs(m.top - appliedHoldTop[0]) <= 1.0
            if (!uiSyncUnlocked || held || sig == lastSig) return@collectLatest
            if (now - lastSentMs < 60) {
                // trailing send — a newer emission replaces this one
                delay(300)
                val m2 = activeMetrics() ?: return@collectLatest
                if (pair.sendUiSync(path.ifEmpty { null }, m2.top,
                        m2.viewH, m2.scrollH,
                        layout.name.lowercase())) {
                    lastSentMs = System.currentTimeMillis()
                    lastSig = "${path}|${m2.top.toLong()}|" +
                        "${m2.viewH.toLong()}|${m2.scrollH.toLong()}|" +
                        layout.name
                }
            } else if (pair.sendUiSync(path.ifEmpty { null }, m.top,
                    m.viewH, m.scrollH, layout.name.lowercase())) {
                lastSentMs = now; lastSig = sig
            }
        }
    }

    val title = if (path.isEmpty()) {
        stringResource(R.string.home_title)
    } else {
        BrowseRules.splitDisplayName(path.substringAfterLast('/')).first
    }

    Scaffold(
        snackbarHost = { SnackbarHost(snackbar) },
        topBar = {
            TopAppBar(
                navigationIcon = {
                    if (path.isNotEmpty()) {
                        IconButton(onClick = onBack) {
                            Icon(Icons.AutoMirrored.Filled.ArrowBack,
                                stringResource(R.string.back))
                        }
                    }
                },
                title = {
                    if (searchOpen) {
                        OutlinedTextField(
                            value = searchQuery,
                            onValueChange = { scope.search.setQuery(it) },
                            modifier = Modifier.fillMaxWidth().testTag("searchField"),
                            singleLine = true,
                            placeholder = { Text(stringResource(R.string.browse_search_hint)) },
                            trailingIcon = {
                                IconButton(onClick = {
                                    scope.search.clear()
                                    filter = ""
                                    searchOpen = false
                                }, modifier = Modifier.testTag("searchClose")) {
                                    Icon(Icons.Default.Close,
                                        stringResource(R.string.browse_search_close))
                                }
                            },
                        )
                    } else if (filterActive) {
                        OutlinedTextField(
                            value = filter,
                            onValueChange = { filter = it },
                            modifier = Modifier.fillMaxWidth(),
                            singleLine = true,
                            placeholder = { Text(stringResource(R.string.browse_filter_hint)) },
                            trailingIcon = {
                                IconButton(onClick = {
                                    filter = ""
                                    filterActive = false
                                }) { Icon(Icons.Default.Close,
                                    stringResource(R.string.browse_filter_close)) }
                            },
                        )
                    } else {
                        Text(title, maxLines = 1)
                    }
                },
                actions = {
                    if (!searchOpen && !filterActive) {
                        IconButton(onClick = { searchOpen = true },
                            modifier = Modifier.testTag("searchBtn")) {
                            Icon(Icons.Default.Search,
                                stringResource(R.string.browse_search))
                        }
                    }
                    // Facet sheet — web `#filter-btn` stays available in
                    // search mode (search × facet compose); badge dot
                    // when armed.
                    IconButton(onClick = {
                        scope.search.loadFacets(if (searchActive) "" else path)
                        facetOpen = true
                    }, modifier = Modifier.testTag("facetBtn")) {
                        Icon(Icons.Default.FilterList,
                            stringResource(R.string.browse_filters))
                    }
                    Box {
                        IconButton(onClick = { menuOpen = true },
                            modifier = Modifier.testTag("layoutMenuBtn")) {
                            Icon(Icons.Default.List,
                                stringResource(R.string.settings_browse_layout))
                        }
                        DropdownMenu(
                            expanded = menuOpen,
                            onDismissRequest = { menuOpen = false },
                        ) {
                            // Dual-reader pairing entry.
                            DropdownMenuItem(
                                text = { Text("ペアリング") },
                                onClick = {
                                    pairSheet = true
                                    menuOpen = false
                                },
                                modifier = Modifier.testTag("pairMenuItem"),
                            )
                            // Show-all entry — iOS menu parity (web edge-swipe).
                            DropdownMenuItem(
                                text = { Text(stringResource(
                                    if (vis.showAll) R.string.vis_hide_mode_off
                                    else R.string.vis_hide_mode_on)) },
                                onClick = {
                                    scope.visibility.setShowAll(!vis.showAll)
                                    menuOpen = false
                                },
                                modifier = Modifier.testTag("showAllMenuItem"),
                            )
                            // In-level name filter (iOS `.searchable` parity).
                            DropdownMenuItem(
                                text = { Text(stringResource(R.string.browse_filter_hint)) },
                                onClick = {
                                    filterActive = true
                                    menuOpen = false
                                },
                            )
                            HorizontalDivider()
                            BrowseLayout.entries.forEach { mode ->
                                DropdownMenuItem(
                                    text = {
                                        Text(when (mode) {
                                            BrowseLayout.TILE -> stringResource(R.string.settings_layout_tile)
                                            BrowseLayout.LIST -> stringResource(R.string.settings_layout_list)
                                            BrowseLayout.CARD -> stringResource(R.string.settings_layout_card)
                                        })
                                    },
                                    onClick = {
                                        coroutineScope.launch {
                                            graph.settings.setBrowseLayout(mode)
                                        }
                                        menuOpen = false
                                    },
                                )
                            }
                        }
                    }
                    IconButton(onClick = onSettings) {
                        Icon(Icons.Default.Settings,
                            stringResource(R.string.settings_title))
                    }
                },
            )
        },
    ) { padding ->
        PullToRefreshBox(
            isRefreshing = refreshing,
            onRefresh = {
                refreshing = true
                // Job-based completion — clears on success AND failure
                // (a generation-based trigger can stale forever when the
                // 202 window outlives the request).
                scope.tree.refresh().invokeOnCompletion { refreshing = false }
            },
            modifier = Modifier.fillMaxSize().padding(padding),
        ) {
            Column(Modifier.fillMaxSize()) {
                // Armed filter chips + visibility status row.
                FilterChipRow(selArtists, selTags) { k, v ->
                    scope.search.removeFilter(k, v)
                }
                scope.visibility.statusText()?.let { s ->
                    Row(verticalAlignment = Alignment.CenterVertically,
                        modifier = Modifier.padding(horizontal = 16.dp)) {
                        Text(s, style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.outline)
                        // Web `_updateStatusDom` retry affordance.
                        if (scope.visibility.lastError != null ||
                            vis.status == VisibilityState.Status.ERROR) {
                            TextButton(onClick = { scope.visibility.retry() },
                                modifier = Modifier.testTag("visRetry")) {
                                Text(stringResource(R.string.home_reload),
                                    style = MaterialTheme.typography.labelSmall)
                            }
                        }
                    }
                }
                // 続きから resume pill — root level only (web parity).
                if (path.isEmpty() && !searchActive) {
                    val resume = remember(progressVersion, visVersion) {
                        book.resumeCandidate { vis.isVisible(it) }
                    }
                    if (resume != null && blocked == null) {
                        ElevatedCard(
                            onClick = { openTarget(resume.folder) },
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(horizontal = 16.dp, vertical = 8.dp)
                                .testTag("resumePill"),
                        ) {
                            Text(
                                ProgressBook.resumeLabel(resume),
                                style = MaterialTheme.typography.bodyMedium,
                                modifier = Modifier.padding(12.dp),
                            )
                        }
                    }
                }
                // 同期中 banner — a bound session stays reachable from
                // browse (web sidebar parity); tap re-enters the paired
                // reader.
                val pairState by scope.pair.status.collectAsStateWithLifecycle()
                val pairSnap by scope.pair.lastState.collectAsStateWithLifecycle()
                if (scope.pair.bound && pairSnap != null) {
                    Surface(
                        onClick = onOpenPair,
                        color = MaterialTheme.colorScheme.tertiaryContainer,
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(horizontal = 16.dp, vertical = 4.dp)
                            .testTag("pairBanner"),
                    ) {
                        Text(
                            "同期中" + (pairSnap?.phase?.let {
                                " — " + when (it) {
                                    dev.bookq.core.sync.PairPhase.waiting -> "待機中"
                                    dev.bookq.core.sync.PairPhase.selecting -> "作品選択中"
                                    dev.bookq.core.sync.PairPhase.loading -> "読み込み中"
                                    dev.bookq.core.sync.PairPhase.reading -> "同期読書中"
                                    dev.bookq.core.sync.PairPhase.paused -> "一時停止"
                                    dev.bookq.core.sync.PairPhase.ended -> "終了"
                                }
                            } ?: ""),
                            style = MaterialTheme.typography.bodyMedium,
                            modifier = Modifier.padding(12.dp),
                        )
                    }
                }
                // `_visibleResults` — render-time isVisible on top of the
                // server list, so a hidden root drops out the moment the
                // show-all toggle flips (before the re-search lands).
                val visibleResults = remember(searchResults, visVersion) {
                    searchResults.filter { vis.isVisible(it.folderPath) }
                }
                when {
                    // Global search results replace the level grid.
                    searchActive -> SearchResultGrid(
                        results = visibleResults,
                        nodes = tree.nodes,
                        scope = scope,
                        coverW = coverW,
                        onTap = ::openResult,
                        modifier = Modifier.fillMaxSize(),
                        badge = { meta, node ->
                            if (node != null) {
                                val it = LevelModel.Item.Folder(meta.folderPath, node)
                                OfflineBadgeFor(it, scope) { f -> startDownloadFlow(f) }
                            }
                        },
                    )
                    searching && searchOpen -> CenterBox {
                        CircularProgressIndicator()
                    }
                    blocked == VisibilityState.BlockedGrid.Loading &&
                        allItems.isEmpty() -> CenterBox {
                        CircularProgressIndicator()
                    }
                    blocked == VisibilityState.BlockedGrid.Error &&
                        allItems.isEmpty() -> CenterBox {
                        Column(horizontalAlignment = Alignment.CenterHorizontally) {
                            Text(stringResource(R.string.home_error),
                                color = MaterialTheme.colorScheme.error)
                            TextButton(onClick = { scope.visibility.refresh() }) {
                                Text(stringResource(R.string.home_reload))
                            }
                        }
                    }
                    tree.error != null && allItems.isEmpty() -> CenterBox {
                        Column(horizontalAlignment = Alignment.CenterHorizontally) {
                            Text(tree.error!!, color = MaterialTheme.colorScheme.error)
                            TextButton(onClick = { scope.tree.load(force = true) }) {
                                Text(stringResource(R.string.home_reload))
                            }
                        }
                    }
                    items.isEmpty() && tree.treeLoaded -> CenterBox {
                        Text(stringResource(R.string.browse_empty),
                            color = MaterialTheme.colorScheme.outline)
                    }
                    items.isEmpty() && !tree.treeLoaded -> CenterBox {
                        CircularProgressIndicator()
                    }
                    else -> when (layout) {
                        BrowseLayout.LIST -> BrowseList(
                            items, listState, book, scope, coverW, ::tap,
                            badge = { OfflineBadgeFor(it, scope) { f -> startDownloadFlow(f) } },
                            overlay = { VisOverlayFor(it, vis, scope, snackbar) },
                        )
                        else -> BrowseGrid(
                            items, gridState, book, scope, coverW,
                            card = layout == BrowseLayout.CARD, onTap = ::tap,
                            badge = { OfflineBadgeFor(it, scope) { f -> startDownloadFlow(f) } },
                            overlay = { VisOverlayFor(it, vis, scope, snackbar) },
                        )
                    }
                }
            }
        }
    }

    if (facetOpen) {
        FacetSheet(
            facets = facets,
            artists = selArtists,
            tags = selTags,
            onToggleArtist = { scope.search.toggleArtist(it); scope.search.filtersChanged() },
            onToggleTag = { scope.search.toggleTag(it); scope.search.filtersChanged() },
            onReset = { scope.search.resetFilters() },
            onDismiss = { facetOpen = false },
        )
    }
}

/** Show-all tile overlay — badges + eye toggle, gated on showAll. */
@Composable
private fun VisOverlayFor(
    item: LevelModel.Item,
    vis: VisibilityState,
    scope: ServerScope,
    snackbar: SnackbarHostState,
) {
    // LazyGrid skips cells whose item is structurally equal — reading the
    // repo version registers a recompose dependency for showAll/snapshot
    // changes.
    scope.visibility.version.collectAsStateWithLifecycle().value
    if (!vis.showAll) return
    // ReadSelf tiles never own the eye toggle (web render.js: read-child
    // passes editable=false) — only the Folder tile may write.
    val pres = vis.presentation(item.path,
        editable = if (item is LevelModel.Item.ReadSelf) false else null)
    if (pres.badge == null && !pres.toggle) return
    val cs = rememberCoroutineScope()
    val busy = scope.visibility.saving
    val invalidMsg = stringResource(R.string.vis_save_failed)
    val conflictMsg = stringResource(R.string.vis_save_conflict)
    val offlineMsg = stringResource(R.string.vis_save_offline)
    VisibilityOverlay(
        pres = pres, path = item.path, busy = busy,
        onToggle = {
            cs.launch {
                // Web `setHidden` parity: unhide only on a SELF match and
                // send the SERVER-stored key — `set_hidden` deletes by
                // exact-match, so the NFC/lowercase display path would
                // 0-row the unhide of a differently-cased stored root.
                val match = vis.hiddenMatch(item.path)
                val (target, hidden) =
                    if (match?.kind == VisibilityState.MatchKind.SELF) {
                        match.key to false
                    } else {
                        item.path to true
                    }
                try {
                    scope.visibility.setHidden(target, hidden)
                } catch (e: dev.bookq.data.VisibilityRepository.SaveException) {
                    snackbar.showSnackbar(when (e.error) {
                        dev.bookq.data.VisibilityRepository.SaveError.CONFLICT ->
                            conflictMsg
                        dev.bookq.data.VisibilityRepository.SaveError.OFFLINE ->
                            offlineMsg
                        else -> invalidMsg
                    })
                }
            }
        },
    )
}

@Composable
private fun CenterBox(content: @Composable () -> Unit) {
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { content() }
}

private fun LevelModel.Item.progressPct(book: ProgressBook): Int? {
    val p = book[path] ?: return null
    return BrowseRules.progressPct(p.page, p.total)
}

/** Tile grid — `renderTiles` (2:3 covers) / `renderCards` (16:9) share
 *  one LazyVerticalGrid with different aspect ratios + min widths. */
@Composable
private fun BrowseGrid(
    items: List<LevelModel.Item>,
    gridState: androidx.compose.foundation.lazy.grid.LazyGridState,
    book: ProgressBook,
    scope: ServerScope,
    coverW: Int,
    card: Boolean,
    onTap: (LevelModel.Item) -> Unit,
    badge: @Composable (LevelModel.Item) -> Unit,
    overlay: @Composable (LevelModel.Item) -> Unit = {},
) {
    val minWidth = if (card) 240.dp else 140.dp
    LazyVerticalGrid(
        columns = GridCells.Adaptive(minWidth),
        state = gridState,
        contentPadding = PaddingValues(8.dp),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
        // UiAutomator locator for the Macrobenchmark grid-fling gate.
        modifier = Modifier.fillMaxSize().testTag("browseGrid"),
    ) {
        gridItemsIndexed(items, key = { _, it -> itemKey(it) }) { _, item ->
            // Flat tile (not ElevatedCard): per-tile shadow layers +
            // tonal providers were measurable per new row during flings
            // — web's tiles are flat anyway (#302).
            Surface(
                onClick = { onTap(item) },
                shape = MaterialTheme.shapes.medium,
                tonalElevation = 1.dp,
                modifier = Modifier.testTag("tile:${item.path}"),
            ) {
                Box(Modifier.fillMaxWidth().aspectRatio(if (card) 16f / 9f else 2f / 3f)) {
                    CoverImage(
                        cover = LevelModel.coverOf(item.node),
                        folder = item.path,
                        api = scope.api,
                        imageLoader = scope.imageLoader,
                        coverW = if (card) 400 else coverW,
                        modifier = Modifier.fillMaxSize(),
                        // Deferred attach while flinging — decode/upload
                        // bursts were the gridFling frame tail (#302).
                        attach = !gridState.isScrollInProgress,
                    )
                    Column(Modifier.align(Alignment.TopEnd).padding(6.dp),
                        horizontalAlignment = Alignment.End) {
                        badge(item)
                        overlay(item)
                    }
                }
                Row(
                    Modifier.fillMaxWidth().padding(8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Column(Modifier.weight(1f)) {
                        val (leaf, parent) = when (item) {
                            is LevelModel.Item.ReadSelf ->
                                BrowseRules.READ_SELF_LABEL to null
                            is LevelModel.Item.Folder ->
                                BrowseRules.splitDisplayName(item.node.name)
                        }
                        // ONE Text: leaf + parent in a single layout pass —
                        // each Text costs a StaticLayout (CJK shaping is the
                        // dominant gridFling frame cost on the trace).
                        Text(
                            text = buildAnnotatedString {
                                append(leaf)
                                if (parent != null) {
                                    append('\n')
                                    withStyle(
                                        androidx.compose.ui.text.SpanStyle(
                                            color = MaterialTheme.colorScheme
                                                .outline,
                                            fontSize = MaterialTheme.typography
                                                .labelSmall.fontSize,
                                        ),
                                    ) { append(parent) }
                                }
                            },
                            style = MaterialTheme.typography.bodySmall,
                            // leaf never wraps — it ellipsizes on line 1
                            // exactly like the two-Text layout did.
                            softWrap = false,
                            overflow = androidx.compose.ui.text.style
                                .TextOverflow.Ellipsis,
                            maxLines = 2,
                        )
                        item.progressPct(book)?.let { pct ->
                            Spacer(Modifier.height(4.dp))
                            LinearProgressIndicator(
                                progress = { pct / 100f },
                                modifier = Modifier.fillMaxWidth().height(3.dp),
                            )
                        }
                    }
                }
            }
        }
    }
}

/** List mode — `renderList`: icon + name + inline progress + end thumb. */
@Composable
private fun BrowseList(
    items: List<LevelModel.Item>,
    listState: androidx.compose.foundation.lazy.LazyListState,
    book: ProgressBook,
    scope: ServerScope,
    coverW: Int,
    onTap: (LevelModel.Item) -> Unit,
    badge: @Composable (LevelModel.Item) -> Unit,
    overlay: @Composable (LevelModel.Item) -> Unit = {},
) {
    LazyColumn(state = listState, modifier = Modifier.fillMaxSize()) {
        itemsIndexed(items, key = { _, it -> itemKey(it) }) { _, item ->
            Surface(onClick = { onTap(item) }, modifier = Modifier.testTag("tile:${item.path}")) {
                Row(
                    Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Column(Modifier.weight(1f)) {
                        val (leaf, _) = when (item) {
                            is LevelModel.Item.ReadSelf ->
                                BrowseRules.READ_SELF_LABEL to null
                            is LevelModel.Item.Folder ->
                                BrowseRules.splitDisplayName(item.node.name)
                        }
                        Text(leaf, style = MaterialTheme.typography.bodyLarge, maxLines = 1)
                        item.progressPct(book)?.let { pct ->
                            Spacer(Modifier.height(4.dp))
                            LinearProgressIndicator(
                                progress = { pct / 100f },
                                modifier = Modifier.fillMaxWidth().height(3.dp),
                            )
                        }
                    }
                    badge(item)
                    overlay(item)
                    Spacer(Modifier.width(12.dp))
                    Box(Modifier.size(48.dp)) {
                        CoverImage(
                            cover = LevelModel.coverOf(item.node),
                            folder = item.path,
                            api = scope.api,
                            imageLoader = scope.imageLoader,
                            coverW = coverW,
                            modifier = Modifier.fillMaxSize(),
                            attach = !listState.isScrollInProgress,
                        )
                    }
                }
            }
            HorizontalDivider()
        }
    }
}

private fun itemKey(item: LevelModel.Item): String =
    when (item) {
        is LevelModel.Item.ReadSelf -> "self:${item.path}"
        is LevelModel.Item.Folder -> "dir:${item.path}"
    }

/** Per-tile badge host — collects the manager's flows so badge/pct/menu
 *  recompose on record/transient changes. */
@Composable
private fun OfflineBadgeFor(
    item: LevelModel.Item,
    scope: ServerScope,
    onStart: (String) -> Unit,
) {
    val records by scope.offline.records.collectAsStateWithLifecycle()
    val trans by scope.offline.transient.collectAsStateWithLifecycle()
    val cs = rememberCoroutineScope()
    val offline = scope.offline
    val t = trans[item.path] ?: OfflineManager.Transient()
    val state = OfflineRules.btnState(
        folder = item.path,
        leaves = offline.leavesOf(item.path),
        recordOf = { f -> records[f]?.let { OfflineRules.LeafState(it.pageCount, it.cachedCount) } },
        confirmArmed = t.confirmUntil > System.currentTimeMillis(),
        deleting = t.deleting, stopping = t.stopping,
        downloading = t.downloading, listing = t.listing,
    )
    val pct = if (t.downloading && t.progressTotal > 0) {
        t.progressDone.toFloat() / t.progressTotal
    } else {
        OfflineRules.partialProgress(offline.leavesOf(item.path)) { f ->
            records[f]?.let { OfflineRules.LeafState(it.pageCount, it.cachedCount) }
        }.pct
    }
    OfflineBadge(
        state = state,
        pct = pct,
        partial = offline.partialProgress(item.path),
        tag = "offline:${item.path}",
        onIdle = { onStart(item.path) },
        onStop = { offline.stop(item.path) },
        onConfirmDelete = {
            if (offline.onBadgeTap(item.path, item.node)
                    is OfflineManager.TapResult.Delete) {
                cs.launch { offline.delete(item.path) }
            }
        },
        onContinue = { onStart(item.path) },
        onPartialDelete = { cs.launch { offline.delete(item.path) } },
    )
}

private suspend fun startDownload(
    scope: ServerScope,
    folder: String,
    original: Boolean,
    onQuota: (WorkRecord) -> Unit,
) {
    val r = scope.offline.requestDownload(folder, original)
    if (r is OfflineManager.RequestResult.NeedsEviction) onQuota(r.victim)
}
