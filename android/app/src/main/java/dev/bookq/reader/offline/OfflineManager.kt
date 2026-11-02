package dev.bookq.reader.offline

import android.content.Context
import android.app.usage.StorageStatsManager
import android.os.storage.StorageManager
import androidx.work.Data
import androidx.work.ExistingWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkInfo
import androidx.work.WorkManager
import androidx.work.workDataOf
import dev.bookq.core.api.TreeNode
import dev.bookq.core.offline.OfflineRules
import dev.bookq.data.BookqApi
import dev.bookq.data.offline.OfflineStore
import dev.bookq.data.offline.WorkRecord
import java.io.File
import java.util.UUID
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/**
 * Per-scope offline orchestrator — owns the badge state machine, unique
 * WorkManager enqueueing, capacity preflight, oldest-work eviction, and
 * the local-first read path (`pageData` / `imageLister`).
 *
 * Transient badge flags (listing/downloading/stopping/deleting/confirm)
 * live here keyed by the TAPPED folder; records live in [OfflineStore].
 * WorkManager progress is folded into transient state by tag observation.
 */
class OfflineManager(
    private val context: Context,
    private val scope: CoroutineScope,
    private val api: BookqApi,
    val store: OfflineStore,
    val scopeKey: String,
    private val roots: () -> List<TreeNode>,
) {
    /** Per-tapped-folder transient flags — the web `_listingSet`,
     *  `_downloadingSet`, `_stoppingSet`, `_deletingSet`, `_confirmMap`
     *  folded into one record. */
    data class Transient(
        val listing: Boolean = false,
        val downloading: Boolean = false,
        val stopping: Boolean = false,
        val deleting: Boolean = false,
        val confirmUntil: Long = 0L,
        val progressDone: Int = 0,
        val progressTotal: Int = 0,
    )

    private val _transient = MutableStateFlow<Map<String, Transient>>(emptyMap())
    val transient: StateFlow<Map<String, Transient>> = _transient.asStateFlow()

    val records: StateFlow<Map<String, WorkRecord>> = store.records

    /** Mid-flight "nospace" work failures → UI opens the quota dialog and
     *  on confirm evicts the oldest work and re-enqueues once. */
    private val _quotaEvents = MutableSharedFlow<String>(extraBufferCapacity = 4)
    val quotaEvents: SharedFlow<String> = _quotaEvents

    /** Folders already given their one eviction retry this session. */
    private val evictedFor = mutableSetOf<String>()

    private val wm = WorkManager.getInstance(context)

    init {
        // Fold WorkManager state into the transient map — survives process
        // death, so a killed app still renders live progress on relaunch.
        scope.launch {
            wm.getWorkInfosByTagFlow(DownloadWorker.tag(scopeKey)).collect { infos ->
                val next = _transient.value.toMutableMap()
                for (info in infos) {
                    val folder = folderOf(info) ?: continue
                    when (info.state) {
                        WorkInfo.State.RUNNING, WorkInfo.State.ENQUEUED -> {
                            val t = next[folder] ?: Transient()
                            next[folder] = t.copy(
                                listing = info.progress.getString(DownloadWorker.P_PHASE) != "downloading",
                                downloading = true,
                                stopping = false,
                                progressDone = info.progress.getInt(DownloadWorker.P_DONE, 0),
                                progressTotal = info.progress.getInt(DownloadWorker.P_TOTAL, 0),
                            )
                        }
                        WorkInfo.State.FAILED -> {
                            if (info.outputData.getString(DownloadWorker.P_REASON) == "nospace") {
                                _quotaEvents.tryEmit(folder)
                            }
                            next.remove(folder)
                        }
                        else -> next.remove(folder) // SUCCEEDED / CANCELLED
                    }
                }
                _transient.value = next
            }
        }
    }

    private fun folderOf(info: WorkInfo): String? =
        info.outputData.getString(DownloadWorker.P_FOLDER)
            ?: info.progress.getString(DownloadWorker.P_TARGET)
            ?: info.tags.firstOrNull { it.startsWith(FOLDER_TAG) }
                ?.removePrefix(FOLDER_TAG)

    // ── state derivation (UI calls per tile) ──

    fun leavesOf(folder: String): List<String> = OfflineRules.leafPaths(folder, roots())

    fun recordOf(folder: String): WorkRecord? = store.get(folder)

    fun btnState(folder: String): OfflineRules.BtnState {
        val t = _transient.value[folder] ?: Transient()
        val recs = store.records.value
        return OfflineRules.btnState(
            folder = folder,
            leaves = leavesOf(folder),
            recordOf = { f -> recs[f]?.let { OfflineRules.LeafState(it.pageCount, it.cachedCount) } },
            confirmArmed = t.confirmUntil > System.currentTimeMillis(),
            deleting = t.deleting,
            stopping = t.stopping,
            downloading = t.downloading,
            listing = t.listing,
        )
    }

    /** Ring fill fraction while downloading, else partial-progress pct. */
    fun progressFor(folder: String): Float {
        val t = _transient.value[folder]
        if (t != null && t.downloading && t.progressTotal > 0) {
            return t.progressDone.toFloat() / t.progressTotal
        }
        val recs = store.records.value
        return OfflineRules.partialProgress(leavesOf(folder)) { f ->
            recs[f]?.let { OfflineRules.LeafState(it.pageCount, it.cachedCount) }
        }.pct
    }

    /** "残り N ページ" / "N ページ分" for the partial menu. */
    fun partialProgress(folder: String): OfflineRules.Progress {
        val recs = store.records.value
        return OfflineRules.partialProgress(leavesOf(folder)) { f ->
            recs[f]?.let { OfflineRules.LeafState(it.pageCount, it.cachedCount) }
        }
    }

    // ── actions ──

    sealed interface RequestResult {
        data object Enqueued : RequestResult
        /** Preflight says storage is tight — UI shows the quota dialog. */
        data class NeedsEviction(val victim: WorkRecord, val needed: Long) : RequestResult
        data object Rejected : RequestResult
    }

    /** Badge tap dispatch — mirrors the web delegated click handler. */
    sealed interface TapResult {
        data class Start(val node: TreeNode) : TapResult
        data object Stop : TapResult
        data class PartialMenu(val progress: OfflineRules.Progress) : TapResult
        data object ArmedConfirm : TapResult
        data class Delete(val node: TreeNode) : TapResult
        data object Ignore : TapResult
    }

    fun onBadgeTap(folder: String, node: TreeNode): TapResult = when (btnState(folder)) {
        OfflineRules.BtnState.IDLE -> TapResult.Start(node)
        OfflineRules.BtnState.DOWNLOADING -> { stop(folder); TapResult.Stop }
        OfflineRules.BtnState.PARTIAL -> TapResult.PartialMenu(partialProgress(folder))
        OfflineRules.BtnState.COMPLETE -> { armConfirm(folder); TapResult.ArmedConfirm }
        OfflineRules.BtnState.CONFIRM_DELETE -> TapResult.Delete(node)
        else -> TapResult.Ignore
    }

    /** Preflight capacity check, then enqueue. `needsBytes` is a rough
     *  page-count estimate — the real guard is the worker's mid-flight
     *  low-space check feeding [quotaEvents]. */
    fun requestDownload(folder: String, original: Boolean): RequestResult {
        val leaves = leavesOf(folder)
        val remaining = leaves.sumOf { leaf ->
            val rec = store.get(leaf)
            (rec?.pageCount ?: 0).coerceAtLeast(0)
        }
        val estNeed = if (remaining > 0) remaining * AVG_PAGE_BYTES else PREFLIGHT_MIN
        val free = allocatableBytes()
        if (free >= 0 && free < estNeed + DownloadWorker.MIN_HEADROOM) {
            val victim = store.oldest()
                ?: return RequestResult.Rejected
            return RequestResult.NeedsEviction(victim, estNeed)
        }
        enqueue(folder, leaves, original)
        return RequestResult.Enqueued
    }

    /** Quota-dialog confirm: evict the oldest work, retry the request —
     *  once per folder per session (`_confirmMap` 1-retry parity). */
    suspend fun evictAndRetry(folder: String, original: Boolean): RequestResult {
        if (!evictedFor.add(folder)) return RequestResult.Rejected
        store.oldest()?.let { store.deleteWork(it.folder) }
        return when (val r = requestDownload(folder, original)) {
            is RequestResult.NeedsEviction -> RequestResult.Rejected
            else -> r
        }
    }

    /** Rough allocatable estimate — `StorageManager.getAllocatableBytes`,
     *  -1 when the platform can't answer (treated as "enough"). */
    fun allocatableBytes(): Long {
        return try {
            val sm = context.getSystemService(StorageManager::class.java) ?: return -1L
            val uuid: UUID = sm.getUuidForPath(context.filesDir)
            sm.getAllocatableBytes(uuid)
        } catch (e: Exception) {
            -1L
        }
    }

    private fun enqueue(folder: String, leaves: List<String>, original: Boolean) {
        val name = DownloadWorker.uniqueName(scopeKey, folder)
        val req = OneTimeWorkRequestBuilder<DownloadWorker>()
            .setInputData(workDataOf(
                DownloadWorker.K_FOLDER to folder,
                DownloadWorker.K_LEAVES to leaves.toTypedArray(),
                DownloadWorker.K_ORIGINAL to original,
                DownloadWorker.K_SCOPE to scopeKey,
                DownloadWorker.K_WORK_NAME to name,
            ))
            .addTag(DownloadWorker.tag(scopeKey))
            .addTag(FOLDER_TAG + folder)
            .build()
        setTransient(folder) { it.copy(listing = true, downloading = true, stopping = false) }
        wm.enqueueUniqueWork(name, ExistingWorkPolicy.REPLACE, req)
    }

    fun stop(folder: String) {
        setTransient(folder) { it.copy(stopping = true) }
        wm.cancelUniqueWork(DownloadWorker.uniqueName(scopeKey, folder))
    }

    /** Continue a partial work — the worker skips files already on disk. */
    fun continueDownload(folder: String, node: TreeNode, original: Boolean): RequestResult =
        requestDownload(folder, original)

    suspend fun delete(folder: String) {
        setTransient(folder) { it.copy(deleting = true, confirmUntil = 0) }
        for (leaf in leavesOf(folder)) store.deleteWork(leaf)
        // A record can exist for the folder itself outside leaf enumeration.
        store.deleteWork(folder)
        _transient.value = _transient.value - folder
    }

    private fun armConfirm(folder: String) {
        val until = System.currentTimeMillis() + CONFIRM_MS
        setTransient(folder) { it.copy(confirmUntil = until) }
        scope.launch {
            delay(CONFIRM_MS + 50)
            _transient.value[folder]?.let { t ->
                if (t.confirmUntil <= System.currentTimeMillis()) {
                    setTransient(folder) { it.copy(confirmUntil = 0) }
                }
            }
        }
    }

    // ── read path (local-first) ──

    /** Coil `data`: the on-disk file when present, else the `/view/` (or
     *  `/image/`) URL — reader + overview get offline pages for free. */
    fun pageData(folder: String, name: String, original: Boolean): Any {
        val f = store.pageFile(folder, name)
        return if (f.isFile && f.length() > 0) f else api.pageUrl(folder, name, original).toString()
    }

    /** Overview thumbnails: the downloaded page file when present, else
     *  the small `/thumb/` URL (cheaper over the network). */
    fun thumbData(folder: String, name: String, w: Int): Any {
        val f = store.pageFile(folder, name)
        return if (f.isFile && f.length() > 0) f
            else api.thumbUrl(folder, name, w).toString()
    }

    /** `/api/v1/images` with the record-names ∩ files fallback — the
     *  offline reader path for works whose listings no longer resolve. */
    suspend fun imageLister(folder: String): List<String> = try {
        api.images(folder)
    } catch (e: Exception) {
        val cached = store.cachedNames(folder)
        if (cached.isNotEmpty()) cached else throw e
    }

    /** Reader opened `path` — bump lastReadAt on its leaf records so
     *  eviction treats recently-read works as newest. */
    fun touchRead(path: String) {
        val now = System.currentTimeMillis()
        for (rec in store.records.value.values) {
            if (rec.folder == path || rec.folder.startsWith("$path/")) {
                store.update(rec.folder) { it.copy(lastReadAt = now) }
            }
        }
    }

    // ── settings / maintenance ──

    fun stats(): Pair<Int, Long> = store.stats()

    fun evictablePreview(): WorkRecord? = store.oldest()

    suspend fun clearAll() {
        store.clear()
        _transient.value = emptyMap()
    }

    private fun setTransient(folder: String, transform: (Transient) -> Transient) {
        val cur = _transient.value[folder] ?: Transient()
        _transient.value = _transient.value + (folder to transform(cur))
    }

    companion object {
        const val CONFIRM_MS = 3_000L
        const val FOLDER_TAG = "bookq-dl-folder|"
        const val PREFLIGHT_MIN = 48L * 1024 * 1024
        const val AVG_PAGE_BYTES = 2L * 1024 * 1024
    }
}
