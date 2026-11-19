package dev.bookq.data

import dev.bookq.core.api.BookqJson
import dev.bookq.core.api.ReadingPosition
import dev.bookq.core.progress.ProgressBook
import java.io.File
import kotlinx.coroutines.CancellationException
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
 * Reading-position store — app.js `ReadingProgress` + `progress/sync.js`
 * boot merge, scope-persisted as web-localStorage `bookq_progress` shape
 * (`{<folder>: {page,total,ts}}`).
 *
 * Boot merge (app.js `_syncRemoteProgress`): pull the server record →
 * remote newer → mergeRemote (never re-pushed); local newest strictly
 * newer → push it up so the server catches up. `pull` returning an error
 * must NOT be treated as "empty" — pushing a stale local newest would
 * clobber a newer remote record.
 *
 * Save path (M3 reader uses it): [save] debounces local writes 500 ms;
 * the server mirror is pushed on a trailing 2 s debounce with at most one
 * in-flight PUT and exponential retry (sync.js contract — implemented
 * here so the reader only calls `save()`).
 */
class ProgressRepository(
    private val api: BookqApi,
    private val scope: CoroutineScope,
    private val store: StoreIO,
    private val clock: () -> Long = { System.currentTimeMillis() },
    /** Test seams — production keeps the sync.js constants. */
    private val saveDebounceMs: Long = SAVE_DEBOUNCE_MS,
    private val pushDebounceMs: Long = DEBOUNCE_MS,
) {

    val book = ProgressBook()

    /** Compose exposure — ticks on every local/remote write. */
    private val _version = MutableStateFlow(0L)
    val version = _version.asStateFlow()

    private val persistMutex = Mutex()

    // ── sync.js transport state — every field mutates under sendLock.
    //    The critical sections never suspend (queue ops + Job handles
    //    only), so a monitor is safer than a Mutex: it works from non-
    //    suspend callers (save→push→flush ordering) and cannot be lost
    //    to coroutine cancellation. @Volatile for the lock-free reads. ──
    private val sendLock = Any()
    @Volatile private var pending: ReadingPosition? = null
    @Volatile private var inFlight = false
    @Volatile private var lastSentKey: String? = null
    @Volatile private var retryDelayMs = 0L
    @Volatile private var sendJob: Job? = null
    @Volatile private var persistJob: Job? = null

    interface StoreIO {
        fun read(): String?
        fun write(text: String)
        fun clear()
    }

    init {
        readStore()?.forEach { (folder, pos) ->
            book.put(ReadingPosition(folder, pos.page, pos.total, pos.ts))
        }
    }

    // ── local writes ──

    /**
     * Record a page position (JS `saveDebounced` + `save`): same-position
     * saves are no-ops; the local write debounces 500 ms; the server push
     * rides the trailing 2 s debounce.
     */
    fun save(folder: String, page: Int, total: Int) {
        val existing = book[folder]
        if (existing != null && existing.page == page && existing.total == total) return
        val pos = ReadingPosition(folder, page, total, clock())
        book.put(pos)
        bump()
        // Trailing debounce — rapid page turns coalesce into one write.
        persistJob?.cancel()
        persistJob = scope.launch {
            delay(saveDebounceMs)
            persistNow()
        }
        push(pos)
    }

    private suspend fun persistNow() {
        persistMutex.withLock {
            runCatching {
                store.write(BookqJson.encodeToString(toStoreMap()))
            }
        }
    }

    fun flushPersist() {
        scope.launch { persistNow() }
    }

    private fun toStoreMap(): Map<String, PosFields> =
        book.all().mapValues { (_, p) -> PosFields(p.page, p.total, p.ts) }

    // ── server sync ──

    /**
     * Boot pull + merge. Returns whether the local book changed. An
     * unreachable server leaves the book untouched (never treated as
     * "empty") and skips the catch-up push.
     */
    suspend fun pull(): Boolean {
        val remote: ReadingPosition? = try {
            api.readingPositions().firstOrNull()
        } catch (e: Exception) {
            return false // ok:false — do not push a stale local newest
        }
        // Newest local BEFORE merging — web pushes it whether or not the
        // merge applied (a remote record in another folder must not
        // swallow the local newest's catch-up PUT).
        val localNewest = book.resumeCandidate()
        val merged = remote != null && book.mergeRemote(remote)
        if (merged) {
            persistNow()
            bump()
        }
        // Local strictly newer than the remote record (or server empty) —
        // push it up so the server catches up (one PUT per boot at most).
        if (localNewest != null && (remote == null || localNewest.ts > remote.ts)) {
            try {
                api.putReadingPosition(localNewest)
                synchronized(sendLock) { lastSentKey = key(localNewest) }
            } catch (e: Exception) { /* stays queued for the next save */ }
        }
        return merged
    }

    // ── push machinery (sync.js port) ──

    private fun key(p: ReadingPosition) = "${p.folder}|${p.page}|${p.total}"

    private fun droppable(code: Int) = code in 400..499 && code != 408 && code != 429

    /** Trailing-edge queue — never blocks the caller's page turn. The
     *  queue mutation is synchronous so `save(); flush()` ordering holds. */
    fun push(pos: ReadingPosition) {
        synchronized(sendLock) { pushLocked(pos) }
    }

    /** Call under sendLock. */
    private fun pushLocked(pos: ReadingPosition) {
        val cur = pending
        if (cur != null && key(cur) == key(pos)) {
            // Same position re-queued: keep the EARLIER ts (sync.js).
            if (pos.ts < cur.ts) pending = cur.copy(ts = pos.ts)
        } else {
            pending = pos
        }
        // During retry backoff a send is already armed — re-arming to 2 s
        // would defeat the backoff while reading through an outage.
        if (retryDelayMs > 0 && (sendJob != null || inFlight)) return
        armSendLocked(pushDebounceMs)
    }

    /**
     * Send the queued record NOW (ON_STOP flush — timers die with pause).
     * An in-flight PUT is never cancelled (sync.js): cancelling it would
     * be swallowed as Retry and stretch the backoff; the flight settles
     * and re-arms whatever is queued by then.
     */
    fun flush(): Job {
        synchronized(sendLock) { if (!inFlight) sendJob?.cancel() }
        return scope.launch { sendNow() }
    }

    /** Call under sendLock. */
    private fun armSendLocked(delayMs: Long) {
        if (!inFlight) sendJob?.cancel()
        sendJob = scope.launch {
            delay(delayMs)
            sendNow()
        }
    }

    private suspend fun sendNow() {
        val rec = synchronized(sendLock) {
            val p = pending ?: return@synchronized null
            if (inFlight) return@synchronized null
            if (key(p) == lastSentKey) {
                pending = null
                return@synchronized null
            }
            pending = null
            inFlight = true
            p
        } ?: return
        var outcome: Outcome = Outcome.Sent
        try {
            api.putReadingPosition(rec)
        } catch (e: CancellationException) {
            // Cancelled mid-PUT is NOT a send failure — requeue the
            // record without growing the backoff, then propagate.
            outcome = Outcome.Cancelled
            throw e
        } catch (e: ApiException) {
            val code = (e as? ApiException.Status)?.code
                ?: (e as? ApiException.Unauthorized)?.status
            outcome = if (code != null && droppable(code)) Outcome.Dropped else Outcome.Retry
        } catch (e: Exception) {
            outcome = Outcome.Retry
        } finally {
            // Even a mid-flight cancel must settle — a latched inFlight
            // would wedge every later send. synchronized never suspends,
            // so this runs inside a cancelled coroutine too.
            synchronized(sendLock) { settleLocked(outcome, rec) }
        }
    }

    /** Call under sendLock. */
    private fun settleLocked(outcome: Outcome, rec: ReadingPosition) {
        inFlight = false
        when (outcome) {
            Outcome.Sent -> {
                lastSentKey = key(rec)
                retryDelayMs = 0
                if (pending != null) armSendLocked(0)
            }
            Outcome.Dropped -> {
                retryDelayMs = 0
                if (pending != null) armSendLocked(0)
            }
            Outcome.Cancelled -> {
                if (pending == null) pending = rec
            }
            Outcome.Retry -> {
                if (pending == null) pending = rec
                retryDelayMs = minOf(
                    (if (retryDelayMs == 0L) pushDebounceMs else retryDelayMs) * 2,
                    RETRY_MAX_MS)
                armSendLocked(retryDelayMs)
            }
        }
    }

    private sealed interface Outcome {
        data object Sent : Outcome
        data object Dropped : Outcome
        data object Retry : Outcome
        data object Cancelled : Outcome
    }

    private fun bump() {
        _version.value++
    }

    private fun readStore(): Map<String, PosFields>? {
        val text = store.read() ?: return null
        return runCatching {
            BookqJson.decodeFromString<Map<String, PosFields>>(text)
        }.getOrNull()
    }

    @Serializable
    data class PosFields(val page: Int, val total: Int, val ts: Long)

    /** `{<folder>:{page,total,ts}}` file — web `bookq_progress` shape. */
    class FileStoreIO(dir: File, scopeKey: String) : StoreIO {
        private val file = File(dir, ScopedFiles.fileName("progress", scopeKey))
        override fun read(): String? = ScopedFiles.readOrNull(file)
        override fun write(text: String) = ScopedFiles.writeAtomic(file, text)
        override fun clear() { file.delete() }
    }

    class MemoryStoreIO : StoreIO {
        var text: String? = null
        override fun read(): String? = text
        override fun write(text: String) { this.text = text }
        override fun clear() { text = null }
    }

    companion object {
        const val SAVE_DEBOUNCE_MS = 500L
        const val DEBOUNCE_MS = 2_000L
        const val RETRY_MAX_MS = 60_000L
    }
}
