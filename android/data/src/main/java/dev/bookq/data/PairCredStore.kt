package dev.bookq.data

import dev.bookq.core.sync.PairCredStore
import dev.bookq.core.sync.PairMember
import kotlinx.serialization.json.Json
import java.io.File

/**
 * File-backed resume credentials — one JSON document per (host,account)
 * scope so an account switch can never resurrect a foreign member
 * (iOS PairStore UserDefaults `bookq.pairSync.v1` parity).
 */
class PairCredStore(
    dir: File, host: String, email: String,
) : PairCredStore {
    private val file = File(dir,
        "pair-${ScopedFiles.key(host, email)}.json")
    private val json = Json { ignoreUnknownKeys = true }

    @Synchronized
    override fun load(): PairMember? =
        runCatching {
            json.decodeFromString<PairMember>(file.readText())
        }.getOrNull()

    @Synchronized
    override fun save(member: PairMember?) {
        if (member == null) {
            file.delete()
            return
        }
        val tmp = File(file.parentFile, file.name + ".tmp")
        tmp.writeText(json.encodeToString(PairMember.serializer(), member))
        if (!tmp.renameTo(file)) {
            tmp.copyTo(file, overwrite = true); tmp.delete()
        }
    }
}
