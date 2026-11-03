package dev.bookq.data.offline

import dev.bookq.core.api.BookqJson
import dev.bookq.core.offline.OfflineRules
import dev.bookq.data.ScopedFiles
import java.io.File
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.serialization.Serializable
import kotlinx.serialization.builtins.MapSerializer
import kotlinx.serialization.builtins.serializer

/**
 * Per-scope offline works store — the Android counterpart of the web
 * IndexedDB `works` store / iOS `works.json`. One JSON file per (host,
 * account) scope under `filesDir/state/`; page files live at
 * `filesDir/offline/<scope>/<enc(folder)>/<name>` (archive inner names may
 * nest — `epub/OEBPS/a.jpg` becomes real subdirs).
 */
@Serializable
data class WorkRecord(
    val folder: String,
    val pageCount: Int = 0,
    val cachedCount: Int = 0,
    val bytes: Long = 0,
    val names: List<String> = emptyList(),
    val thumb: String? = null,
    val addedAt: Long = 0,
    val lastReadAt: Long = 0,
) {
    val complete: Boolean get() = pageCount > 0 && cachedCount >= pageCount
}

class OfflineStore(
    filesDir: File,
    scopeKey: String,
) {
    private val scopeSegment = ScopedFiles.segment(scopeKey)
    private val workRoot = File(filesDir, "offline/$scopeSegment")
    private val recordsFile =
        File(File(filesDir, "state"), ScopedFiles.fileName("offline-works", scopeKey))

    private val mapSer = MapSerializer(String.serializer(), WorkRecord.serializer())

    private val _records = MutableStateFlow(load())
    /** folder → record; collectors derive badge states. */
    val records: StateFlow<Map<String, WorkRecord>> = _records.asStateFlow()

    private fun load(): Map<String, WorkRecord> {
        val text = ScopedFiles.readOrNull(recordsFile) ?: return emptyMap()
        return runCatching { BookqJson.decodeFromString(mapSer, text) }
            .getOrDefault(emptyMap())
    }

    @Synchronized
    private fun persist() {
        ScopedFiles.writeAtomic(recordsFile, BookqJson.encodeToString(mapSer, _records.value))
    }

    fun get(folder: String): WorkRecord? = _records.value[folder]

    /** Insert or merge a record; persists atomically. */
    @Synchronized
    fun put(rec: WorkRecord) {
        _records.value = _records.value + (rec.folder to rec)
        persist()
    }

    /** Mutate one record in place (no-op when absent). */
    @Synchronized
    fun update(folder: String, transform: (WorkRecord) -> WorkRecord) {
        val cur = _records.value[folder] ?: return
        _records.value = _records.value + (folder to transform(cur))
        persist()
    }

    /** Records are deleted from the map; files go away with the dir. */
    @Synchronized
    fun remove(folder: String) {
        if (folder !in _records.value) return
        _records.value = _records.value - folder
        persist()
    }

    fun workDir(folder: String): File = File(workRoot, OfflineRules.encFolder(folder))

    fun pageFile(folder: String, name: String): File = File(workDir(folder), name)

    /** Recorded names that physically exist — the offline reader fallback. */
    fun cachedNames(folder: String): List<String> {
        val rec = get(folder) ?: return emptyList()
        return rec.names.filter { pageFile(folder, it).isFile && pageFile(folder, it).length() > 0 }
    }

    /** Refresh a record's `cachedCount`/`bytes` from what is on disk —
     *  stop/partial paths call this so records never claim phantom pages. */
    @Synchronized
    fun reconcile(folder: String) {
        val rec = get(folder) ?: return
        var count = 0
        var bytes = 0L
        for (name in rec.names) {
            val f = pageFile(folder, name)
            if (f.isFile && f.length() > 0) { count++; bytes += f.length() }
        }
        update(folder) { it.copy(cachedCount = count, bytes = bytes) }
    }

    /** Record + files gone. Returns the bytes freed (for eviction stats). */
    fun deleteWork(folder: String): Long {
        val bytes = get(folder)?.bytes ?: 0
        workDir(folder).deleteRecursively()
        remove(folder)
        return bytes
    }

    /** All works + all files — the settings "全削除" path. */
    @Synchronized
    fun clear() {
        workRoot.deleteRecursively()
        _records.value = emptyMap()
        persist()
    }

    /** Works count = leaf-most records (a parent and its leaves count once);
     *  size = dedupedBytes parity with the web settings line. */
    fun stats(): Pair<Int, Long> {
        val all = _records.value
        val leaves = OfflineRules.leafMost(all.keys.toList())
        val bytes = OfflineRules.dedupedBytes(all.keys.toList()) { all[it]?.bytes ?: 0L }
        return leaves.size to bytes
    }

    /** Oldest work by `lastReadAt ?: addedAt` — the eviction victim. */
    fun oldest(): WorkRecord? {
        val all = _records.value
        if (all.isEmpty()) return null
        val order = OfflineRules.evictOrder(all.keys.toList()) {
            val r = all[it]!!
            if (r.lastReadAt > 0) r.lastReadAt else r.addedAt
        }
        return all[order.first()]
    }
}
