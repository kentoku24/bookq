package dev.bookq.reader

import android.content.Context
import coil3.ImageLoader
import coil3.network.okhttp.OkHttpNetworkFetcherFactory
import dev.bookq.data.BookqApi
import dev.bookq.data.ProgressRepository
import dev.bookq.data.ScopedFiles
import dev.bookq.data.TreeRepository
import dev.bookq.data.VisibilityRepository
import java.io.File
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import okhttp3.HttpUrl
import okhttp3.OkHttpClient

/**
 * Per-(host, account) service bundle — the Android counterpart of the
 * web's localStorage-keyed stores. Recreated when the server URL or the
 * logged-in email changes; all file-backed stores live under
 * `filesDir/state/` with scope-sanitized names, so server/account
 * switches can never leak data across scopes.
 */
class ServerScope(
    context: Context,
    val api: BookqApi,
    host: String,
    email: String?,
    /** Media client (Bearer + refresh) shared by the API and Coil. */
    mediaClient: OkHttpClient,
    /** Live bearer for the reader-sync WS upgrade — auth.token. */
    tokenProvider: () -> String?,
    parent: CoroutineScope,
) {
    private val stateDir = File(context.filesDir, "state")

    private val scopeKey = ScopedFiles.key(host, email)

    /** PeerConnectionFactory — lazily created on first pair use (the
     *  native webrtc lib never loads for solo readers). */
    private val pairRtc = lazy { dev.bookq.data.PairRtc(context) }

    /** Authed client for raw page fetches (the download worker uses it —
     *  Coil got its own reference at build time). */
    val mediaClient: OkHttpClient = mediaClient

    /** Repo jobs die with the scope — not on screen teardown. IO-bound:
     *  inherits the parent's job but runs on Dispatchers.IO so
     *  decode/Retry-After sleeps never touch the main thread. */
    val ioScope = CoroutineScope(
        parent.coroutineContext + SupervisorJob(parent.coroutineContext[Job]) +
            Dispatchers.IO)

    val tree = TreeRepository(
        api = api,
        snapshotDir = stateDir,
        scope = ioScope,
        snapshot = TreeRepository.FileSnapshotIO(stateDir, host),
    )

    val visibility = VisibilityRepository(
        api = api,
        scope = ioScope,
        cache = VisibilityRepository.FileCacheIO(stateDir, host, email.orEmpty()),
    )

    /** Global metadata search + facet state — `search/facets.js` port. */
    val search = dev.bookq.data.SearchRepository(
        api = api,
        scope = ioScope,
        showAll = { visibility.vis.showAll },
    )

    val progress = ProgressRepository(
        api = api,
        scope = ioScope,
        store = ProgressRepository.FileStoreIO(
            stateDir, ScopedFiles.key(host, email)),
    )

    /** Offline downloads — JSON records + page files under
     *  `filesDir/offline/<scope>/`, badge state machine, WorkManager
     *  orchestration, local-first read path. */
    val offline = dev.bookq.reader.offline.OfflineManager(
        context = context,
        scope = ioScope,
        api = api,
        store = dev.bookq.data.offline.OfflineStore(
            filesDir = context.filesDir, scopeKey = scopeKey),
        scopeKey = scopeKey,
        roots = { tree.state.value.nodes },
    )

    /** Dual-reader sync — server-ws PairStore (iOS PairStore port).
     *  Bound at boot via bootResume() when persisted creds exist; the
     *  reconnect machine owns RESUME within the server's 300 s window. */
    val pair = dev.bookq.core.sync.PairStore(
        scope = ioScope,
        urlProvider = { api.readerSyncUrl() },
        tokenProvider = tokenProvider,
        transportFactory = { dev.bookq.data.PairSocket(mediaClient) },
        creds = dev.bookq.data.PairCredStore(stateDir, host, email.orEmpty()),
    ).apply {
        // webrtc-dc upgrade — the factory spins up the native stack on
        // the first paired STATE, not at scope boot (#300).
        rtcFactory = { pairRtc.value.peerConnection() }
        // Leader work-pick: saved position (clamped) or the first page —
        // same resolution the web's OPEN_WORK bootstrap uses.
        openWorkResolver = { folder ->
            val imgs = runCatching { api.images(folder) }.getOrNull()
                ?: emptyList()
            val want = progress.book[folder]?.page ?: 0
            imgs.getOrNull(want.coerceIn(0, (imgs.size - 1).coerceAtLeast(0)))
                ?.let { dev.bookq.core.sync.PairPageRef(folder, it) }
        }
        // Fresh-pair bootstrap: share the resume candidate (newest read).
        bootstrapWork = {
            val pos = progress.book.resumeCandidate { visibility.vis.isVisible(it) }
            pos?.let { p ->
                val imgs = runCatching { api.images(p.folder) }.getOrNull()
                    ?: emptyList()
                imgs.getOrNull(
                    p.page.coerceIn(0, (imgs.size - 1).coerceAtLeast(0)))
                    ?.let { dev.bookq.core.sync.PairPageRef(p.folder, it) to p.folder }
            }
        }
        // Hidden-root veto — web `_visBlocksFolder` parity.
        visibilityBlocks = { folder -> !visibility.vis.isVisible(folder) }
    }

    /** Coil with the authed client — `/thumb/` needs the same Bearer. */
    val imageLoader = ImageLoader.Builder(context)
        .components {
            add(OkHttpNetworkFetcherFactory(callFactory = { mediaClient }))
        }
        .build()

    /** Boot the scope — snapshot paint, two-stage tree, visibility, progress. */
    fun start() {
        tree.restoreSnapshot()
        tree.load()
        tree.startPolling()
        visibility.refresh()
        ioScope.launch { progress.pull() }
        pair.bootResume()
    }

    fun setForeground(resumed: Boolean) {
        tree.setForeground(resumed)
        // scenePhase parity — SET_ACTIVITY suspended while backgrounded.
        pair.onForeground(resumed)
        if (!resumed) {
            progress.flush()        // trailing 2 s server push
            // The 500 ms-debounced LOCAL write too — backgrounding can
            // precede process death, and a network flush alone leaves
            // the on-disk position stale.
            progress.flushPersist()
        }
    }

    fun close() {
        tree.stopPolling()
        if (pairRtc.isInitialized()) pairRtc.value.dispose()
        ioScope.cancel()
    }

    companion object {
        /** Boot-probe helper (AppModel.hasOfflineSnapshot) — exists purely
         *  so a failed boot can still show the cached library. */
        fun hasTreeSnapshot(context: Context, host: String): Boolean =
            TreeRepository.FileSnapshotIO(File(context.filesDir, "state"), host).exists()

    }
}
