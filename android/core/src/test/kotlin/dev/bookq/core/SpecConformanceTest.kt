package dev.bookq.core

import dev.bookq.core.api.BookqJson
import dev.bookq.core.api.ComicMetadata
import dev.bookq.core.api.Facets
import dev.bookq.core.api.ImagesResponse
import dev.bookq.core.api.PositionBody
import dev.bookq.core.api.PositionStatus
import dev.bookq.core.api.PositionsResponse
import dev.bookq.core.api.ReadingPosition
import dev.bookq.core.api.SearchResponse
import dev.bookq.core.api.TokenPairResponse
import dev.bookq.core.api.TreeNode
import dev.bookq.core.api.TreeResponse
import dev.bookq.core.api.VersionResponse
import dev.bookq.core.api.VisibilitySnapshot
import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.encodeToJsonElement
import kotlinx.serialization.serializer
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.yaml.snakeyaml.Yaml
import java.io.File

/**
 * Drift guard for `api/openapi.v1.yaml` → `api/Models.kt` (iOS
 * SpecConformanceTests parity, #198).
 *
 * The spec file is read straight from the repo (path via the
 * `bookq.apiDir` system property set in `core/build.gradle.kts`), each
 * covered response/request schema is interpreted, a canonical JSON
 * instance is fabricated from it, and every production model must
 * (a) decode that instance and (b) re-encode every spec property. A spec
 * change that drifts from the hand-written models fails here.
 *
 * Anything the interpreter cannot handle throws — a hard failure, never
 * a silent pass.
 */
class SpecConformanceTest {

    // MARK: - Coverage table

    /** components.schemas entries → production model. */
    @Test fun componentSchemas() {
        assertModel<TreeNode>(schema = "TreeNode")
        assertModel<TreeResponse>(schema = "TreeResponse")
        assertModel<ComicMetadata>(schema = "ComicMetadata")
        assertModel<Facets>(schema = "Facets")
        assertModel<ReadingPosition>(schema = "ReadingPosition")
        assertModel<VisibilitySnapshot>(schema = "FolderVisibility")
    }

    /** Inline (non-$ref) response/request schemas under paths/. */
    @Test fun pathSchemas() {
        assertModel<ImagesResponse>(against = schemaAt(
            "paths", "/api/v1/images", "get", "responses",
            "200", "content", "application/json", "schema"))

        assertModel<PositionsResponse>(against = schemaAt(
            "paths", "/api/v1/reading-positions", "get", "responses",
            "200", "content", "application/json", "schema"))

        assertModel<PositionBody>(against = schemaAt(
            "paths", "/api/v1/reading-positions", "put", "requestBody",
            "content", "application/json", "schema"))

        assertModel<PositionStatus>(against = schemaAt(
            "paths", "/api/v1/reading-positions", "put", "responses",
            "200", "content", "application/json", "schema"))

        assertModel<PositionStatus>(against = schemaAt(
            "paths", "/api/v1/reading-positions", "delete", "responses",
            "200", "content", "application/json", "schema"))

        assertModel<SearchResponse>(against = schemaAt(
            "paths", "/api/v1/search", "get", "responses",
            "200", "content", "application/json", "schema"))

        assertModel<VersionResponse>(against = schemaAt(
            "paths", "/api/v1/version", "get", "responses",
            "200", "content", "application/json", "schema"))

        assertModel<TokenPairResponse>(against = schemaAt(
            "paths", "/api/v1/auth/token", "post", "responses",
            "200", "content", "application/json", "schema"))
    }

    // MARK: - Decode + re-encode assertion

    private inline fun <reified T> assertModel(schema: String) =
        assertModel<T>(against = schemaAt("components", "schemas", schema))

    /** Fabricated instance must decode AND re-encode every spec property —
     *  the re-encode direction is what catches a spec field the model
     *  silently drops (decode alone tolerates unknown keys). */
    @OptIn(ExperimentalSerializationApi::class)
    private inline fun <reified T> assertModel(against: SpecSchema) {
        val jsonText = BookqJson.encodeToJsonElement(
            valueToJson(fabricate(against))).toString()
        val model = BookqJson.decodeFromString(serializer<T>(), jsonText)
        // Spec-optional properties may be absent on the wire — the model
        // must still decode an instance containing required keys only.
        val minimal = BookqJson.encodeToJsonElement(
            valueToJson(fabricate(against, requiredOnly = true))).toString()
        BookqJson.decodeFromString(serializer<T>(), minimal)

        if (against !is SpecSchema.Obj) return
        val reencode = Json { ignoreUnknownKeys = true; explicitNulls = false; encodeDefaults = true }
        val dict = reencode.encodeToJsonElement(serializer<T>(), model)
        assertTrue("re-encoded $jsonText is not a JsonObject", dict is JsonObject)
        val obj = dict as JsonObject
        for (key in against.props.keys) {
            assertNotNull(
                "${typeName<T>()} does not round-trip spec property \"$key\"",
                obj[key])
        }
        for (key in obj.keys) {
            assertTrue(
                "${typeName<T>()} emits \"$key\", which is not in the spec schema",
                key in against.props)
        }
    }

    private inline fun <reified T> typeName(): String = T::class.simpleName ?: "model"

    // MARK: - Spec navigation (snakeyaml → Any? tree)

    private fun specDoc(): Map<*, *> {
        val dir = System.getProperty("bookq.apiDir")
            ?: error("bookq.apiDir system property not set")
        val file = File(dir, "openapi.v1.yaml")
        require(file.isFile) { "spec not found at ${file.absolutePath}" }
        val doc = Yaml().load<Any>(file.readText())
        require(doc is Map<*, *>) { "spec root is not a map" }
        return doc
    }

    private fun schemaAt(vararg path: String): SpecSchema {
        var node: Any? = specDoc()
        for (key in path) {
            require(node is Map<*, *>) { "non-map node under ${path.toList()}" }
            node = node[key]
                ?: error("missing key \"$key\" under ${path.toList()}")
        }
        return interpret(node, depth = 0)
    }

    // MARK: - Schema interpretation

    private sealed interface SpecSchema {
        data class Obj(val props: Map<String, SpecSchema>, val required: Set<String>) : SpecSchema
        data class Arr(val item: SpecSchema) : SpecSchema
        data class OneOf(val options: List<SpecSchema>) : SpecSchema
        data class Prim(val type: String, val literal: String? = null) : SpecSchema
        data class Nullable(val inner: SpecSchema) : SpecSchema
        /** recursive $ref — fabricates as []. */
        data object Cyclic : SpecSchema
    }

    private fun interpret(
        y: Any?,
        depth: Int,
        resolving: Set<String> = emptySet(),
    ): SpecSchema {
        require(y is Map<*, *>) { "schema node is not a map: $y" }
        require(depth <= 20) { "ref depth exceeded" }

        y["\$ref"]?.let { ref ->
            require(ref is String) { "non-string \$ref" }
            val name = ref.substringAfterLast('/')
            if (name in resolving) return SpecSchema.Cyclic
            val target = ((specDoc()["components"] as? Map<*, *>)
                ?.get("schemas") as? Map<*, *>)?.get(name)
                ?: error("unresolvable \$ref $ref")
            return interpret(target, depth + 1, resolving + name)
        }
        (y["oneOf"] as? List<*>)?.let { oneOf ->
            return SpecSchema.OneOf(oneOf.map { interpret(it, depth + 1, resolving) })
        }
        (y["anyOf"] as? List<*>)?.let { anyOf ->
            return SpecSchema.OneOf(anyOf.map { interpret(it, depth + 1, resolving) })
        }
        (y["allOf"] as? List<*>)?.let { allOf ->
            // merge object members
            val merged = LinkedHashMap<String, SpecSchema>()
            val req = LinkedHashSet<String>()
            for (m in allOf) {
                val s = interpret(m, depth + 1, resolving)
                if (s is SpecSchema.Obj) {
                    merged.putAll(s.props)
                    req.addAll(s.required)
                }
            }
            return SpecSchema.Obj(merged, req)
        }

        val typeNode = y["type"]
        if (typeNode is List<*>) {                // [type, "null"]
            val nonNull = typeNode.filter { it != "null" }
            if (nonNull.size != 1) {
                return SpecSchema.Nullable(SpecSchema.Prim("any"))
            }
            val rebuilt = LinkedHashMap<String, Any?>()
            rebuilt["type"] = nonNull[0]
            for (key in listOf("items", "properties", "required")) {
                y[key]?.let { rebuilt[key] = it }
            }
            return SpecSchema.Nullable(interpret(rebuilt, depth, resolving))
        }

        val literal = (y["const"] as? String)
            ?: ((y["enum"] as? List<*>)?.firstOrNull() as? String)
        val hasProps = y["properties"] != null
        return when {
            hasProps -> {
                val props = LinkedHashMap<String, SpecSchema>()
                val propsMap = y["properties"]
                require(propsMap is Map<*, *>) { "properties is not a map" }
                for ((k, v) in propsMap) {
                    require(k is String) { "non-string property key $k" }
                    props[k] = interpret(v, depth + 1, resolving)
                }
                val req = LinkedHashSet<String>()
                (y["required"] as? List<*>)?.forEach {
                    require(it is String) { "non-string required entry" }
                    req.add(it)
                }
                SpecSchema.Obj(props, req)
            }
            typeNode == "array" -> SpecSchema.Arr(
                interpret(y["items"] ?: "string", depth + 1, resolving))
            typeNode == "object" -> SpecSchema.Prim("object", literal) // free-form
            typeNode is String -> SpecSchema.Prim(typeNode, literal)
            typeNode == null -> SpecSchema.Prim("any", literal)
            else -> error("unknown schema type node: $typeNode")
        }
    }

    // MARK: - Instance fabrication

    private fun fabricate(
        s: SpecSchema,
        depth: Int = 4,
        requiredOnly: Boolean = false,
    ): Any? = when (s) {
        is SpecSchema.Prim -> when {
            s.literal != null -> s.literal
            s.type == "integer" || s.type == "number" -> 1
            s.type == "boolean" -> true
            s.type == "object" -> mapOf("k" to "spec")
            else -> "spec"
        }
        is SpecSchema.Obj -> buildMap {
            for ((k, v) in s.props) {
                if (!requiredOnly || k in s.required) {
                    put(k, fabricate(v, depth, requiredOnly))
                }
            }
        }
        is SpecSchema.Arr ->
            // A recursive element has no finite instance; [] is spec-legal.
            if (s.item is SpecSchema.Cyclic || depth <= 0) emptyList<Any>()
            else listOf(fabricate(s.item, depth - 1, requiredOnly))
        is SpecSchema.Nullable -> fabricate(s.inner, depth, requiredOnly)
        is SpecSchema.OneOf -> fabricate(s.options[0], depth, requiredOnly)
        SpecSchema.Cyclic -> emptyList<Any>()
    }

    /** Plain Any? tree → JsonElement for serialization. */
    private fun valueToJson(v: Any?): kotlinx.serialization.json.JsonElement = when (v) {
        null -> kotlinx.serialization.json.JsonNull
        is String -> kotlinx.serialization.json.JsonPrimitive(v)
        is Boolean -> kotlinx.serialization.json.JsonPrimitive(v)
        is Int -> kotlinx.serialization.json.JsonPrimitive(v)
        is Long -> kotlinx.serialization.json.JsonPrimitive(v)
        is Number -> kotlinx.serialization.json.JsonPrimitive(v.toDouble())
        is Map<*, *> -> kotlinx.serialization.json.JsonObject(
            v.mapKeys { (k, _) -> k as String }
                .mapValues { (_, x) -> valueToJson(x) })
        is List<*> -> kotlinx.serialization.json.JsonArray(v.map { valueToJson(it) })
        else -> error("cannot convert to JSON: $v")
    }
}
