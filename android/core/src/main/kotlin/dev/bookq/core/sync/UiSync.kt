package dev.bookq.core.sync

import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt

/* UI_SYNC helpers — browse scroll mirror (#300). Wire validation mirrors
 * app/reader_sync/protocol.py::_validate_ui_sync and pairClient.js
 * sendUiSync; the progress-ratio mapping mirrors browse/pairSync.js
 * _remoteTop. Pixel values are not portable across devices: when both
 * peers carry scrollH (the V2 flag) the shared quantity is the
 * normalized progress top / (scrollH - viewH), which the receiver maps
 * onto its own estimated LazyGrid scroll range. */
object UiSync {

    /** This deployment always carries scrollH — BOOKQ_UI_SYNC_V2 parity. */
    const val RATIO_CAPABLE = true

    val MODES = setOf("tile", "list", "card")

    /** Validated outbound payload (pairClient.sendUiSync parity):
     *  returns the frame payload or null when invalid. scrollH is
     *  optional — an out-of-range value is dropped rather than failing
     *  the whole frame (receivers treat a missing scrollH as legacy). */
    fun payload(
        path: String?,
        top: Double,
        viewH: Double,
        scrollH: Double?,
        mode: String?,
    ): JsonObject? {
        if (!top.isFinite() || top < 0) return null
        if (!viewH.isFinite() || viewH <= 0 || viewH > 100000) return null
        return buildJsonObject {
            put("view", JsonPrimitive("browse"))
            put("path", path?.let { JsonPrimitive(it) } ?: JsonNull)
            put("top", JsonPrimitive(top))
            put("viewH", JsonPrimitive(viewH))
            if (mode in MODES) put("mode", JsonPrimitive(mode))
            if (scrollH != null && scrollH.isFinite() &&
                scrollH > 0 && scrollH <= 1e8) {
                put("scrollH", JsonPrimitive(scrollH))
            }
        }
    }

    data class Frame(
        val path: String?,
        val top: Double,
        val viewH: Double,
        val scrollH: Double?,
        val mode: String?,
        val pred: Double?,
        val from: String?,
    )

    /** Parse an inbound UI_SYNC relay ({payload, from}) — lenient read of
     *  the server-validated shape; unknown fields ignored. */
    fun parse(f: JsonObject): Frame? {
        val p = f["payload"] as? JsonObject ?: return null
        if ((p["view"] as? JsonPrimitive)?.contentOrNull != "browse")
            return null
        val top = (p["top"] as? JsonPrimitive)?.doubleOrNull ?: return null
        val viewH = (p["viewH"] as? JsonPrimitive)?.doubleOrNull
            ?: return null
        if (!top.isFinite() || top < 0 || !viewH.isFinite() || viewH <= 0)
            return null
        val scrollH = (p["scrollH"] as? JsonPrimitive)?.doubleOrNull
            ?.takeIf { it.isFinite() && it > 0 }
        return Frame(
            path = (p["path"] as? JsonPrimitive)?.contentOrNull,
            top = top, viewH = viewH, scrollH = scrollH,
            mode = (p["mode"] as? JsonPrimitive)?.contentOrNull
                ?.takeIf { it in MODES },
            pred = (p["pred"] as? JsonPrimitive)?.doubleOrNull
                ?.takeIf { it.isFinite() && it >= 0 },
            from = (f["from"] as? JsonPrimitive)?.contentOrNull)
    }

    /* ── scroll-space mapping ── */

    /** max(0, scrollH - viewH). */
    fun maxTop(scrollH: Double, viewH: Double): Double =
        max(0.0, scrollH - viewH)

    /** Normalized peer progress: top / peerMax clamped to 0..1. */
    fun progress(top: Double, viewH: Double, scrollH: Double): Double {
        val peerMax = maxTop(scrollH, viewH)
        return if (peerMax > 0) min(1.0, top / peerMax) else 0.0
    }

    /** pairSync._remoteTop — peer px → local px. Ratio mapping when this
     *  deployment can emit scrollH (RATIO_CAPABLE) AND the frame carries
     *  one; otherwise the legacy one-screen px offset (role-dependent —
     *  the follower mirrors the page *below* the leader's view). */
    fun remoteTop(
        localViewH: Double, localScrollH: Double,
        top: Double, viewH: Double, scrollH: Double?,
        follower: Boolean,
    ): Double {
        val localMax = maxTop(localScrollH, localViewH)
        val t = if (RATIO_CAPABLE && scrollH != null && scrollH > 0) {
            progress(top, viewH, scrollH) * localMax
        } else {
            if (follower) top + viewH else top - localViewH
        }
        return t.coerceIn(0.0, localMax)
    }

    /* ── LazyGrid metrics estimation ──
     * Compose exposes item indices, not a scrollable px range: estimate
     * the grid's virtual px metrics from the average visible cell height
     * so both directions of the mapping share the same px math as web. */

    data class Metrics(val top: Double, val viewH: Double,
                       val scrollH: Double)

    /** Estimate grid px metrics. `firstIndex`/`firstOffsetPx` are the
     *  LazyGridState first-visible values; `columns` = cells per row;
     *  `cellPx` = average cell height incl. spacing; `totalItems` =
     *  layoutInfo.totalItemsCount; `viewportPx` = visible height. */
    fun estimateGridMetrics(
        firstIndex: Int, firstOffsetPx: Int, columns: Int,
        cellPx: Float, totalItems: Int, viewportPx: Int,
    ): Metrics {
        if (columns <= 0 || cellPx <= 0f || viewportPx <= 0)
            return Metrics(0.0, 0.0, 0.0)
        val rows = (totalItems + columns - 1) / columns
        val scrollH = rows * cellPx.toDouble()
        val top = (firstIndex / columns) * cellPx.toDouble() +
            firstOffsetPx.toDouble()
        return Metrics(top.coerceIn(0.0, maxTop(scrollH, viewportPx.toDouble())),
            viewH = viewportPx.toDouble(), scrollH = scrollH)
    }

    /** Inverse of the ratio map for a LazyGrid: a target px-top becomes
     *  (index, offsetPx) for scrollToItem — index space never overshoots
     *  the last row, offset clamps inside the cell. */
    fun targetIndexOffset(
        targetTop: Double, columns: Int, cellPx: Float,
        totalItems: Int,
    ): Pair<Int, Int> {
        if (columns <= 0 || cellPx <= 0f || totalItems <= 0 ||
            targetTop <= 0) return 0 to 0
        val rows = (totalItems + columns - 1) / columns
        val maxRow = rows - 1
        val rowPx = targetTop / cellPx
        val row = rowPx.toInt().coerceIn(0, maxRow)
        val off = ((rowPx - row) * cellPx).roundToInt()
            .coerceIn(0, cellPx.toInt())
        return (row * columns).coerceAtMost(totalItems - 1) to off
    }
}
