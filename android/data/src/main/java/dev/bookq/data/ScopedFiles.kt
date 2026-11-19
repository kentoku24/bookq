package dev.bookq.data

import java.io.File
import java.security.MessageDigest

/**
 * Per-scope file helpers shared by the snapshot-backed repositories.
 * Scope keys contain host/user strings that are unsafe as filenames —
 * sanitize to `[a-zA-Z0-9._-]` and append a short sha1 of the raw key so
 * different scopes can never collide after sanitization (iOS uses the same
 * sanitize approach in LibraryStore).
 */
object ScopedFiles {

    fun key(host: String, email: String?): String =
        if (email.isNullOrEmpty()) host else "$host|$email"

    /** Scope-key as a safe single path segment — readable prefix + 8-hex
     *  sha1 tail, same scheme as [fileName] minus the extension. */
    fun segment(scopeKey: String): String {
        val readable = scopeKey
            .map { if (it.isLetterOrDigit() || it == '-' || it == '_' || it == '.') it else '_' }
            .joinToString("")
            .take(48)
        val digest = MessageDigest.getInstance("SHA-1")
            .digest(scopeKey.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }
            .take(8)
        return "$readable-$digest"
    }

    /** Safe file name for a scope key — readable prefix + 8-hex sha1 tail. */
    fun fileName(prefix: String, scopeKey: String): String =
        "$prefix-${segment(scopeKey)}.json"

    /** Write `text` to `file` atomically (tmp + rename) — works on JVM and
     *  Android without pulling in androidx AtomicFile. */
    fun writeAtomic(file: File, text: String) {
        file.parentFile?.mkdirs()
        val tmp = File(file.parentFile, file.name + ".tmp")
        tmp.writeText(text)
        if (!tmp.renameTo(file)) {
            tmp.copyTo(file, overwrite = true)
            tmp.delete()
        }
    }

    fun readOrNull(file: File): String? =
        if (file.isFile) runCatching { file.readText() }.getOrNull() else null
}
