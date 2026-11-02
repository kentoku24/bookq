package dev.bookq.data

import dev.bookq.core.api.BookqJson
import dev.bookq.core.api.ReadingPosition
import dev.bookq.core.progress.ProgressBook
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

    // ── sync.js transport state ──
    private var pending: ReadingPosition? = null
    private var inFlight = false
    private var lastSentKey: String? = null
    private var retryDelayMs = 0L
    private var sendJob: kotlinx.coroutines.Job? = null

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
        scope.launch {
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
        if (remote != null) {
            if (book.mergeRemote(remote)) {
                persistNow()
                bump()
                return true
            }
        }
        // Local strictly newer than the remote record (or server empty) —
        // push it up so the server catches up (one PUT per boot at most).
        val localNewest = book.resumeCandidate() ?: return false
        if (remote == null || localNewest.ts > remote.ts) {
            try {
                api.putReadingPosition(localNewest)
                lastSentKey = key(localNewest)
            } catch (e: Exception) { /* stays queued for the next save */ }
        }
        return false
    }

    // ── push machinery (sync.js port) ──

    private fun key(p: ReadingPosition) = "${p.folder}|${p.page}|${p.total}"

    private fun droppable(code: Int) = code in 400..499 && code != 408 && code != 429

    /** Trailing-edge queue — never blocks the caller's page turn. */
    fun push(pos: ReadingPosition) {
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
        armSend(pushDebounceMs)
    }

    /** Send the queued record NOW (ON_STOP flush — timers die with pause). */
    fun flush(): Job {
        sendJob?.cancel()
        val j = scope.launch { sendNow() }
        sendJob = j
        return j
    }

    private fun armSend(delayMs: Long) {
        sendJob?.cancel()
        sendJob = scope.launch {
            delay(delayMs)
            sendNow()
        }
    }

    private suspend fun sendNow() {
        val rec = pending ?: return
        if (inFlight) return
        if (key(rec) == lastSentKey) {
            pending = null
            return
        }
        pending = null
        inFlight = true
        val outcome = try {
            api.putReadingPosition(rec)
            lastSentKey = key(rec)
            Outcome.Sent
        } catch (e: ApiException) {
            val code = (e as? ApiException.Status)?.code
                ?: (e as? ApiException.Unauthorized)?.status
            if (code != null && droppable(code)) Outcome.Dropped else Outcome.Retry
        } catch (e: Exception) {
            Outcome.Retry
        }
        inFlight = false
        when (outcome) {
            Outcome.Sent, Outcome.Dropped -> {
                retryDelayMs = 0
                if (pending != null) armSend(0)
            }
            Outcome.Retry -> {
                if (pending == null) pending = rec
                retryDelayMs = minOf((if (retryDelayMs == 0L) pushDebounceMs else retryDelayMs) * 2, RETRY_MAX_MS)
                armSend(retryDelayMs)
            }
        }
    }

    private sealed interface Outcome { object Sent : Outcome; object Dropped : Outcome; object Retry : Outcome }

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
