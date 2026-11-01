package dev.bookq.reader

import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

/** Server URL normalization + the cleartext policy (AppConfig.swift port). */
object ServerUrls {

    /**
     * Trim → add `https://` when scheme-less → validate → strip the
     * trailing "/". Returns null for non-http(s) schemes and for http on
     * non-loopback hosts (bearer tokens never travel in cleartext —
     * loopback + the Android emulator host `10.0.2.2` are the only
     * exceptions, matching the debug network security config).
     */
    fun normalize(raw: String): String? {
        var s = raw.trim()
        if (s.isEmpty()) return null
        if (!s.contains("://")) s = "https://$s"
        val url = s.toHttpUrlOrNull() ?: return null
        if (url.scheme != "http" && url.scheme != "https") return null
        if (url.scheme == "http" && !isLoopbackOrEmulator(url.host)) return null
        return url.toString().removeSuffix("/")
    }

    fun parse(raw: String): HttpUrl? = normalize(raw)?.toHttpUrlOrNull()

    /** localhost / 127.* / ::1 / the emulator's host bridge. */
    fun isLoopbackOrEmulator(host: String): Boolean {
        val h = host.lowercase().removePrefix("[").removeSuffix("]")
        return h == "localhost" || h.endsWith(".localhost") ||
            h.startsWith("127.") || h == "::1" || h == "10.0.2.2"
    }
}
