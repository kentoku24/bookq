package dev.bookq.reader.nav

import kotlinx.serialization.Serializable

/** Type-safe Navigation Compose routes. */
object Routes {
    /** Browse level — `path` is the composed `_fullPath` ("" = root).
     *  Each level is its own back-stack entry: framework state restore
     *  gives per-entry scroll persistence for free (web's mode|path key). */
    @Serializable
    data class Browse(val path: String = "")

    /** M2 stub for the reader (real viewer lands in M3). `page` is the
     *  saved position the tile carried, 0-based. */
    @Serializable
    data class Reader(val folder: String, val page: Int = 0)

    @Serializable
    data object Settings

    /** Dual-reader paired view — renders this device's own spread side. */
    @Serializable
    data object Pair
}
