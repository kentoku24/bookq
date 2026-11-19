package dev.bookq.core

import dev.bookq.core.api.BookqJson
import dev.bookq.core.api.PositionBody
import dev.bookq.core.api.PositionsResponse
import dev.bookq.core.api.TreeNode
import dev.bookq.core.api.TreeResponse
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.int
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ModelsTest {
    @Test fun treeNodeDecodesTreeArray() {
        val json = """
        [{"name":"a","type":"folder","children":[
          {"name":"b/c","type":"folder","children":[{"name":"p1.jpg","type":"image"}],
            "thumb":"b/c/p1.jpg","thumbs":["b/c/p1.jpg"]}
        ],"thumb":"b/c/p1.jpg"}]
        """.trimIndent()
        val nodes = BookqJson.decodeFromString<List<TreeNode>>(json)
        assertEquals(1, nodes.size)
        assertEquals("b/c", nodes[0].children?.get(0)?.name)
        assertEquals("b/c/p1.jpg", nodes[0].children?.get(0)?.thumb)
    }

    /** v1 tree envelope — `{kind,nodes,generation}` wraps every mode. */
    @Test fun treeResponseEnvelope() {
        val env = BookqJson.decodeFromString<TreeResponse>(
            """{"kind":"tree","nodes":[{"name":"a","type":"folder"}],"generation":42}""")
        assertEquals("tree", env.kind)
        assertEquals(42, env.generation)
        assertEquals(1, env.nodes.size)
    }

    @Test fun treeResponseChildrenKind() {
        val env = BookqJson.decodeFromString<TreeResponse>(
            """{"kind":"children","nodes":[],"generation":1}""")
        assertEquals("children", env.kind)
        assertTrue(env.nodes.isEmpty())
    }

    /** v1 positions list — `{positions[]}` newest-first. */
    @Test fun positionsResponse() {
        val env = BookqJson.decodeFromString<PositionsResponse>(
            """{"positions":[{"folder":"a/b","page":3,"total":10,"ts":1000}]}""")
        assertEquals("a/b", env.positions.first().folder)
        assertEquals(3, env.positions.first().page)
        assertEquals(1000L, env.positions.first().ts)
    }

    @Test fun positionsResponseEmpty() {
        val env = BookqJson.decodeFromString<PositionsResponse>("""{"positions":[]}""")
        assertTrue(env.positions.isEmpty())
    }

    /** v1 PUT body carries {page,total,ts} ONLY — `folder` rides the query. */
    @Test fun positionBodyEncodesWithoutFolder() {
        val obj = BookqJson.parseToJsonElement(
            BookqJson.encodeToString(PositionBody(page = 3, total = 10, ts = 1234))
        ).jsonObject
        assertNull(obj["folder"])
        assertEquals(3, obj["page"]!!.jsonPrimitive.int)
        assertEquals(10, obj["total"]!!.jsonPrimitive.int)
        assertEquals(1234, obj["ts"]!!.jsonPrimitive.int)
    }

    @Test fun unknownKeysIgnored() {
        val n = BookqJson.decodeFromString<TreeNode>(
            """{"name":"x","type":"folder","future_field":{"a":1}}""")
        assertEquals("x", n.name)
        assertTrue(n.isFolder)
        assertFalse(n.isImage)
    }
}
