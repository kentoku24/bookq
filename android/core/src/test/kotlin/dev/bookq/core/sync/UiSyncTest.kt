package dev.bookq.core.sync

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/// UiSync payload validation + scroll-space mapping + LazyGrid metric
/// estimation — ports of pairSync.js _remoteTop / _payload semantics.
class UiSyncTest {

    // MARK: - payload validation (sendUiSync / _validate_ui_sync parity)

    @Test fun payloadRejectsBadRanges() {
        assertNull(UiSync.payload("p", -1.0, 800.0, 2000.0, "tile"))
        assertNull(UiSync.payload("p", Double.NaN, 800.0, 2000.0, "tile"))
        assertNull(UiSync.payload("p", 0.0, 0.0, 2000.0, "tile"))
        assertNull(UiSync.payload("p", 0.0, -5.0, 2000.0, "tile"))
        assertNull(UiSync.payload("p", 0.0, 100001.0, 2000.0, "tile"))
        assertNotNull(UiSync.payload("p", 0.0, 100000.0, 2000.0, "tile"))
    }

    @Test fun payloadDropsBadOptionalFields() {
        val p = UiSync.payload("p", 10.0, 800.0, 2e8, "weird")!!
        assertNull(p["scrollH"])
        assertNull(p["mode"])
        val p2 = UiSync.payload("p", 10.0, 800.0, -3.0, "card")!!
        assertNull(p2["scrollH"])
        assertEquals("card", p2["mode"]?.jsonPrimitive?.content)
    }

    @Test fun payloadShape() {
        val p = UiSync.payload("f/a", 250.0, 800.0, 2000.0, "list")!!
        assertEquals("browse", p["view"]?.jsonPrimitive?.content)
        assertEquals("f/a", p["path"]?.jsonPrimitive?.content)
        assertEquals("list", p["mode"]?.jsonPrimitive?.content)
        assertEquals(250.0, p["top"]!!.jsonPrimitive.content.toDouble(),
            0.001)
        val n = UiSync.payload(null, 0.0, 800.0, null, null)!!
        assertTrue(n["path"] is kotlinx.serialization.json.JsonNull)
        assertNull(n["mode"]); assertNull(n["scrollH"])
    }

    // MARK: - inbound parse

    private fun uiFrame(p: JsonObject) = buildJsonObject {
        put("v", 1); put("type", "UI_SYNC"); put("sessionId", "s1")
        put("from", "leader"); put("payload", p)
    }

    @Test fun parseRelayedFrame() {
        val f = UiSync.parse(uiFrame(buildJsonObject {
            put("view", "browse"); put("path", "f/a")
            put("top", 120.0); put("viewH", 800.0)
            put("scrollH", 4000.0); put("mode", "tile")
            put("pred", 900.0)
        }))!!
        assertEquals("f/a", f.path)
        assertEquals(120.0, f.top, 0.001)
        assertEquals(4000.0, f.scrollH!!, 0.001)
        assertEquals(900.0, f.pred!!, 0.001)
        assertEquals("leader", f.from)
        assertEquals("tile", f.mode)
    }

    @Test fun parseRejectsMalformed() {
        assertNull(UiSync.parse(buildJsonObject { put("payload",
            buildJsonObject { put("view", "reader") }) }))
        assertNull(UiSync.parse(uiFrame(buildJsonObject {
            put("view", "browse"); put("top", -1.0)
            put("viewH", 800.0) })))
        assertNull(UiSync.parse(uiFrame(buildJsonObject {
            put("view", "browse"); put("top", 10.0) })))   // no viewH
        // legacy frame without scrollH still parses
        val f = UiSync.parse(uiFrame(buildJsonObject {
            put("view", "browse"); put("top", 10.0); put("viewH", 800.0)
        }))!!
        assertNull(f.scrollH)
    }

    // MARK: - remoteTop mapping

    @Test fun ratioMapping() {
        // peer at 50% progress → local lands at 50% of own max
        val t = UiSync.remoteTop(
            localViewH = 1000.0, localScrollH = 5000.0,
            top = 500.0, viewH = 1000.0, scrollH = 2000.0,
            follower = true)
        assertEquals(2000.0, t, 0.001)   // 0.5 * (5000-1000)
    }

    @Test fun ratioClamps() {
        val t = UiSync.remoteTop(
            localViewH = 1000.0, localScrollH = 5000.0,
            top = 1e9, viewH = 1000.0, scrollH = 2000.0,
            follower = true)
        assertEquals(4000.0, t, 0.001)   // prog clamped to 1 → local max
    }

    @Test fun legacyPxFallback() {
        // no scrollH → one-screen px offset (role-dependent)
        assertEquals(1500.0, UiSync.remoteTop(
            localViewH = 1000.0, localScrollH = 5000.0,
            top = 500.0, viewH = 1000.0, scrollH = null,
            follower = true), 0.001)     // top + viewH
        assertEquals(0.0, UiSync.remoteTop(
            localViewH = 1000.0, localScrollH = 5000.0,
            top = 500.0, viewH = 1000.0, scrollH = null,
            follower = false), 0.001)    // top - localViewH → clamp 0
    }

    // MARK: - LazyGrid estimation

    @Test fun estimateMetrics() {
        // 100 items, 4 cols → 25 rows × 240px = 6000px; first row 3 half
        val m = UiSync.estimateGridMetrics(
            firstIndex = 12, firstOffsetPx = 120, columns = 4,
            cellPx = 240f, totalItems = 100, viewportPx = 1200)
        assertEquals(6000.0, m.scrollH, 0.001)
        assertEquals(1200.0, m.viewH, 0.001)
        assertEquals(840.0, m.top, 0.001)   // row 3 * 240 + 120
    }

    @Test fun estimateMetricsDegenerate() {
        val m = UiSync.estimateGridMetrics(
            firstIndex = 0, firstOffsetPx = 0, columns = 0,
            cellPx = 0f, totalItems = 10, viewportPx = 0)
        assertEquals(0.0, m.scrollH, 0.001)
        assertEquals(0.0, m.viewH, 0.001)
    }

    @Test fun targetIndexOffsetRoundTrip() {
        // px-top 840 on a 4-col 240px grid → row 3 → index 12, offset 120
        val (idx, off) = UiSync.targetIndexOffset(
            targetTop = 840.0, columns = 4, cellPx = 240f,
            totalItems = 100)
        assertEquals(12, idx)
        assertEquals(120, off)
        // past the end → clamps inside last row
        val (i2, _) = UiSync.targetIndexOffset(
            targetTop = 1e9, columns = 4, cellPx = 240f,
            totalItems = 100)
        assertEquals(96, i2)
    }
}

/// PairJoinCode — QR payload / bare-code parsing (#300).
class PairJoinCodeTest {
    @Test fun parsesJoinUrl() {
        assertEquals("232914", PairJoinCode.parse(
            "https://dev-01.example.ts.net/#/join/232914"))
        assertEquals("123456", PairJoinCode.parse("http://h/#/join/123456"))
        // trailing path/query tolerated
        assertEquals("654321", PairJoinCode.parse(
            "https://h/#/join/654321?x=1"))
    }
    @Test fun parsesBareCode() {
        assertEquals("000001", PairJoinCode.parse("000001"))
        assertEquals("999999", PairJoinCode.parse(" 999999 "))
    }
    @Test fun rejectsGarbage() {
        assertNull(PairJoinCode.parse(""))
        assertNull(PairJoinCode.parse("12345"))           // too short
        assertNull(PairJoinCode.parse("1234567"))         // too long
        assertNull(PairJoinCode.parse("https://h/#/browse/f"))
        assertNull(PairJoinCode.parse("https://h/#/join/abcde"))
        assertNull(PairJoinCode.parse("https://h/other/123456"))
    }
}
