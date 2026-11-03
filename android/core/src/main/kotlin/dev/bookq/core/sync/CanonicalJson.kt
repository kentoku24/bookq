package dev.bookq.core.sync

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.longOrNull
import java.security.MessageDigest

/**
 * Canonical JSON serialization matching Python's
 * `json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",",":"))`
 * — the byte-exact encoding `manifest_digest` is computed over
 * (app/reader_sync/catalog.py). Port of ios CanonicalJSON.swift. Rules:
 *   - object keys sorted by code point (ASCII keys in practice)
 *   - compact separators, no whitespace
 *   - `"` `\` and control chars escaped (\b \f \n \r \t short forms)
 *   - every char outside 0x20...0x7E emitted as `\uXXXX` per UTF-16 code
 *     unit — astral chars become surrogate-pair escapes, lowercase hex
 */
object CanonicalJson {

    fun encode(value: JsonElement): String {
        val out = StringBuilder()
        write(value, out)
        return out.toString()
    }

    private fun write(v: JsonElement, out: StringBuilder) {
        when (v) {
            is JsonNull -> out.append("null")
            is JsonObject -> {
                out.append('{')
                v.keys.sorted().forEachIndexed { i, k ->
                    if (i > 0) out.append(',')
                    writeString(k, out)
                    out.append(':')
                    write(v.getValue(k), out)
                }
                out.append('}')
            }
            is JsonArray -> {
                out.append('[')
                v.forEachIndexed { i, e ->
                    if (i > 0) out.append(',')
                    write(e, out)
                }
                out.append(']')
            }
            is JsonPrimitive -> when {
                v.isString -> writeString(v.content, out)
                v.booleanOrNull != null ->
                    out.append(if (v.booleanOrNull == true) "true" else "false")
                else -> writeNumber(v, out)
            }
        }
    }

    /** Python dumps: integral values emit without a fraction; doubles use
     *  the shortest repr — kotlin's Double.toString matches (1e15 →
     *  "1.0E15" differs from Python "1e+15", so stay under the bound). */
    private fun writeNumber(v: JsonPrimitive, out: StringBuilder) {
        val l = v.longOrNull
        if (l != null) {
            out.append(l)
            return
        }
        val d = v.doubleOrNull ?: run { out.append(v.content); return }
        if (d == Math.floor(d) && kotlin.math.abs(d) < 1e15 && !d.isInfinite()) {
            out.append(d.toLong())
        } else {
            out.append(d.toString())
        }
    }

    private fun writeString(s: String, out: StringBuilder) {
        out.append('"')
        for (unit in s) {
            when (unit) {
                '"' -> out.append("\\\"")
                '\\' -> out.append("\\\\")
                '\b' -> out.append("\\b")
                '\u000C' -> out.append("\\f")
                '\n' -> out.append("\\n")
                '\r' -> out.append("\\r")
                '\t' -> out.append("\\t")
                else -> if (unit.code in 0x20..0x7E) {
                    out.append(unit)
                } else {
                    out.append("\\u")
                    out.append(unit.code.toString(16).padStart(4, '0'))
                }
            }
        }
        out.append('"')
    }

    private val contentKeys =
        listOf("v", "folder", "offset", "chapters", "pageRefs", "spreadTable")

    /**
     * sha256 of the canonical encoding of the manifest's content keys —
     * catalog.manifest_digest. `obj` is the full parsed manifest.
     */
    fun manifestDigest(obj: JsonObject): String? {
        val content = buildMap {
            for (k in contentKeys) {
                put(k, obj[k] ?: return null)
            }
        }
        val canon = encode(JsonObject(content))
        val digest = MessageDigest.getInstance("SHA-256")
            .digest(canon.toByteArray(Charsets.UTF_8))
        return "sha256:" + digest.joinToString("") { "%02x".format(it) }
    }
}
