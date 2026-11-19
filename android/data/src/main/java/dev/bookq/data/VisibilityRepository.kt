package dev.bookq.data

import dev.bookq.core.api.BookqJson
import dev.bookq.core.visibility.VisibilityState
import java.io.File
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.async
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString

/**
 * Hidden-root snapshot repository — port of `browse/visibility.js` read
 * path (`refresh` / `_resolveGet`):
 *
 *  - GET succeeds → apply the server snapshot verbatim (owner may change →
 *    force; down-rev allowed for server responses) → READY, writable.
 *  - 401/403 ("authFail") → ERROR — never continue on another account's
 *    cached snapshot.
 *  - network/5xx with a same-owner cached snapshot → READONLY + cached
 *    snapshot applied (browse works, mutations locked).
 *  - network/5xx with no snapshot → ERROR + retry affordance — an empty
 *    hidden set must NOT be assumed (that would expose everything) and
 *    error is not denied-by-default either: guards queue.
 *
 * Single-flight like JS `_refreshFlight`: concurrent refresh() calls
 * queue onto the in-flight request and run again if they arrived
 * mid-flight (`_refreshQueued`).
 *
 * Compose exposure: [version] ticks on every state mutation; read the
 * predicates on [vis] after collecting it.
 */
class VisibilityRepository(
    private val api: BookqApi,
    scope: CoroutineScope,
    private val cache: CacheIO,
) {

    /** Core predicate machine — read after collecting [version]. */
    val vis = VisibilityState()

    private val _version = MutableStateFlow(0L)
    val version = _version.asStateFlow()

    private val mutex = Mutex()
    private var inFlight = false
    private var queued = false
    private val repoScope = scope

    /** Serializes every `vis` mutation — refresh() runs on the IO scope
     *  while setHidden() may be invoked from a Main-scoped cell. */
    private val writeMutex = Mutex()

    /** `_reqGen`/`_appliedGen` — a GET issued before a PUT whose response
     *  lands afterwards must not roll the applied snapshot back. */
    private val reqGen = AtomicInteger(0)
    private var appliedGen = 0

    // ── write-path state (web `_saving`/`_lastError`/`_coalesceTimer`) ──

    enum class LastError { FETCH, AUTH, SAVE, OWNER }
    /** `setHidden` rejection reasons — caller toasts accordingly. */
    enum class SaveError { INVALID, BUSY, OFFLINE, CONFLICT, SAVE, NET }

    /** One in-flight PUT (web `_saving` + `vis-busy` lock). */
    @Volatile var saving = false
        private set
    @Volatile var lastError: LastError? = null
        private set
    private var coalesceJob: Job? = null
    /** Guarded by [writeMutex]. */
    private var convergenceSent = false

    interface CacheIO {
        fun read(): String?
        fun write(text: String)
        fun clear()
    }

    init {
        // Same-owner cached snapshot applies immediately (marks read-only
        // intent until the server confirms) — JS init() lines 643-647.
        val cached = readCache()
        if (cached != null) {
            vis.applySnapshot(cached.owner, cached.revision, cached.hiddenRoots)
            bump()
        }
    }

    fun setShowAll(v: Boolean) {
        if (vis.showAll == v) return
        vis.setShowAll(v)
        bump()
        scheduleRefresh() // web: mode switch re-syncs against the server
    }

    /** `_scheduleRefresh` — coalesced re-refresh (400 ms): mode toggles,
     *  foreground regains, post-stale-write convergence. */
    fun scheduleRefresh() {
        coalesceJob?.cancel()
        coalesceJob = repoScope.launch {
            kotlinx.coroutines.delay(400)
            refresh().join()
        }
    }

    /** Error-state retry — status stays ERROR while in flight (web parity). */
    fun retry() {
        lastError = null
        bump()
        refresh()
    }

    fun refresh(): Job {
        return repoScope.launch {
            mutex.withLock {
                if (inFlight) {
                    queued = true
                    return@withLock
                }
                inFlight = true
            }
            try {
                do {
                    var again = false
                    if (!vis.hasSnapshot) {
                        writeMutex.withLock {
                            vis.setStatus(VisibilityState.Status.LOADING)
                            bump()
                        }
                    }
                    val cached = readCache()
                    val gen = reqGen.incrementAndGet()
                    val outcome = try {
                        Outcome.Snapshot(api.folderVisibility())
                    } catch (e: CancellationException) {
                        throw e
                    } catch (e: ApiException.Unauthorized) {
                        Outcome.AuthFail
                    } catch (e: Exception) {
                        Outcome.NetErr
                    }
                    writeMutex.withLock {
                        // Stale responses drop — a PUT (or a newer GET)
                        // already applied a newer snapshot.
                        if (gen > appliedGen) {
                            when (outcome) {
                                is Outcome.Snapshot -> {
                                    val j = outcome.snap
                                    val owner = j.owner.ifEmpty { vis.owner }
                                    if (owner == vis.owner && j.revision < vis.revision) {
                                        // Down-rev payload — converge once
                                        // (web `_resolveGet` → `_convergeOnce`).
                                        if (!convergenceSent) {
                                            convergenceSent = true
                                            scheduleRefresh()
                                        }
                                    } else {
                                        appliedGen = gen
                                        vis.setStatus(
                                            VisibilityState.Status.READY,
                                            readOnly = false)
                                        // The GET succeeded — clear before
                                        // apply so the new owner is never
                                        // observable with a stale error.
                                        lastError = null
                                        vis.applySnapshot(
                                            owner, j.revision, j.hiddenRoots,
                                            force = owner != vis.owner,
                                            allowDownrev = true,
                                        )
                                        writeCache(vis.snapshot())
                                        convergenceSent = false
                                    }
                                }
                                Outcome.AuthFail -> {
                                    vis.setStatus(VisibilityState.Status.ERROR)
                                }
                                Outcome.NetErr -> {
                                    // The cache file is already owner-scoped —
                                    // any hit is same-owner by construction
                                    // (JS `_readCache(_owner)` parity).
                                    if (cached != null) {
                                        vis.setStatus(
                                            VisibilityState.Status.READONLY,
                                            readOnly = true)
                                        vis.applySnapshot(
                                            cached.owner, cached.revision,
                                            cached.hiddenRoots)
                                    } else {
                                        vis.setStatus(VisibilityState.Status.ERROR)
                                    }
                                }
                            }
                        }
                        bump()
                    }
                    mutex.withLock {
                        if (queued) {
                            queued = false
                            again = true
                        }
                    }
                } while (again)
            } finally {
                mutex.withLock { inFlight = false }
            }
        }
    }

    // ── write path: one in-flight PUT, server snapshot commits verbatim ──

    /**
     * `setHidden` — iOS `VisibilityStore.setHidden` / web `setHidden`.
     * Throws [SaveException] with the [SaveError] kind so the caller can
     * toast the right failure. The response body IS the authoritative
     * post-write snapshot; a stale payload (another device's GET landed
     * mid-flight) is dropped in favour of exactly one convergence GET.
     */
    suspend fun setHidden(root: String, hidden: Boolean) {
        val target = vis.validRootPath(root) ?: throw SaveException(SaveError.INVALID)
        // Fast-path reject without touching the lock — a second call
        // while a PUT blocks must answer BUSY immediately.
        if (saving) throw SaveException(SaveError.BUSY)
        if (vis.readOnly) throw SaveException(SaveError.OFFLINE)
        // The write rides repoScope — the invoking cell's composable scope
        // may be disposed mid-PUT, and cancelling after the server applied
        // the write would strand local state (the PUT is not aborted by
        // dropping the await). runCatching keeps a SaveException INSIDE
        // the result — a failed deferred child would otherwise cancel
        // repoScope's Job and kill every queued refresh/write.
        repoScope.async {
            runCatching {
                writeMutex.withLock {
                    if (saving) throw SaveException(SaveError.BUSY)
                    if (vis.readOnly) throw SaveException(SaveError.OFFLINE)
                    saving = true
                    bump()
                    try {
                        val snap = api.setFolderVisibility(
                            root = target, hidden = hidden, owner = vis.owner)
                        vis.setStatus(VisibilityState.Status.READY, readOnly = false)
                        lastError = null
                        if (snap.owner != vis.owner || snap.revision >= vis.revision) {
                            vis.applySnapshot(
                                snap.owner.ifEmpty { vis.owner }, snap.revision,
                                snap.hiddenRoots,
                                force = snap.owner.isNotEmpty() && snap.owner != vis.owner,
                                allowDownrev = true,
                            )
                            writeCache(vis.snapshot())
                            // The PUT result is authoritative — every GET issued
                            // so far is now stale (web `_appliedGen = _reqGen`).
                            appliedGen = reqGen.get()
                            convergenceSent = false
                        } else if (!convergenceSent) {
                            convergenceSent = true
                            scheduleRefresh()
                        }
                        bump()
                    } catch (e: CancellationException) {
                        throw e
                    } catch (e: ApiException.Unauthorized) {
                        vis.setStatus(VisibilityState.Status.ERROR)
                        lastError = LastError.AUTH
                        bump()
                        throw SaveException(SaveError.SAVE)
                    } catch (e: ApiException.Status) {
                        if (e.code == 409) {
                            // Booted owner is stale — pull the server's
                            // authoritative owner/snapshot instead of writing
                            // to another account.
                            vis.setStatus(VisibilityState.Status.ERROR)
                            lastError = LastError.OWNER
                            refresh()
                            bump()
                            throw SaveException(SaveError.CONFLICT)
                        }
                        lastError = LastError.SAVE
                        bump()
                        throw SaveException(SaveError.SAVE)
                    } catch (e: Exception) {
                        lastError = LastError.SAVE
                        bump()
                        throw SaveException(SaveError.NET)
                    } finally {
                        saving = false
                        bump()
                    }
                }
            }
        }.await().getOrThrow()
    }

    /** `statusText` — web `_updateStatusDom` labels (null = hidden row). */
    fun statusText(): String? = when {
        saving -> "保存中…"
        vis.status == VisibilityState.Status.LOADING -> "読込中…"
        vis.readOnly -> "オフライン（変更不可）"
        vis.status == VisibilityState.Status.ERROR -> "取得失敗"
        lastError == LastError.SAVE -> "保存失敗"
        else -> null
    }

    class SaveException(val error: SaveError) : Exception()

    private fun bump() {
        _version.update { it + 1 }
    }

    private fun readCache(): CacheSnapshot? {
        val text = cache.read() ?: return null
        val snap = runCatching {
            BookqJson.decodeFromString<CacheSnapshot>(text)
        }.getOrNull() ?: return null
        if (snap.v != 1) return null
        return snap
    }

    private fun writeCache(snap: VisibilityState.Snapshot) {
        runCatching {
            cache.write(BookqJson.encodeToString(
                CacheSnapshot(1, snap.owner, snap.revision, snap.hiddenRoots)))
        }
    }

    private sealed interface Outcome {
        data class Snapshot(val snap: dev.bookq.core.api.VisibilitySnapshot) : Outcome
        data object AuthFail : Outcome
        data object NetErr : Outcome
    }

    @Serializable
    data class CacheSnapshot(
        val v: Int,
        val owner: String,
        val revision: Int,
        val hiddenRoots: List<String>,
    )

    /** Owner-scoped JSON file: `<dir>/visibility-<host|email>.json` —
     *  JS localStorage `bookq_visibility_<owner>` parity. */
    class FileCacheIO(dir: File, host: String, owner: String) : CacheIO {
        private val file = File(dir, ScopedFiles.fileName(
            "visibility", ScopedFiles.key(host, owner)))
        override fun read(): String? = ScopedFiles.readOrNull(file)
        override fun write(text: String) = ScopedFiles.writeAtomic(file, text)
        override fun clear() { file.delete() }
    }

    class MemoryCacheIO : CacheIO {
        var text: String? = null
        override fun read(): String? = text
        override fun write(text: String) { this.text = text }
        override fun clear() { text = null }
    }
}
