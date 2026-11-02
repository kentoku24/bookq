package dev.bookq.data

import dev.bookq.core.api.BookqJson
import dev.bookq.core.visibility.VisibilityState
import java.io.File
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
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
        vis.setShowAll(v)
        bump()
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
                        vis.setStatus(VisibilityState.Status.LOADING)
                        bump()
                    }
                    val cached = readCache()
                    val outcome = try {
                        Outcome.Snapshot(api.folderVisibility())
                    } catch (e: ApiException.Unauthorized) {
                        Outcome.AuthFail
                    } catch (e: Exception) {
                        Outcome.NetErr
                    }
                    when (outcome) {
                        is Outcome.Snapshot -> {
                            val j = outcome.snap
                            val owner = j.owner.ifEmpty { vis.owner }
                            vis.setStatus(VisibilityState.Status.READY, readOnly = false)
                            vis.applySnapshot(
                                owner, j.revision, j.hiddenRoots,
                                force = owner != vis.owner,
                                allowDownrev = true,
                            )
                            writeCache(vis.snapshot())
                        }
                        Outcome.AuthFail -> {
                            vis.setStatus(VisibilityState.Status.ERROR)
                        }
                        Outcome.NetErr -> {
                            // The cache file is already owner-scoped — any
                            // hit is same-owner by construction (JS reads
                            // `_readCache(_owner)` the same way).
                            if (cached != null) {
                                vis.setStatus(VisibilityState.Status.READONLY, readOnly = true)
                                vis.applySnapshot(cached.owner, cached.revision, cached.hiddenRoots)
                            } else {
                                vis.setStatus(VisibilityState.Status.ERROR)
                            }
                        }
                    }
                    bump()
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

    private fun bump() {
        _version.value++
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
