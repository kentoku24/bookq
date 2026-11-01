package dev.bookq.reader.nav

import kotlinx.serialization.Serializable

/** Type-safe Navigation Compose routes (M1 set — Browse/Reader come later). */
object Routes {
    @Serializable
    data object Home

    @Serializable
    data object Settings
}
