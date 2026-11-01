package dev.bookq.core.api

/**
 * Path encoding that mirrors JS `encodeURIComponent` per segment
 * (`app/static/offline-core.js` `encodePath`). Byte-exact parity with the
 * web client's URLs is required (cache keys / server path matching). The
 * composed path is split on "/" — flattened names like "parent/child"
 * naturally become two segments, exactly as the JS encodePath does.
 */
object ImagePath {
    /** JS encodeURIComponent character class: everything except
     *  A-Z a-z 0-9 - _ . ! ~ * ' ( ) gets percent-encoded. */
    private val UNRESERVED = BooleanArray(128).also { t ->
        for (c in 'A'..'Z') t[c.code] = true
        for (c in 'a'..'z') t[c.code] = true
        for (c in '0'..'9') t[c.code] = true
        for (c in "-_.!~*'()") t[c.code] = true
    }
    private const val HEX = "0123456789ABCDEF"

    /**
     * Encode one path segment like JS `encodeURIComponent`: the segment is
     * UTF-8 encoded and every byte that is not an unreserved ASCII char is
     * emitted as `%HH` (uppercase hex). Encoding whole UTF-8 bytes is
     * identical to JS's per-code-point behaviour for valid strings.
     */
    fun encodeSegment(segment: String): String {
        val bytes = segment.toByteArray(Charsets.UTF_8)
        val sb = StringBuilder(bytes.size + 8)
        for (b in bytes) {
            val u = b.toInt() and 0xFF
            if (u < 128 && UNRESERVED[u]) {
                sb.append(u.toChar())
            } else {
                sb.append('%').append(HEX[u shr 4]).append(HEX[u and 0x0F])
            }
        }
        return sb.toString()
    }

    /** Per-segment encode of a composed path (`/` stays a separator). */
    fun encodePath(path: String): String =
        path.split('/').joinToString("/", transform = ::encodeSegment)
}
