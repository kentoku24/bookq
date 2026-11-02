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
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
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
import dev.bookq.reader.R
import dev.bookq.reader.ServerScope
import dev.bookq.reader.browse.BrowseController
import androidx.compose.ui.platform.LocalContext
import kotlinx.coroutines.launch

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
        onNavigate, onOpenReader, onSettings, onBack, graph,
    )
}

@OptIn(ExperimentalMaterial3Api::class)
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
    var facetOpen by remember { mutableStateOf(false) }

    // Editable roots = current top-level tree paths (web setTopLevelPaths).
    LaunchedEffect(tree.nodes) {
        vis.setTopLevelPaths(BrowseRules.foldersAt("", tree.nodes).map { it.first })
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
        onOpenReader(p, page)
    }

    fun handle(action: BrowseController.Action) {
        when (action) {
            is BrowseController.Action.Navigate -> onNavigate(action.path)
            is BrowseController.Action.Open -> openTarget(action.path)
            BrowseController.Action.Denied ->
                coroutineScope.launch { snackbar.showSnackbar("非表示フォルダです") }
            BrowseController.Action.Queued -> Unit
            null -> Unit
        }
    }

    // Search-result tap → same tap path as a folder tile (nav/open).
    fun openResult(meta: dev.bookq.core.api.ComicMetadata) {
        scope.search.clear()
        searchOpen = false
        val node = BrowseRules.nodeAt(meta.folderPath, tree.nodes)
        handle(controller.tap(meta.folderPath,
            node?.let { BrowseRules.drillsDown(it) } ?: false, tree, vis))
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
    val listState = rememberLazyListState()
    val gridState = rememberLazyGridState()
    LaunchedEffect(Unit) {
        val anchor = entry.savedStateHandle.get<String>(RETURN_ANCHOR_KEY)
        if (anchor != null) {
            entry.savedStateHandle.remove<String>(RETURN_ANCHOR_KEY)
            controller.decider.noteViewerReturn(path, anchor)
        }
        val restored = listState.firstVisibleItemIndex > 0 ||
            listState.firstVisibleItemScrollOffset > 0 ||
            gridState.firstVisibleItemIndex > 0 ||
            gridState.firstVisibleItemScrollOffset > 0
        val childPaths = allItems.map { it.path }
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
        suspend fun scrollTo(i: Int) {
            if (i >= 0) {
                listState.scrollToItem(i)
                gridState.scrollToItem(i)
            }
        }
        when (decision.via) {
            ScrollDecider.Via.VIEWER, ScrollDecider.Via.PROGRESS ->
                if (index >= 0) scrollTo(index)
            ScrollDecider.Via.TOP -> if (!restored) scrollTo(0)
            ScrollDecider.Via.SAVED -> Unit // framework already restored
        }
    }

    // Drain the shallow/PENDING navigation queue once the tree is usable.
    LaunchedEffect(tree.treeLoaded, visVersion) {
        controller.drain(tree.nodes, tree, vis)?.let(::handle)
    }

    // New tree data ends the pull-to-refresh spinner.
    LaunchedEffect(tree.generation, tree.shallow) { refreshing = false }

    val density = LocalDensity.current.density
    val coverW = coverWidth(density)

    // Visible-first cover prefetch — fires only once the scroll settles
    // (web: idle-scheduled, ≤50, never mid-gesture). Coil dedupes memory-
    // cached hits; `enqueued` survives the entry for the session.
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
            var sent = 0
            for (i in first until minOf(first + 50, items.size)) {
                if (sent >= 50) break
                val item = items[i]
                val url = when (val c = LevelModel.coverOf(item.node)) {
                    is LevelModel.Cover.Single ->
                        scope.api.thumbUrl(item.path, c.name, coverW).toString()
                    is LevelModel.Cover.Sprite ->
                        scope.api.thumbSpriteUrl(
                            item.path, c.names, c.spec.rows, coverW).toString()
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
                            Icon(Icons.AutoMirrored.Filled.ArrowBack, null)
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
                                    Icon(Icons.Default.Close, null)
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
                                }) { Icon(Icons.Default.Close, null) }
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
                        // Facet sheet — web `#filter-btn`; badge dot when armed.
                        IconButton(onClick = {
                            scope.search.loadFacets(if (searchActive) "" else path)
                            facetOpen = true
                        }, modifier = Modifier.testTag("facetBtn")) {
                            Icon(Icons.Default.FilterList,
                                stringResource(R.string.browse_filters))
                        }
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
                scope.tree.refresh()
            },
            modifier = Modifier.fillMaxSize().padding(padding),
        ) {
            Column(Modifier.fillMaxSize()) {
                // Armed filter chips + visibility status row.
                FilterChipRow(selArtists, selTags) { k, v ->
                    scope.search.removeFilter(k, v)
                }
                scope.visibility.statusText()?.let { s ->
                    Text(s, style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.outline,
                        modifier = Modifier.padding(horizontal = 16.dp))
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
                when {
                    // Global search results replace the level grid.
                    searchActive -> SearchResultGrid(
                        results = searchResults,
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
    if (!vis.showAll) return
    val pres = vis.presentation(item.path)
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
                try {
                    scope.visibility.setHidden(item.path, !pres.hidden)
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
        modifier = Modifier.fillMaxSize(),
    ) {
        gridItemsIndexed(items, key = { _, it -> itemKey(it) }) { _, item ->
            ElevatedCard(
                onClick = { onTap(item) },
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
                        Text(leaf, style = MaterialTheme.typography.bodySmall,
                            maxLines = 1)
                        if (parent != null) {
                            Text(parent, style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.outline, maxLines = 1)
                        }
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
