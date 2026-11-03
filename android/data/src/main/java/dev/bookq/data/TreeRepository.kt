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
    /** Top-level-only sibling of [snapshot] — kilobytes, decodes
     *  instantly, so cold start can paint the root grid while the
     *  multi-MB full snapshot is still parsing. */
    private val shallow: SnapshotIO? = null,
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
        /** Backing file name — lets a boot-time prewarm prove the decoded
         *  candidate really came from this repo's file (never apply a
         *  different host's snapshot). Null for in-memory stores. */
        val backingName: String? get() = null
    }

    /** A snapshot decoded ahead of scope construction (boot prewarm). */
    class PrewarmedSnapshot(val name: String, val snap: TreeSnapshot)

    private val _state = MutableStateFlow(State())
    val state = _state.asStateFlow()

    @Volatile private var loadGen = 0
    @Volatile private var fullRetry = 0
    private val loadMutex = Mutex()
    private var pollJob: Job? = null
    @Volatile private var foreground = true

    /** Instant-paint phase — restore the top-level snapshot (KBs) so
     *  the grid can draw while the full restore is still decoding.
     *  Mirrors the network `depth=1` stage: `shallow=true` until the
     *  full tree arrives. Returns true when applied. */
    fun restoreShallow(): Boolean {
        val io = shallow ?: return false
        val text = io.read() ?: return false
        val snap = runCatching {
            BookqJson.decodeFromString<TreeSnapshot>(text)
        }.getOrNull() ?: return false
        _state.value = State(
            nodes = snap.nodes,
            generation = snap.generation,
            shallow = true,
            treeLoaded = false,
            hasSnapshot = true,
        )
        return true
    }

    /** Restore the host snapshot synchronously so cold start paints
     *  instantly (SW NetworkFirst parity). A [prewarmed] candidate is
     *  used only when it came from this repo's backing file — the boot
     *  prewarm races settings load, before the scope's host is known.
     *  Returns true when applied. */
    fun restoreSnapshot(prewarmed: PrewarmedSnapshot? = null): Boolean {
        val snap = if (prewarmed != null && prewarmed.name == snapshot.backingName) {
            prewarmed.snap
        } else {
            val text = snapshot.read() ?: return false
            runCatching {
                BookqJson.decodeFromString<TreeSnapshot>(text)
            }.getOrNull() ?: return false
        }
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
        if (!force) fullRetry = 0
        return scope.launch {
            loadMutex.withLock {
                var shallowOk = false
                _state.value = _state.value.copy(error = null)
                // Stage 1: shallow first paint — skipped when a complete
                // tree already backs the screen (restored snapshot or an
                // earlier full load): swapping it for a shallow window
                // would LOSE data when stage 2 then fails.
                if (!force && !_state.value.hasSnapshot) {
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
                        // Stay shallow — but web's _scheduleFullRetry:
                        // a transient 202/5xx must not wedge the grid.
                        scheduleFullRetry(gen)
                        return@withLock
                    }
                    val st = _state.value
                    if (st.nodes.isNotEmpty() && st.hasSnapshot &&
                        !st.shallow) {
                        // Stale FULL tree still on screen — mark loaded
                        // and show it. A depth-1-only list is NOT
                        // complete: keep the retry loop alive instead
                        // of freezing the shallow window as "loaded".
                        _state.value = st.copy(
                            shallow = false, treeLoaded = true)
                    } else if (st.shallow && restoreSnapshot()) {
                        // Shallow snapshot on screen + full fetch down:
                        // children=null tiles can never drill. The file
                        // snapshot is a COMPLETE stale tree — swap to
                        // it, then keep retrying for a fresh pull.
                        scheduleFullRetry(gen)
                    } else if (st.nodes.isNotEmpty()) {
                        scheduleFullRetry(gen)
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

    /** Full-tree retry while the shallow window stays up (app.js
     *  `_scheduleFullRetry`). Bounded — pull-to-refresh always works. */
    private fun scheduleFullRetry(gen: Int) {
        if (fullRetry >= MAX_FULL_RETRIES) return
        fullRetry++
        scope.launch {
            delay(FULL_RETRY_MS)
            if (loadGen == gen) load(force = true)
        }
    }

    private fun applyFull(res: TreeResult.Nodes, gen: Int) {
        fullRetry = 0
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
                // Sibling shallow snapshot — `depth=1` semantics, the
                // next cold start's instant paint.
                shallow?.write(BookqJson.encodeToString(
                    TreeSnapshot(
                        res.nodes.map { it.copy(children = null) },
                        res.generation)))
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

    /** Pull-to-refresh: POST index/refresh, then reload unconditionally.
     *  Returns the Job so the caller can clear its spinner in a finally —
     *  completes on BOTH success and failure (error lands in state). */
    fun refresh(): Job {
        return scope.launch {
            try {
                api.refreshIndex()
            } catch (e: Exception) {
                _state.value = _state.value.copy(
                    error = e.message ?: "再読み込みに失敗しました")
                return@launch
            }
            load(force = true).join()
        }
    }

    @Serializable
    data class TreeSnapshot(val nodes: List<TreeNode>, val generation: Int)

    /** File-backed snapshot under `<dir>/tree-<host>.json` (iOS parity:
     *  keyed per server host so switching servers can't leak a tree). */
    class FileSnapshotIO(dir: File, host: String, prefix: String = "tree") : SnapshotIO {
        private val file = File(dir, ScopedFiles.fileName(prefix, host))
        override val backingName: String get() = file.name
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
        private const val FULL_RETRY_MS = 5_000L
        private const val MAX_FULL_RETRIES = 8
    }
}
