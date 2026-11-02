package dev.bookq.data

import dev.bookq.core.api.BookqJson
import dev.bookq.core.api.TreeNode
import java.io.File
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString

/**
 * Folder-tree state — port of app.js `loadFolders` two-stage first paint
 * plus `LibraryStore` snapshot/refresh semantics:
 *
 *  - stage 1: `?depth=1` shallow envelope → paint immediately
 *    (`shallow = true`; children arrays are empty — drill-downs queue).
 *  - stage 2: full tree → replace + persist snapshot + `treeLoaded`.
 *  - failure after a shallow paint keeps the shallow grid; failure with no
 *    paint falls back to the host-scoped snapshot, else error.
 *  - poll: `?since=<generation>` every [pollIntervalMs] while the app is
 *    RESUMED — 304 skips work; iOS uses the same 15 s cadence (web's
 *    FillPoll is 5 s→30 s backoff; 15 s sits inside its quiet window so
 *    the server-side background fill is not starved).
 *
 * `_loadGen` (epoch) mirrors app.js `_loadFoldersGen`: a newer load()
 * supersedes every in-flight stage of an older one.
 */
class TreeRepository(
    private val api: BookqApi,
    private val snapshotDir: File,
    private val scope: CoroutineScope,
    private val pollIntervalMs: Long = POLL_INTERVAL_MS,
    private val snapshot: SnapshotIO = FileSnapshotIO(snapshotDir, api.baseUrl.host),
) {

    data class State(
        val nodes: List<TreeNode> = emptyList(),
        val generation: Int = 0,
        /** True between the shallow first paint and full-tree arrival. */
        val shallow: Boolean = false,
        /** app.js `treeLoaded` — false during the shallow window. */
        val treeLoaded: Boolean = false,
        /** A snapshot (this session's or a restored one) backs `nodes`. */
        val hasSnapshot: Boolean = false,
        val error: String? = null,
    )

    interface SnapshotIO {
        fun read(): String?
        fun write(text: String)
        fun exists(): Boolean
    }

    private val _state = MutableStateFlow(State())
    val state = _state.asStateFlow()

    @Volatile private var loadGen = 0
    @Volatile private var etag: String? = null
    private val loadMutex = Mutex()
    private var pollJob: Job? = null
    @Volatile private var foreground = true

    /** Last applied node list — needed by the poller's re-emit path. */

    /** Restore the host snapshot synchronously so cold start paints
     *  instantly (SW NetworkFirst parity). Returns true when applied. */
    fun restoreSnapshot(): Boolean {
        val text = snapshot.read() ?: return false
        val snap = runCatching {
            BookqJson.decodeFromString<TreeSnapshot>(text)
        }.getOrNull() ?: return false
        _state.value = State(
            nodes = snap.nodes,
            generation = snap.generation,
            shallow = false,
            treeLoaded = true,
            hasSnapshot = true,
        )
        return true
    }

    fun hasSnapshot(): Boolean = snapshot.exists()

    /**
     * Two-stage load. `preserve` is implicit: browse state lives in the
     * ViewModel, which re-derives it from [state].
     */
    fun load(force: Boolean = false): Job {
        val gen = ++loadGen
        return scope.launch {
            loadMutex.withLock {
                var shallowOk = false
                _state.value = _state.value.copy(error = null)
                // Stage 1: shallow first paint.
                if (!force) {
                    try {
                        when (val res = api.tree(depth = 1)) {
                            is TreeResult.Nodes -> {
                                if (gen != loadGen) return@withLock
                                _state.value = State(
                                    nodes = res.nodes,
                                    generation = res.generation,
                                    shallow = true,
                                    treeLoaded = false,
                                    hasSnapshot = _state.value.hasSnapshot,
                                )
                                shallowOk = true
                            }
                            TreeResult.NotModified -> { /* depth=1 never 304s */ }
                        }
                    } catch (e: Exception) {
                        // fall through to full
                    }
                }
                // Stage 2: full tree.
                try {
                    when (val res = api.tree()) {
                        is TreeResult.Nodes -> {
                            if (gen != loadGen) return@withLock
                            applyFull(res, gen)
                        }
                        TreeResult.NotModified -> {
                            if (gen != loadGen) return@withLock
                            _state.value = _state.value.copy(
                                shallow = false, treeLoaded = true)
                        }
                    }
                } catch (e: Exception) {
                    if (gen != loadGen) return@withLock
                    if (shallowOk) {
                        // Stay on the shallow grid — treeLoaded stays false.
                        return@withLock
                    }
                    if (_state.value.nodes.isNotEmpty()) {
                        // Stale tree still on screen — mark loaded and show it.
                        _state.value = _state.value.copy(
                            shallow = false, treeLoaded = true)
                    } else if (restoreSnapshot()) {
                        // snapshot path already emitted
                    } else {
                        _state.value = _state.value.copy(
                            error = e.message ?: "フォルダ一覧を読み込めませんでした")
                    }
                }
            }
        }
    }

    private fun applyFull(res: TreeResult.Nodes, gen: Int) {
        etag = res.etag ?: etag
        _state.value = State(
            nodes = res.nodes,
            generation = res.generation,
            shallow = false,
            treeLoaded = true,
            hasSnapshot = true,
        )
        // Persist in the background — never delay first paint.
        scope.launch {
            runCatching {
                snapshot.write(BookqJson.encodeToString(
                    TreeSnapshot(res.nodes, res.generation)))
            }
        }
    }

    /** Foreground-driven since-poll — call from lifecycle RESUMED/PAUSED. */
    fun setForeground(resumed: Boolean) {
        foreground = resumed
        if (resumed && pollJob == null) startPolling()
    }

    fun startPolling() {
        if (pollJob != null) return
        pollJob = scope.launch {
            while (true) {
                delay(pollIntervalMs)
                if (!foreground) continue
                val st = _state.value
                if (!st.treeLoaded || st.shallow) continue
                try {
                    when (val res = api.tree(since = st.generation)) {
                        is TreeResult.Nodes -> {
                            if (res.generation != _state.value.generation) {
                                applyFull(res, ++loadGen)
                            }
                        }
                        TreeResult.NotModified -> { /* unchanged */ }
                    }
                } catch (e: Exception) {
                    // Silent: next tick retries. Polling must never mask the UI.
                }
            }
        }
    }

    fun stopPolling() {
        pollJob?.cancel()
        pollJob = null
    }

    /** Pull-to-refresh: POST index/refresh, then reload unconditionally. */
    fun refresh() {
        scope.launch {
            try {
                api.refreshIndex()
            } catch (e: Exception) {
                _state.value = _state.value.copy(
                    error = e.message ?: "再読み込みに失敗しました")
                return@launch
            }
            load(force = true)
        }
    }

    @Serializable
    data class TreeSnapshot(val nodes: List<TreeNode>, val generation: Int)

    /** File-backed snapshot under `<dir>/tree-<host>.json` (iOS parity:
     *  keyed per server host so switching servers can't leak a tree). */
    class FileSnapshotIO(dir: File, host: String) : SnapshotIO {
        private val file = File(dir, ScopedFiles.fileName("tree", host))
        override fun read(): String? = ScopedFiles.readOrNull(file)
        override fun write(text: String) = ScopedFiles.writeAtomic(file, text)
        override fun exists(): Boolean = file.isFile
    }

    /** In-memory snapshot for tests. */
    class MemorySnapshotIO : SnapshotIO {
        var text: String? = null
        override fun read(): String? = text
        override fun write(text: String) { this.text = text }
        override fun exists(): Boolean = text != null
    }

    companion object {
        const val POLL_INTERVAL_MS = 15_000L
    }
}
