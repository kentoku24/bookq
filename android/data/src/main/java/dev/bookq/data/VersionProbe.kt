package dev.bookq.data

import dev.bookq.core.api.VersionResponse

/** Minimal boot-probe surface — implemented by [BookqApi]; faked in tests. */
interface VersionProbe {
    suspend fun version(): VersionResponse
}
