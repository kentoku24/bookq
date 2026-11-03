package dev.bookq.core.sync

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.intOrNull

/**
 * Wire types for /ws/reader-sync protocol v1 — ports of
 * ios/Bookq/Sync/PairModels.swift (itself a port of
 * app/reader_sync/{protocol,model}.py + viewer/pairModel.js).
 * PageRef {folder,name} is the shared addressing scheme: `folder` is the
 * downloads-relative path, `name` the entry name inside it.
 */
enum class PairRole { leader, follower }
enum class PairSide { right, left }
enum class PairPhase { waiting, loading, reading, selecting, paused, ended }

/**
 * Protocol constants — the drift-guard counterpart of
 * `api/asyncapi.yaml` (PairSpecTest reads the spec and asserts these
 * sets cover every declared member; a spec change that adds a request
 * type, error code or enum value fails the conformance suite).
 */
object PairProto {
    /** Request-envelope `type` values this client sends. */
    val SEND_TYPES = setOf(
        "CREATE", "JOIN", "APPROVE", "REJECT", "RESUME", "LEAVE",
        "MATCH", "TURN", "GOTO", "SET_OFFSET", "SWAP_SIDES",
        "OPEN_WORK", "SET_ACTIVITY", "READY", "PING", "APPLIED",
    )

    /** Server/push `type` values `dispatchLocked` routes. */
    val INBOUND_TYPES = setOf(
        "HELLO", "PING", "PONG", "ACK", "REJECT", "JOIN_REQUEST",
        "JOIN_CANCEL", "STATE", "MANIFEST", "APPLIED", "END", "ERROR",
    )

    /** Spec request types tolerated without acting on (M8 relays). */
    val RELAY_TYPES = setOf("SIGNAL", "UI_SYNC", "TRANSPORT_STATE")

    /** `components.schemas` ERROR `code` enum. */
    val ERROR_CODES = setOf(
        "ERR_VERSION", "ERR_MALFORMED", "ERR_TYPE",
        "ERR_RANGE", "ERR_SIZE",
    )

    /** TRANSPORT_STATE `dataPlane` — `server-ws` is the M7 plane. */
    val DATA_PLANES = setOf("server-ws", "webrtc-dc")

    val JOIN_CODE = Regex("^\\d{6}$")
    const val PROTOCOL_VERSION = 1
    const val MAX_FRAME_BYTES = 64 * 1024
    const val MAX_MANIFEST_BYTES = 32 * 1024 * 1024
    const val RESUME_WINDOW_MS = 300_000L
    const val LIVENESS_MS = 20_000L
}

@Serializable
data class PairPageRef(val folder: String, val name: String)

data class PairChapter(
    val name: String,
    val folder: String,
    val pages: List<PairPageRef>,
)

data class PairSpread(
    val id: String,            // "c<chapterIdx>:s<spreadIdx>"
    val chapterFolder: String,
    val right: PairPageRef,
    val left: PairPageRef?,
)

/**
 * Committed work manifest (catalog.resolve_work). `digest` covers only
 * the content keys — see CanonicalJson.manifestDigest.
 */
data class PairManifest(
    val folder: String,
    val offset: Int,
    val chapters: List<PairChapter>,
    val pageRefs: List<PairPageRef>,
    val spreadTable: List<PairSpread>,
    val digest: String,
    val initialSpreadId: String?,
) {
    fun spreadIndex(byId: String): Int? =
        spreadTable.indexOfFirst { it.id == byId }.takeIf { it >= 0 }

    /** `spreadIndexForPage` — first spread containing the ref on either side. */
    fun spreadIndex(forPage: PairPageRef): Int? =
        spreadTable.indexOfFirst { it.right == forPage || it.left == forPage }
            .takeIf { it >= 0 }

    companion object {
        /** Parse the digest-verified manifest object. */
        fun parse(obj: kotlinx.serialization.json.JsonObject): PairManifest? {
            fun ref(v: kotlinx.serialization.json.JsonElement?): PairPageRef? {
                val d = v as? kotlinx.serialization.json.JsonObject
                    ?: return null
                val f = (d["folder"] as? kotlinx.serialization.json.JsonPrimitive)
                    ?.content ?: return null
                val n = (d["name"] as? kotlinx.serialization.json.JsonPrimitive)
                    ?.content ?: return null
                return PairPageRef(folder = f, name = n)
            }
            val folder = (obj["folder"]
                as? kotlinx.serialization.json.JsonPrimitive)?.content
                ?: return null
            val offset = (obj["offset"]
                as? kotlinx.serialization.json.JsonPrimitive)
                ?.intOrNull ?: return null
            val digest = (obj["digest"]
                as? kotlinx.serialization.json.JsonPrimitive)?.content
                ?: return null
            val chapterDicts = (obj["chapters"]
                as? kotlinx.serialization.json.JsonArray) ?: return null
            val refDicts = (obj["pageRefs"]
                as? kotlinx.serialization.json.JsonArray) ?: return null
            val spreadDicts = (obj["spreadTable"]
                as? kotlinx.serialization.json.JsonArray) ?: return null

            val chapters = chapterDicts.map { ce ->
                val c = ce as? kotlinx.serialization.json.JsonObject
                    ?: return null
                val name = (c["name"]
                    as? kotlinx.serialization.json.JsonPrimitive)?.content
                    ?: return null
                val cf = (c["folder"]
                    as? kotlinx.serialization.json.JsonPrimitive)?.content
                    ?: return null
                val pages = (c["pages"]
                    as? kotlinx.serialization.json.JsonArray)
                    ?.mapNotNull(::ref) ?: return null
                PairChapter(name = name, folder = cf, pages = pages)
            }
            val table = spreadDicts.map { se ->
                val s = se as? kotlinx.serialization.json.JsonObject
                    ?: return null
                val id = (s["id"]
                    as? kotlinx.serialization.json.JsonPrimitive)?.content
                    ?: return null
                val cf = (s["chapterFolder"]
                    as? kotlinx.serialization.json.JsonPrimitive)?.content
                    ?: return null
                val right = ref(s["right"]) ?: return null
                PairSpread(id = id, chapterFolder = cf, right = right,
                    left = ref(s["left"]))
            }
            val initial = ((obj["initial"]
                as? kotlinx.serialization.json.JsonObject)?.get("spreadId")
                as? kotlinx.serialization.json.JsonPrimitive)?.content
            return PairManifest(
                folder = folder, offset = offset, chapters = chapters,
                pageRefs = refDicts.mapNotNull(::ref),
                spreadTable = table, digest = digest,
                initialSpreadId = initial)
        }
    }
}

/** pairModel.turnIndex — chapter clamp when !crossChapter. */
object PairModel {
    fun turnIndex(
        table: List<PairSpread>, index: Int,
        delta: Int, crossChapter: Boolean,
    ): Int? {
        if (table.isEmpty() || index < 0 || index >= table.size) return null
        if (delta != 1 && delta != -1) return null
        val target = index + delta
        if (crossChapter) return target.coerceIn(0, table.size - 1)
        val chapter = table[index].chapterFolder
        var lo = index
        var hi = index
        while (lo > 0 && table[lo - 1].chapterFolder == chapter) lo--
        while (hi + 1 < table.size && table[hi + 1].chapterFolder == chapter) hi++
        return target.coerceIn(lo, hi)
    }

    /**
     * Canonical projection (§3.2): leader renders leaderSide, follower the
     * opposite. ref == null → blank side.
     */
    fun project(
        spread: PairSpread, role: PairRole, leaderSide: PairSide,
    ): Pair<PairSide, PairPageRef?> {
        val side = if (role == PairRole.leader) leaderSide
            else if (leaderSide == PairSide.right) PairSide.left
            else PairSide.right
        return side to if (side == PairSide.right) spread.right else spread.left
    }
}

/**
 * Paired-reader cover rules shared by the paired-reader gate (#216).
 */
object PairReaderGate {
    /**
     * Phases that keep the shared reader mounted. `loading` and `paused`
     * must stay presented: they are transient (manifest reload on
     * SET_OFFSET/OPEN_WORK, member away) and the view renders their own
     * overlays. `waiting` is excluded — the pair sheet owns that UX.
     */
    fun presents(phase: PairPhase?): Boolean =
        phase == PairPhase.reading || phase == PairPhase.loading ||
            phase == PairPhase.paused || phase == PairPhase.selecting

    /**
     * Whether dismissing the cover in this phase is the leader's intent to
     * go browse (SET_ACTIVITY selecting). Phase-driven dismissals —
     * transient loading, paused, ended — are not a browse intent, so the
     * gate fires only when the close happened while `reading`.
     */
    fun closeSignalsSelecting(phase: PairPhase?): Boolean =
        phase == PairPhase.reading
}

/** STATE.presence entry. */
data class PairPresenceEntry(
    val role: PairRole,
    val status: String,        // active | suspended | offline | pending
    val name: String,
    val ready: Boolean,
)

/** Committed snapshot — STATE frame minus the envelope. */
data class PairSnapshot(
    val sessionId: String?,
    val revision: Int,
    val workEpoch: Int,
    val manifestVersion: Int,
    val phase: PairPhase,
    val spreadId: String?,
    val offset: Int,
    val leaderSide: PairSide,
    val dataPlane: String,     // "server-ws" | "webrtc-dc"
)

/**
 * This device's current projection into the shared spread table —
 * pairedView.js apply() equivalent.
 */
data class PairProjection(
    val spreadIndex: Int,
    val spreadTotal: Int,
    val side: PairSide,
    val ref: PairPageRef?,      // null → blank side
    val anchor: PairPageRef,    // first real page of the spread
    val chapterName: String,
    val chapterFolder: String,
)

/** QR / deep-link join payload parser — the web QR carries
 *  `<origin>/#/join/<code>` (pairUI.js); a bare 6-digit code is accepted
 *  too so the scanner and the manual field share one validation. */
object PairJoinCode {
    private val URL_CODE = Regex("""#/join/(\d{6})(?:\D|$)""")
    private val BARE_CODE = Regex("""^(\d{6})$""")

    /** Extract the 6-digit join code from scanned text, or null. */
    fun parse(text: String): String? {
        val t = text.trim()
        URL_CODE.find(t)?.let { return it.groupValues[1] }
        BARE_CODE.matchEntire(t)?.let { return it.groupValues[1] }
        return null
    }
}
