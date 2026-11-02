package dev.bookq.reader.offline

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.work.CoroutineWorker
import androidx.work.Data
import androidx.work.ForegroundInfo
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import dev.bookq.data.offline.WorkRecord
import dev.bookq.reader.BookqApp
import dev.bookq.reader.R
import java.io.File
import java.io.IOException
import okhttp3.Request
import okhttp3.coroutines.executeAsync

/**
 * Background page fetcher — one unique work per (scope, tapped folder).
 * Lists each leaf (`/api/v1/images`), then GETs `/view/` (or `/image/` for
 * the original-images setting) sequentially into the per-work dir. Stop
 * keeps what landed (→ PARTIAL); records are reconciled from disk so a
 * killed process never claims phantom pages.
 *
 * Progress keys: [P_PHASE] "listing"|"downloading", [P_DONE], [P_TOTAL];
 * the UI maps these onto the badge spinner/ring.
 */
class DownloadWorker(
    appContext: Context,
    params: WorkerParameters,
) : CoroutineWorker(appContext, params) {

    override suspend fun doWork(): Result {
        val folder = inputData.getString(K_FOLDER) ?: return Result.failure()
        val leaves = inputData.getStringArray(K_LEAVES)?.toList().orEmpty()
            .ifEmpty { listOf(folder) }
        val original = inputData.getBoolean(K_ORIGINAL, false)

        val app = applicationContext as BookqApp
        val scopeKey = inputData.getString(K_SCOPE) ?: ""
        val scope = app.graph.serverScope.value
        // Scope must match the enqueuing scope — a different server/account
        // must never inherit this work's auth or files.
        if (scope == null || scope.offline.scopeKey != scopeKey) {
            return if (runAttemptCount < 3) Result.retry() else Result.failure()
        }
        val store = scope.offline.store
        val api = scope.api
        val http = scope.mediaClient

        ensureChannel()
        setForeground(foreground(0, 0))

        var totalPages = 0
        var donePages = 0
        var totalEstimate = 0

        try {
            // Listing phase — leaf listings can be slow on big trees.
            val plans = mutableListOf<Pair<String, List<String>>>()
            for (leaf in leaves) {
                if (isStopped) return stopped(store, leaves)
                val names = api.images(leaf)
                plans += leaf to names
                totalEstimate += names.size
            }
            totalPages = totalEstimate

            for ((leaf, names) in plans) {
                if (isStopped) return stopped(store, leaves)
                val existing = store.get(leaf)
                store.put(WorkRecord(
                    folder = leaf,
                    pageCount = names.size,
                    cachedCount = existing?.cachedCount ?: 0,
                    bytes = existing?.bytes ?: 0,
                    names = names,
                    thumb = names.firstOrNull(),
                    addedAt = if (existing != null) existing.addedAt else System.currentTimeMillis(),
                    lastReadAt = existing?.lastReadAt ?: 0,
                ))

                var cached = store.cachedNames(leaf).size
                var bytes = 0L
                store.get(leaf)?.let { bytes = it.bytes }
                for (name in names) {
                    if (isStopped) return stopped(store, leaves)
                    val out = store.pageFile(leaf, name)
                    if (out.isFile && out.length() > 0) { donePages++; continue }
                    if (isLowOnSpace()) return noSpace(store, leaves)
                    try {
                        fetch(http, api.pageUrl(leaf, name, original).toString(), out)
                    } catch (e: IOException) {
                        if (isStopped) return stopped(store, leaves)
                        if (isLowOnSpace()) return noSpace(store, leaves)
                        throw e
                    }
                    cached++
                    donePages++
                    bytes += out.length()
                    if (donePages % 4 == 0 || donePages == totalPages) {
                        store.put(WorkRecord(
                            folder = leaf, pageCount = names.size,
                            cachedCount = cached, bytes = bytes, names = names,
                            thumb = names.firstOrNull(),
                            addedAt = store.get(leaf)?.addedAt ?: System.currentTimeMillis(),
                            lastReadAt = store.get(leaf)?.lastReadAt ?: 0,
                        ))
                        progress("downloading", donePages, totalPages)
                    }
                }
                store.put(WorkRecord(
                    folder = leaf, pageCount = names.size, cachedCount = cached,
                    bytes = bytes, names = names, thumb = names.firstOrNull(),
                    addedAt = store.get(leaf)?.addedAt ?: System.currentTimeMillis(),
                    lastReadAt = store.get(leaf)?.lastReadAt ?: 0,
                ))
                store.reconcile(leaf)
            }
        } catch (e: Exception) {
            for (leaf in leaves) runCatching { store.reconcile(leaf) }
            if (isStopped) return Result.success()
            return if (runAttemptCount < 2) Result.retry() else Result.failure(
                workDataOf(P_REASON to (e.message ?: "error"), P_FOLDER to folder))
        }
        return Result.success(workDataOf(P_FOLDER to folder))
    }

    /** Stop pressed / work cancelled — keep what landed, reconcile records. */
    private fun stopped(store: dev.bookq.data.offline.OfflineStore, leaves: List<String>): Result {
        for (leaf in leaves) runCatching { store.reconcile(leaf) }
        return Result.success()
    }

    private fun noSpace(store: dev.bookq.data.offline.OfflineStore, leaves: List<String>): Result {
        for (leaf in leaves) runCatching { store.reconcile(leaf) }
        return Result.failure(workDataOf(P_REASON to "nospace"))
    }

    private fun isLowOnSpace(): Boolean {
        val dir = applicationContext.filesDir
        val free = android.os.StatFs(dir.path).availableBytes
        return free < MIN_HEADROOM
    }

    private suspend fun fetch(http: okhttp3.OkHttpClient, url: String, out: File) {
        out.parentFile?.mkdirs()
        val tmp = File(out.parentFile, out.name + ".part")
        try {
            http.newCall(Request.Builder().url(url).build()).executeAsync().use { resp ->
                if (!resp.isSuccessful) throw IOException("HTTP ${resp.code}")
                resp.body.byteStream().use { src ->
                    tmp.outputStream().buffered().use { dst -> src.copyTo(dst) }
                }
            }
            if (!tmp.renameTo(out)) { tmp.copyTo(out, overwrite = true); tmp.delete() }
        } finally {
            if (tmp.exists()) tmp.delete()
        }
    }

    private suspend fun progress(phase: String, done: Int, total: Int) {
        setProgress(workDataOf(P_PHASE to phase, P_DONE to done, P_TOTAL to total,
            P_TARGET to inputData.getString(K_FOLDER)))
        if (phase == "downloading") {
            nm()?.notify(NOTIF_ID, notif(done, total))
        }
    }

    private fun foreground(done: Int, total: Int): ForegroundInfo {
        val notif = notif(done, total)
        return if (Build.VERSION.SDK_INT >= 29) {
            ForegroundInfo(NOTIF_ID, notif, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            ForegroundInfo(NOTIF_ID, notif)
        }
    }

    private fun notif(done: Int, total: Int): Notification {
        val stop = Intent(applicationContext, DownloadStopReceiver::class.java)
            .putExtra(DownloadStopReceiver.EXTRA_WORK_NAME, workName())
        val stopPi = PendingIntent.getBroadcast(
            applicationContext, 0, stop,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val openPi = applicationContext.packageManager
            .getLaunchIntentForPackage(applicationContext.packageName)
            ?.let { PendingIntent.getActivity(applicationContext, 0, it,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE) }
        return NotificationCompat.Builder(applicationContext, CHANNEL)
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setContentTitle(applicationContext.getString(R.string.offline_dl_title))
            .setContentText(if (total > 0) "$done / $total"
                else applicationContext.getString(R.string.offline_dl_listing))
            .setProgress(if (total > 0) total else 0, done, total <= 0)
            .setOngoing(true)
            .setContentIntent(openPi)
            .addAction(0, applicationContext.getString(R.string.offline_dl_stop), stopPi)
            .build()
    }

    private fun ensureChannel() {
        if (Build.VERSION.SDK_INT >= 26) {
            nm()?.createNotificationChannel(NotificationChannel(
                CHANNEL, applicationContext.getString(R.string.offline_dl_channel),
                NotificationManager.IMPORTANCE_LOW))
        }
    }

    private fun nm() =
        applicationContext.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager

    private fun workName(): String =
        inputData.getString(K_WORK_NAME) ?: ""

    companion object {
        const val K_FOLDER = "folder"
        const val K_LEAVES = "leaves"
        const val K_ORIGINAL = "original"
        const val K_SCOPE = "scope"
        const val K_WORK_NAME = "workName"
        const val P_PHASE = "phase"
        const val P_DONE = "done"
        const val P_TOTAL = "total"
        const val P_TARGET = "target"
        const val P_REASON = "reason"
        const val P_FOLDER = "outFolder"

        const val CHANNEL = "bookq-downloads"
        const val NOTIF_ID = 4205
        const val MIN_HEADROOM = 24L * 1024 * 1024

        fun uniqueName(scopeKey: String, folder: String): String =
            "bookq-dl|$scopeKey|$folder"

        fun tag(scopeKey: String): String = "bookq-dl|$scopeKey"
    }
}

/** Notification "stop" action — cancels the unique work; the worker's
 *  isStopped check lands it in the partial-keep path. */
class DownloadStopReceiver : android.content.BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val name = intent.getStringExtra(EXTRA_WORK_NAME) ?: return
        WorkManager.getInstance(context).cancelUniqueWork(name)
    }

    companion object { const val EXTRA_WORK_NAME = "workName" }
}
