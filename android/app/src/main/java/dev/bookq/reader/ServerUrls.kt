package dev.bookq.reader

import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

/** Server URL normalization + the cleartext policy (AppConfig.swift port). */
object ServerUrls {

    /**
     * Trim → add `https://` when scheme-less → validate → strip the
     * trailing "/". Returns null for non-http(s) schemes and for http on
     * non-loopback hosts (bearer tokens never travel in cleartext —
     * the exceptions mirror the network security config exactly, so a
     * URL this accepts is always one the platform permits).
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

    /**
     * Exact match against the cleartext allow-list in
     * `network_security_config.xml`: debug permits `localhost` (+subs),
     * `127.0.0.1` and the emulator host bridge `10.0.2.2`; release
     * permits nothing. Anything accepted here but blocked there (other
     * 127.x, ::1) would surface as an opaque "接続できません".
     * A "127." prefix check is NOT safe — `127.evil.example` is a DNS
     * name, not an IP literal.
     */
    fun isLoopbackOrEmulator(host: String): Boolean {
        if (!BuildConfig.DEBUG) return false
        val h = host.lowercase().removePrefix("[").removeSuffix("]")
        return h == "localhost" || h.endsWith(".localhost") ||
            h == "127.0.0.1" || h == "10.0.2.2"
    }
}
