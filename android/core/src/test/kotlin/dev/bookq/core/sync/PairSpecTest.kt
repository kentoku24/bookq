package dev.bookq.core.sync

import org.junit.Assert.assertTrue
import org.junit.Test
import org.yaml.snakeyaml.Yaml
import java.io.File

/**
 * AsyncAPI drift guard — `api/asyncapi.yaml` ↔ `PairProto`/`PairModels`
 * (SpecConformanceTest parity for the REST spec, #198).
 *
 * The walk collects every `enum:`/`const:` declared under a schema
 * property named `type`, `code`, `role`, `by`, `from`, `side`,
 * `leaderSide`, `dataPlane`, `kind`, `status` or `phase` and asserts
 * each member is a value the production code knows. A spec addition
 * that lands without a matching PairProto update fails here — never
 * silently.
 */
class PairSpecTest {

    private val spec: Any by lazy {
        val dir = File(System.getProperty("bookq.apiDir"))
        Yaml().load(File(dir, "asyncapi.yaml").inputStream())
    }

    // MARK: - enum/const collection

    /**
     * Walk the YAML tree; every map containing an `enum` or `const`
     * entry contributes `(fieldName, values)` — the field name is the
     * schema property the enum constrains, discovered by looking at
     * the parent `properties:` map when present.
     */
    private fun collect(): Map<String, Set<String>> {
        val out = mutableMapOf<String, MutableSet<String>>()

        fun visit(node: Any?, fieldHint: String?) {
            when (node) {
                is Map<*, *> -> {
                    // properties: {name: {enum: [...]}} binds the enum
                    // to the property name.
                    val props = node["properties"] as? Map<*, *>
                    if (props != null) {
                        for ((k, v) in props) visit(v, k?.toString())
                    }
                    for ((k, v) in node) {
                        when (k?.toString()) {
                            "properties" -> Unit // handled above
                            "enum", "const" -> {
                                val vals = when (v) {
                                    is List<*> -> v.mapNotNull {
                                        it?.toString() }
                                    else -> listOfNotNull(v?.toString())
                                }
                                val key = fieldHint ?: "_"
                                out.getOrPut(key) { mutableSetOf() }
                                    .addAll(vals)
                            }
                            else -> visit(v, fieldHint)
                        }
                    }
                }
                is List<*> -> node.forEach { visit(it, fieldHint) }
                else -> Unit
            }
        }
        visit(spec, null)
        return out
    }

    private fun known(): Set<String> =
        PairProto.SEND_TYPES + PairProto.INBOUND_TYPES +
            PairProto.RELAY_TYPES

    // MARK: - assertions

    /** Every spec `type` value is a frame this client sends, routes or
     *  tolerates. */
    @Test fun typesAreKnown() {
        val types = collect()["type"] ?: error("no `type` enum in spec")
        val unknown = types - known()
        assertTrue("spec types not handled by PairProto: $unknown",
            unknown.isEmpty())
    }

    /** Spec ERROR codes must match the production set exactly. */
    @Test fun errorCodesMatch() {
        val codes = collect()["code"] ?: emptySet()
        val unknown = codes.filter { it.startsWith("ERR_") }
            .toSet() - PairProto.ERROR_CODES
        assertTrue("spec error codes unknown: $unknown",
            unknown.isEmpty())
    }

    /** `role`/`by`/`from` enums ⊆ PairRole members. */
    @Test fun rolesMatch() {
        val c = collect()
        val roles = (c["role"] ?: emptySet()) +
            (c["by"] ?: emptySet()) + (c["from"] ?: emptySet())
        val known = PairRole.entries.map { it.name }.toSet()
        val unknown = roles - known
        assertTrue("spec roles unknown: $unknown", unknown.isEmpty())
    }

    /** `side`/`leaderSide` enums ⊆ PairSide members. */
    @Test fun sidesMatch() {
        val c = collect()
        val sides = (c["side"] ?: emptySet()) +
            (c["leaderSide"] ?: emptySet())
        val known = PairSide.entries.map { it.name }.toSet()
        val unknown = sides - known
        assertTrue("spec sides unknown: $unknown", unknown.isEmpty())
    }

    /** `dataPlane` enum ⊆ supported planes (server-ws live in M7). */
    @Test fun dataPlanesMatch() {
        val planes = collect()["dataPlane"] ?: emptySet()
        val unknown = planes - PairProto.DATA_PLANES
        assertTrue("spec data planes unknown: $unknown",
            unknown.isEmpty())
        assertTrue("server-ws must remain a spec plane",
            planes.contains("server-ws"))
    }

    /** Presence `status`/`phase` enums ⊆ production members. */
    @Test fun phasesMatch() {
        val c = collect()
        val phases = (c["phase"] ?: emptySet())
        val known = PairPhase.entries.map { it.name }.toSet()
        val unknown = phases - known
        assertTrue("spec phases unknown: $unknown", unknown.isEmpty())
    }

    /** JOIN `code` pattern stays `^\d{6}$` (PairProto.JOIN_CODE). */
    @Test fun joinCodePattern() {
        assertTrue("6-digit join code must be accepted",
            PairProto.JOIN_CODE.matches("123456"))
        assertTrue(PairProto.JOIN_CODE.matches("000000"))
        listOf("12345", "1234567", "abcdef", "12345a").forEach {
            assertTrue("$it must be rejected",
                !PairProto.JOIN_CODE.matches(it))
        }
    }

    /** Protocol constants the spec fixes. */
    @Test fun constantsMatch() {
        // `v` const 1, text-frame 64 KiB, resume 300 s, liveness 20 s.
        assertTrue(PairProto.PROTOCOL_VERSION == 1)
        assertTrue(PairProto.MAX_FRAME_BYTES == 64 * 1024)
        assertTrue(PairProto.RESUME_WINDOW_MS == 300_000L)
        assertTrue(PairProto.LIVENESS_MS == 20_000L)
    }
}
