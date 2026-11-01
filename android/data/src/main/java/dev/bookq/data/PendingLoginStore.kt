package dev.bookq.data

import android.content.Context
import android.util.AtomicFile
import dev.bookq.core.api.BookqJson
import java.io.File
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString

/** The in-flight PKCE attempt — persisted so a process death during the
 *  browser round-trip can still complete the login. */
@Serializable
data class PendingLogin(
    val baseUrl: String,
    val state: String,
    val verifier: String,
)

interface PendingLoginStore {
    fun load(): PendingLogin?
    fun save(pending: PendingLogin)
    fun clear()
}

/** Plain-file store in `noBackupFilesDir` (verifier stays off backups). */
class FilePendingLoginStore(context: Context) : PendingLoginStore {
    private val file = File(context.noBackupFilesDir, "bookq-pending-login.json")

    override fun load(): PendingLogin? {
        if (!file.isFile) return null
        return try {
            BookqJson.decodeFromString<PendingLogin>(file.readText())
        } catch (e: Exception) {
            null
        }
    }

    override fun save(pending: PendingLogin) {
        val atomic = AtomicFile(file)
        val out = atomic.startWrite()
        try {
            out.write(BookqJson.encodeToString(pending).toByteArray(Charsets.UTF_8))
            atomic.finishWrite(out)
        } catch (e: Exception) {
            atomic.failWrite(out)
            throw e
        }
    }

    override fun clear() {
        file.delete()
    }
}

/** Volatile store for tests. */
class InMemoryPendingLoginStore : PendingLoginStore {
    @Volatile private var pending: PendingLogin? = null
    override fun load(): PendingLogin? = pending
    override fun save(pending: PendingLogin) { this.pending = pending }
    override fun clear() { pending = null }
}
