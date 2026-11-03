package dev.bookq.core.progress

import dev.bookq.core.api.ReadingPosition

/**
 * Port of app.js `ReadingProgress` (localStorage map) — the local progress
 * book every browse render and the resume pill read from.
 *
 * Wire/storage shape: `folder -> {page,total,ts}`; capped at 50 entries
 * newest-first by `ts` (JS `_store` eviction). `mergeRemote` lands a
 * server-pulled record verbatim (its remote `ts`, never re-pushed).
 *
 * Thread safety: the repository writes on IO while Compose reads on
 * Main — every public member serializes on [lock]. All work inside the
 * lock is in-memory O(n) so contention stays negligible.
 */
class ProgressBook {

    private val lock = Any()
    private val entries = linkedMapOf<String, ReadingPosition>()

    /** All positions (folder -> position). Snapshot copy. */
    fun all(): Map<String, ReadingPosition> = synchronized(lock) { entries.toMap() }

    fun folders(): Set<String> = synchronized(lock) { entries.keys.toSet() }

    operator fun get(folder: String): ReadingPosition? = synchronized(lock) { entries[folder] }

    fun put(pos: ReadingPosition) {
        synchronized(lock) {
            entries[pos.folder] = pos
            evict()
        }
    }

    /** LWW merge: adopt `remote` only when strictly newer than the local
     *  entry (a server record must never roll a fresher local one back). */
    fun mergeRemote(remote: ReadingPosition): Boolean = synchronized(lock) {
        val local = entries[remote.folder]
        if (local != null && remote.ts <= local.ts) return@synchronized false
        entries[remote.folder] = remote
        evict()
        return@synchronized true
    }

    fun remove(folder: String) {
        synchronized(lock) { entries.remove(folder) }
    }

    fun clear() = synchronized(lock) { entries.clear() }

    /** JS `_store` cap: keep the 50 newest by ts. Call under [lock]. */
    private fun evict() {
        if (entries.size <= MAX_ENTRIES) return
        val sorted = entries.entries.sortedByDescending { it.value.ts }
        entries.clear()
        for (e in sorted.take(MAX_ENTRIES)) entries[e.key] = e.value
    }

    /**
     * The resume-pill candidate: newest entry by `ts` whose folder passes
     * `visible` (web filters hidden-root history out of candidates in
     * normal mode but never deletes the records). `visible` defaults to
     * allow-all for callers without a visibility snapshot.
     */
    fun resumeCandidate(visible: (String) -> Boolean = { true }): ReadingPosition? =
        synchronized(lock) {
            entries.entries.filter { visible(it.key) }.maxByOrNull { it.value.ts }?.value
        }

    companion object {
        const val MAX_ENTRIES = 50

        /** Pill label — app.js `📖 続きから: <leaf> p.<page+1>` where leaf is
         *  the last path segment (flattened names keep their last segment). */
        fun resumeLabel(pos: ReadingPosition): String {
            val leaf = pos.folder.substringAfterLast('/')
            return "📖 続きから: $leaf p.${pos.page + 1}"
        }
    }
}
