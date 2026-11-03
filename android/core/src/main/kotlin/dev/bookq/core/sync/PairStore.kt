package dev.bookq.core.sync

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put
import kotlin.random.Random

/** Inbound wire text — the store owns JSON decoding so the stream stays
 *  lightweight across the transport boundary. */
sealed interface PairEvent {
    data object Open : PairEvent
    data class Frame(val text: String) : PairEvent
    data class Closed(val code: Int, val reason: String) : PairEvent
    data class Failed(val message: String) : PairEvent
}

/** Transport contract — OkHttp implementation lives in :data. */
interface PairTransport {
    fun connect(url: String, token: String?): kotlinx.coroutines.flow.Flow<PairEvent>
    suspend fun send(text: String)
    suspend fun close()
}

sealed class PairException(message: String) : Exception(message) {
    class Rejected(val reason: String, val snapshot: JsonObject?) :
        PairException("rejected: $reason")
    class Disconnected : PairException("disconnected")
    class Malformed(msg: String) : PairException(msg)
}

/** Result a CREATE/JOIN/etc ACK resolves with (entry replies carry the
 *  bind fields + optional `ambiguous` MATCH candidates). */
typealias PairAckData = JsonObject

/** Persisted member credentials — survives process death for RESUME. */
@kotlinx.serialization.Serializable
data class PairMember(
    val sessionId: String,
    val memberId: String,
    val role: String,        // "leader" | "follower"
    val resumeToken: String,
)

interface PairCredStore {
    fun load(): PairMember?
    fun save(member: PairMember?)
}

/**
 * Dual-reader sync client — Kotlin port of ios/Bookq/Sync/PairStore.swift
 * (which ports viewer/pairClient.js + pairController.js; server-ws plane
 * only, `dataPlane` stays "server-ws").
 *
 * Lifecycle: idle → connecting → waiting(leader)/pending(follower) →
 * paired (phase from STATE drives the reader) → ended. RESUME rebinds a
 * member within the server's 300s window; nothing is ever replayed.
 */
class PairStore(
    private val scope: CoroutineScope,
    private val urlProvider: () -> String?,
    private val tokenProvider: () -> String?,
    private val transportFactory: () -> PairTransport,
    private val creds: PairCredStore,
    private val resumeWindowMs: Long = 300_000,
    private val nowMs: () -> Long = { System.currentTimeMillis() },
    private val rng: () -> Int = { Random.nextInt(0, Int.MAX_VALUE) },
) {
    sealed interface UiStatus {
        data object Idle : UiStatus
        data object Connecting : UiStatus
        data object Waiting : UiStatus
        data object Pending : UiStatus
        data object Paired : UiStatus
        data class Ended(val reason: String) : UiStatus
    }

    // MARK: - Published surface

    private val _status = MutableStateFlow<UiStatus>(UiStatus.Idle)
    val status: StateFlow<UiStatus> = _status
    private val _member = MutableStateFlow<PairMember?>(null)
    val member: StateFlow<PairMember?> = _member
    private val _code = MutableStateFlow<String?>(null)
    val code: StateFlow<String?> = _code
    data class JoinRequest(val memberId: String, val name: String)

    private val _joinRequest = MutableStateFlow<JoinRequest?>(null)
    val joinRequest: StateFlow<JoinRequest?> = _joinRequest
    private val _lastState = MutableStateFlow<PairSnapshot?>(null)
    val lastState: StateFlow<PairSnapshot?> = _lastState
    private val _manifest = MutableStateFlow<PairManifest?>(null)
    val manifest: StateFlow<PairManifest?> = _manifest
    private val _projection = MutableStateFlow<PairProjection?>(null)
    val projection: StateFlow<PairProjection?> = _projection
    private val _presence = MutableStateFlow<Map<String, PairPresenceEntry>>(emptyMap())
    val presence: StateFlow<Map<String, PairPresenceEntry>> = _presence
    private val _errorText = MutableStateFlow<String?>(null)
    val errorText: StateFlow<String?> = _errorText
    private val _endReason = MutableStateFlow<String?>(null)
    val endReason: StateFlow<String?> = _endReason

    val paired: Boolean
        get() = _status.value == UiStatus.Paired &&
            _lastState.value?.phase != PairPhase.waiting
    val role: PairRole?
        get() = _member.value?.role?.let { r ->
            PairRole.entries.firstOrNull { it.name == r } }
    val phase: PairPhase? get() = _lastState.value?.phase
    val bound: Boolean
        get() = _member.value != null &&
            _status.value != UiStatus.Idle &&
            _status.value != UiStatus.Connecting

    /** Peer display info (the other member in presence). */
    val peer: PairPresenceEntry?
        get() {
            val m = _member.value ?: return null
            return _presence.value.entries.firstOrNull { it.key != m.memberId }
                ?.value
        }

    // MARK: - Wiring

    /** Leader bootstrap / openWork initial-page resolution. */
    var openWorkResolver: (suspend (String) -> PairPageRef?)? = null
    /** Folder a hidden-root check must veto before it reaches the wire. */
    var visibilityBlocks: ((String) -> Boolean)? = null
    /** Leader bootstrap: which work to share when paired with no manifest. */
    var bootstrapWork: (suspend () -> Pair<PairPageRef, String>?)? = null

    private val lock = Mutex()
    private var transport: PairTransport? = null
    private var eventJob: Job? = null
    private var linkGen = 0
    private val pending = LinkedHashMap<String, CompletableDeferred<Unit>>()
    private var ridSeq = 0

    // protocol mirrors (pairClient.js _seen/_applied/_readyFor/_deferredState)
    private var seen = -1 to -1                     // (epoch, rev)
    private var applied = -1 to -1
    private var seenMv = 0
    private var readyFor: String? = null
    private var deferredState: JsonObject? = null
    private var manifestMV: Int? = null
    private var manifestEpoch: Int? = null

    private class Chunks(
        val key: String, val digest: String, val count: Int,
        val epoch: Int, val mv: Int,
    ) {
        val parts = arrayOfNulls<String>(count)
        var got = 0
    }
    private var chunks: Chunks? = null
    private val appliedPending = LinkedHashMap<Int, String>()   // rev -> requestId

    // reconnect machine
    private var offlineSince: Long? = null
    private var retryCount = 0
    private var retryJob: Job? = null
    private var livenessJob: Job? = null
    private var lastRx = Long.MIN_VALUE
    private var attemptingResume = false
    private var entryType: String? = null
    private var entryPayload: JsonObject = JsonObject(emptyMap())
    private var bootstrapTried = false
    private var suspended = false
    private var entryCont: CompletableDeferred<Unit>? = null

    private val json = Json { ignoreUnknownKeys = true }

    // MARK: - Entry ops

    /** Silent boot resume (web boot()): adopt persisted creds if any. */
    fun bootResume() {
        if (_status.value != UiStatus.Idle || creds.load() == null) return
        scope.launch { runCatching { resume() } }
    }

    suspend fun create() = entry(Entry.CREATE, "CREATE",
        JsonObject(emptyMap()), memberOverride = null)

    suspend fun join(code: String) {
        if (!code.matches(PairProto.JOIN_CODE)) {
            throw PairException.Malformed("code must be 6 digits")
        }
        entry(Entry.JOIN, "JOIN", buildJsonObject { put("code", code) },
            memberOverride = null)
    }

    /** `pick` = matched memberId hint (web "この端末と同期" entry point). */
    suspend fun match(pick: String? = null) {
        val p = buildJsonObject { pick?.let { put("pick", it) } }
        entry(Entry.MATCH, "MATCH", p, memberOverride = null)
    }

    suspend fun resume() {
        val c = _member.value ?: creds.load()
            ?: run { finishEnded("credentials_lost"); return }
        if (offlineSince == null) offlineSince = nowMs()
        entry(Entry.RESUME, "RESUME", buildJsonObject {
            put("memberId", c.memberId); put("resumeToken", c.resumeToken)
        }, memberOverride = c)
    }

    private enum class Entry { CREATE, JOIN, MATCH, RESUME }

    private suspend fun entry(
        mode: Entry, type: String, payload: JsonObject,
        memberOverride: PairMember?,
    ) {
        // Leave politely first, then reset (web _leaveBound +
        // _teardownLink). The resume window must survive teardown —
        // it clears offlineSince/retryCount which reconnect owns.
        if (bound) {
            runCatching {
                sendRequest("LEAVE", JsonObject(emptyMap()))
            }
        }
        lock.withLock {
            val priorOffline = offlineSince
            val priorRetry = retryCount
            val priorSuspended = suspended
            teardownLocked()
            offlineSince = priorOffline
            retryCount = priorRetry
            suspended = priorSuspended
            attemptingResume = (mode == Entry.RESUME)
            if (memberOverride != null) _member.value = memberOverride
            entryType = type
            entryPayload = payload
            _status.value = UiStatus.Connecting
        }
        openSocket()
        val waiter = CompletableDeferred<Unit>()
        lock.withLock { entryCont = waiter }
        armEntryTimeout()
        waiter.await()
    }

    private fun armEntryTimeout() {
        scope.launch {
            delay(10_000)
            lock.withLock {
                val c = entryCont ?: return@withLock
                entryCont = null
                linkDeadLocked()
                c.completeExceptionally(PairException.Disconnected())
            }
        }
    }

    // MARK: - Socket lifecycle

    private fun openSocket() {
        linkGen += 1
        val url = urlProvider() ?: run {
            finishEnded("error"); return
        }
        val t = transportFactory()
        transport = t
        val token = tokenProvider()
        val gen = linkGen
        eventJob?.cancel()
        eventJob = scope.launch {
            t.connect(url, token).collect { ev -> handle(ev, gen) }
        }
    }

    private suspend fun handle(ev: PairEvent, gen: Int) {
        lock.withLock {
            if (gen != linkGen) return
            when (ev) {
                PairEvent.Open -> Unit
                is PairEvent.Frame -> {
                    lastRx = nowMs(); armLivenessLocked()
                    val f = runCatching {
                        json.parseToJsonElement(ev.text).jsonObject
                    }.getOrNull() ?: return   // undecodable — drop
                    dispatchLocked(f)
                }
                is PairEvent.Closed, is PairEvent.Failed -> linkDeadLocked()
            }
        }
    }

    private fun armLivenessLocked() {
        livenessJob?.cancel()
        livenessJob = scope.launch {
            delay(20_000)
            lock.withLock {
                // 4 missed server PINGs — treat as a black-holed path.
                if (nowMs() - lastRx >= 20_000) linkDeadLocked()
            }
        }
    }

    private fun linkDeadLocked() {
        // Reject all in-flight requests; nothing is replayed (§3.5).
        val pend = pending.toMap(); pending.clear()
        for ((_, c) in pend) c.completeExceptionally(PairException.Disconnected())
        entryCont?.let {
            entryCont = null
            it.completeExceptionally(PairException.Disconnected())
        }
        if (_member.value != null) {
            if (offlineSince == null) offlineSince = nowMs()
            _status.value = UiStatus.Connecting
            scheduleRetryLocked()
        } else {
            _status.value = UiStatus.Idle
        }
    }

    private fun scheduleRetryLocked() {
        retryJob?.cancel()
        val base = doubleArrayOf(0.5, 1.0, 2.0, 5.0)[retryCount.coerceAtMost(3)]
        val jitter = Random.nextDouble(0.0, 0.5)
        retryCount += 1
        val off = offlineSince
        if (off != null &&
            nowMs() - off + ((base + jitter) * 1000).toLong() > resumeWindowMs) {
            finishEndedLocked("resume_expired"); return
        }
        retryJob = scope.launch {
            delay(((base + jitter) * 1000).toLong())
            lock.withLock {
                if (_member.value != null) {
                    scope.launch { runCatching { resume() } }
                }
            }
        }
    }

    private fun teardownLocked() {
        eventJob?.cancel(); eventJob = null
        livenessJob?.cancel(); livenessJob = null
        retryJob?.cancel(); retryJob = null
        val t = transport; transport = null
        if (t != null) scope.launch { t.close() }
        for ((_, c) in pending) c.completeExceptionally(PairException.Disconnected())
        pending.clear()
        entryCont?.completeExceptionally(PairException.Disconnected())
        entryCont = null
        _member.value = null; _code.value = null; _joinRequest.value = null
        _lastState.value = null; _manifest.value = null; _projection.value = null
        _presence.value = emptyMap()
        seen = -1 to -1; applied = -1 to -1; seenMv = 0
        readyFor = null; deferredState = null
        manifestMV = null; manifestEpoch = null; chunks = null
        appliedPending.clear()
        offlineSince = null; retryCount = 0
        bootstrapTried = false
        suspended = false
        attemptingResume = false
    }

    // MARK: - Frame dispatch (inbound)

    private fun dispatchLocked(f: JsonObject) {
        if ((f["v"] as? JsonPrimitive)?.intOrNull != 1) {
            finishEndedLocked("incompatible"); return
        }
        when ((f["type"] as? JsonPrimitive)?.content) {
            "HELLO" -> onHelloLocked()
            "PING" -> sendRaw(buildJsonObject {
                put("v", 1); put("type", "PONG")
                put("seq", f["seq"] ?: JsonNull) })
            "PONG" -> Unit
            "ACK" -> onAckLocked(f)
            "REJECT" -> onRejectLocked(f)
            "JOIN_REQUEST" -> {
                _joinRequest.value = JoinRequest(
                    (f["memberId"] as? JsonPrimitive)?.content ?: "",
                    (f["name"] as? JsonPrimitive)?.content ?: "")
                // Auto-approve (pairController parity).
                scope.launch { runCatching { approve() } }
            }
            "JOIN_CANCEL" -> _joinRequest.value = null
            "STATE" -> onStateLocked(f)
            "MANIFEST" -> onManifestLocked(f)
            "APPLIED" -> {
                val rev = (f["revision"] as? JsonPrimitive)?.intOrNull
                val rid = (f["requestId"] as? JsonPrimitive)?.content
                if (rev != null && rid != null) appliedPending[rev] = rid
            }
            "END" -> {
                val r = (f["reason"] as? JsonPrimitive)?.content ?: "session_lost"
                finishEndedLocked(if (r == "shutdown") "session_lost" else r)
            }
            "ERROR" ->
                _errorText.value =
                    (f["code"] as? JsonPrimitive)?.content ?: "error"
            else -> Unit    // SIGNAL / UI_SYNC / TRANSPORT_STATE unused
        }
    }

    private fun onHelloLocked() {
        val cont = entryCont ?: return
        val type = entryType ?: return
        val req = envelope(type, entryPayload,
            sessionId = _member.value?.sessionId)
        pending[req.requestId] = cont
        entryCont = null
        sendReq(req)
    }

    // MARK: - Requests

    private class OutRequest(val requestId: String, val text: String)

    private fun rid(): String {
        ridSeq += 1
        return "r$ridSeq${rng().toString(36)}"
    }

    private fun envelope(
        type: String, payload: JsonObject,
        sessionId: String? = null,
        workEpoch: Int? = null, baseRevision: Int? = null,
    ): OutRequest {
        val requestId = rid()
        val f = buildJsonObject {
            put("v", 1); put("requestId", requestId)
            put("sessionId", (sessionId ?: _member.value?.sessionId)
                ?.let { JsonPrimitive(it) } ?: JsonNull)
            put("workEpoch", workEpoch ?: seen.first.coerceAtLeast(0))
            put("baseRevision", baseRevision ?: seen.second.coerceAtLeast(0))
            put("type", type)
            put("payload", payload)
        }
        return OutRequest(requestId = requestId, text = f.toString())
    }

    /** Send a session-bound request; resolves with ACK or throws. */
    suspend fun sendRequest(
        type: String, payload: JsonObject,
        workEpoch: Int? = null, baseRevision: Int? = null,
    ) {
        val req = lock.withLock {
            envelope(type, payload,
                workEpoch = workEpoch, baseRevision = baseRevision)
        }
        val waiter = CompletableDeferred<Unit>()
        lock.withLock { pending[req.requestId] = waiter }
        scope.launch {
            try {
                transport?.send(req.text)
            } catch (e: Exception) {
                lock.withLock {
                    pending.remove(req.requestId)
                        ?.completeExceptionally(e)
                }
            }
        }
        waiter.await()
    }

    private fun onAckLocked(f: JsonObject) {
        val rid = (f["requestId"] as? JsonPrimitive)?.content ?: return
        val cont = pending.remove(rid) ?: return
        val data = (f["data"] as? JsonObject) ?: JsonObject(emptyMap())
        bindFromAckLocked(data, (f["request"] as? JsonPrimitive)?.content)
        cont.complete(Unit)
    }

    private fun onRejectLocked(f: JsonObject) {
        val reason = (f["reason"] as? JsonPrimitive)?.content ?: "malformed"
        val rid = (f["requestId"] as? JsonPrimitive)?.content
        val cont = rid?.let { pending.remove(it) }
        val wasEntry = cont != null && _member.value == null && !attemptingResume
        (f["snapshot"] as? JsonObject)?.let { adoptSnapshotLocked(it) }
        if (attemptingResume) {
            when (reason) {
                "resume_in_use" -> {
                    cont?.completeExceptionally(
                        PairException.Rejected(reason, null))
                    scheduleRetryLocked()
                }
                "bad_resume" -> {
                    cont?.completeExceptionally(
                        PairException.Rejected(reason, null))
                    finishEndedLocked("session_lost")
                }
                else -> {
                    cont?.completeExceptionally(
                        PairException.Rejected(reason, null))
                    finishEndedLocked(reason)
                }
            }
            return
        }
        cont?.completeExceptionally(
            PairException.Rejected(reason, f["snapshot"] as? JsonObject))
        if (wasEntry) {
            // Dead-end entry (bad code etc.) — don't hold the socket.
            _errorText.value = reason
            teardownLocked()
            _status.value = UiStatus.Idle
        }
    }

    private fun bindFromAckLocked(d: JsonObject, requestType: String?) {
        val sid = (d["sessionId"] as? JsonPrimitive)?.content ?: return
        val mid = (d["memberId"] as? JsonPrimitive)?.content ?: return
        val role = (d["role"] as? JsonPrimitive)?.content ?: return
        val tok = (d["resumeToken"] as? JsonPrimitive)?.content ?: return
        // MATCH ambiguous: no bind fields — caller handles candidates.
        if ((d["ambiguous"] as? JsonPrimitive)?.booleanOrNull == true) return
        val m = PairMember(sessionId = sid, memberId = mid,
            role = role, resumeToken = tok)
        _member.value = m
        creds.save(m)
        (d["code"] as? JsonPrimitive)?.content?.let { _code.value = it }
        readyFor = null
        retryCount = 0; offlineSince = null; attemptingResume = false
        // JOIN binds a *pending* candidate — paired only once the leader's
        // APPROVE lands and STATE shows both members active.
        _status.value =
            if (requestType == "JOIN") UiStatus.Pending else UiStatus.Paired
        // A resumed member is re-marked 'active' server-side — re-assert a
        // local suspension so the session doesn't un-pause for nobody.
        if (requestType == "RESUME" && suspended) setActivity("suspended")
    }

    // MARK: - STATE / MANIFEST / projection

    private fun adoptSnapshotLocked(snap: JsonObject) {
        val pseudo = buildJsonObject {
            put("v", 1); put("type", "STATE")
            put("sessionId", snap["sessionId"]
                ?: _member.value?.sessionId?.let { JsonPrimitive(it) }
                ?: JsonNull)
            put("revision", snap["revision"] ?: JsonPrimitive(0))
            put("workEpoch", snap["workEpoch"] ?: JsonPrimitive(0))
            put("manifestVersion", snap["manifestVersion"] ?: JsonPrimitive(0))
            put("phase", snap["phase"] ?: JsonPrimitive("waiting"))
            put("spreadId", snap["spreadId"] ?: JsonNull)
            put("offset", snap["offset"] ?: JsonPrimitive(0))
            put("leaderSide", snap["leaderSide"] ?: JsonPrimitive("left"))
            put("presence", buildJsonObject {
                for ((k, v) in _presence.value) {
                    put(k, buildJsonObject {
                        put("role", v.role.name); put("status", v.status)
                        put("name", v.name); put("ready", v.ready)
                    })
                }
            })
            put("dataPlane", snap["dataPlane"] ?: JsonPrimitive("server-ws"))
        }
        onStateLocked(pseudo)
    }

    private fun onStateLocked(f: JsonObject) {
        val rev = (f["revision"] as? JsonPrimitive)?.intOrNull ?: return
        val epoch = (f["workEpoch"] as? JsonPrimitive)?.intOrNull ?: return
        _member.value?.let { m ->
            val sid = (f["sessionId"] as? JsonPrimitive)?.content
            if (sid != null && sid != m.sessionId) return
        }
        val newer = epoch > seen.first ||
            (epoch == seen.first && rev > seen.second)
        if (!newer) return    // dup / out-of-order
        seen = epoch to rev
        seenMv = (f["manifestVersion"] as? JsonPrimitive)?.intOrNull ?: 0
        _errorText.value = null
        maybeApplyLocked(f)
    }

    /** _maybeApply — manifest gate + applied dedup + apply. Called for
     *  fresh STATEs AND deferred replays: a replay skips the seen dedup
     *  (it already bumped `seen`), matching web `_maybeApply` — replaying
     *  through the seen-gate would drop the frame iOS-style. */
    private fun maybeApplyLocked(f: JsonObject) {
        val rev = (f["revision"] as? JsonPrimitive)?.intOrNull ?: return
        val epoch = (f["workEpoch"] as? JsonPrimitive)?.intOrNull ?: return
        _presence.value =
            (f["presence"] as? JsonObject)?.mapValues { (_, v) ->
                val d = v as? JsonObject
                PairPresenceEntry(
                    role = PairRole.entries.firstOrNull {
                        it.name == ((d?.get("role")
                            as? JsonPrimitive)?.content) }
                        ?: PairRole.follower,
                    status = (d?.get("status")
                        as? JsonPrimitive)?.content ?: "",
                    name = (d?.get("name")
                        as? JsonPrimitive)?.content ?: "",
                    ready = (d?.get("ready")
                        as? JsonPrimitive)?.booleanOrNull ?: false)
            } ?: emptyMap()

        val snap = PairSnapshot(
            sessionId = (f["sessionId"] as? JsonPrimitive)?.content,
            revision = rev, workEpoch = epoch, manifestVersion = seenMv,
            phase = PairPhase.entries.firstOrNull {
                it.name == ((f["phase"] as? JsonPrimitive)?.content) }
                ?: PairPhase.waiting,
            spreadId = (f["spreadId"] as? JsonPrimitive)?.content,
            offset = (f["offset"] as? JsonPrimitive)?.intOrNull ?: 0,
            leaderSide = PairSide.entries.firstOrNull {
                it.name == ((f["leaderSide"] as? JsonPrimitive)?.content) }
                ?: PairSide.left,
            dataPlane = (f["dataPlane"] as? JsonPrimitive)?.content
                ?: "server-ws")
        _lastState.value = snap

        // Status follows the committed phase.
        if (_member.value != null) {
            _status.value = when (snap.phase) {
                PairPhase.waiting -> if (role == PairRole.leader)
                    UiStatus.Waiting else UiStatus.Pending
                else -> UiStatus.Paired
            }
        }

        // Apply only when we hold the manifest this STATE references.
        val needMv = snap.manifestVersion
        val haveManifest = _manifest.value != null &&
            manifestEpoch == epoch && manifestMV == needMv
        if (needMv > 0 && !haveManifest) {
            deferredState = f; return
        }
        if (applied.first == epoch && applied.second >= rev) return
        if (epoch < applied.first) return
        applied = epoch to rev
        applyStateLocked(snap)
        appliedPending.remove(rev)?.let { rid ->
            sendReq(envelope("APPLIED", buildJsonObject {
                put("requestId", rid) }, workEpoch = epoch, baseRevision = rev))
        }
        maybeBootstrapLocked(snap)
    }

    /** pairedView.apply — project the committed spread onto this device. */
    private fun applyStateLocked(s: PairSnapshot) {
        val manifest = _manifest.value
        val sid = s.spreadId
        val si = sid?.let { manifest?.spreadIndex(it) }
        val role = role
        if ((s.phase != PairPhase.reading &&
                s.phase != PairPhase.loading &&
                s.phase != PairPhase.paused) ||
            manifest == null || sid == null || si == null || role == null) {
            if (s.phase == PairPhase.selecting) _projection.value = null
            return
        }
        val spread = manifest.spreadTable[si]
        val (side, ref) = PairModel.project(spread, role, s.leaderSide)
        val chName = manifest.chapters
            .firstOrNull { it.folder == spread.chapterFolder }?.name
            ?: spread.chapterFolder
        _projection.value = PairProjection(
            spreadIndex = si, spreadTotal = manifest.spreadTable.size,
            side = side, ref = ref, anchor = spread.right,
            chapterName = chName, chapterFolder = spread.chapterFolder)
    }

    /** Leader bootstrap (pairController._maybeBootstrapWork): a fresh
     *  pair with no manifest shares the leader's current/last-read work. */
    private fun maybeBootstrapLocked(s: PairSnapshot) {
        if (bootstrapTried || role != PairRole.leader ||
            s.phase != PairPhase.selecting || s.manifestVersion != 0) return
        bootstrapTried = true
        scope.launch {
            val pick = bootstrapWork?.invoke() ?: return@launch
            lock.withLock {
                openWork(folder = pick.second, page = pick.first)
            }
        }
    }

    private fun onManifestLocked(f: JsonObject) {
        val kind = (f["kind"] as? JsonPrimitive)?.content
        val idx = (f["index"] as? JsonPrimitive)?.intOrNull
        val count = (f["count"] as? JsonPrimitive)?.intOrNull
        val data = (f["data"] as? JsonPrimitive)?.content
        val digest = (f["digest"] as? JsonPrimitive)?.content
        if (kind != "manifest-chunk" || idx == null || count == null ||
            count < 1 || idx < 0 || idx >= count || data == null ||
            digest == null) {
            _errorText.value = "manifest_error"; return
        }
        val epoch = (f["workEpoch"] as? JsonPrimitive)?.intOrNull ?: -1
        val mv = (f["manifestVersion"] as? JsonPrimitive)?.intOrNull ?: -1
        // Re-delivery of the held manifest → re-READY (post-RESUME).
        if (_manifest.value != null && manifestEpoch == epoch &&
            manifestMV == mv) {
            sendReady(); return
        }
        // Stale delivery — ignore entirely.
        val haveE = manifestEpoch; val haveM = manifestMV
        if (haveE != null && haveM != null &&
            (epoch < haveE || (epoch == haveE && mv < haveM))) return
        val key = "${(f["sessionId"] as? JsonPrimitive)?.content ?: ""}|$epoch|$mv"
        if (chunks?.key != key) chunks = Chunks(key, digest, count, epoch, mv)
        val b = chunks ?: return
        if (b.digest != digest || b.count != count) {
            chunks = null; _errorText.value = "manifest_error"; return
        }
        if (b.parts[idx] == null) {
            b.parts[idx] = data; b.got += 1
        }
        if (b.got == b.count) finishManifestLocked(b)
    }

    private fun finishManifestLocked(b: Chunks) {
        chunks = null
        val text = b.parts.joinToString("") { it ?: "" }
        if (text.toByteArray(Charsets.UTF_8).size > 32 * 1024 * 1024) {
            _errorText.value = "manifest_error"; return
        }
        val obj = runCatching {
            json.parseToJsonElement(text).jsonObject }.getOrNull()
        if (obj == null ||
            (obj["digest"] as? JsonPrimitive)?.content != b.digest ||
            CanonicalJson.manifestDigest(obj) != b.digest) {
            _errorText.value = "manifest_error"; return
        }
        val parsed = PairManifest.parse(obj)
        if (parsed == null) {
            _errorText.value = "manifest_error"; return
        }
        _manifest.value = parsed
        manifestEpoch = b.epoch
        manifestMV = b.mv
        sendReady()
        deferredState?.let { d ->
            deferredState = null
            maybeApplyLocked(d)
        }
    }

    /** READY once per (workEpoch, manifestVersion) — rides the manifest's
     *  own epoch (the commit STATE may lag the chunks). */
    private fun sendReady() {
        if (_member.value == null) return
        val e = manifestEpoch ?: return
        val mv = manifestMV ?: return
        val key = "$e:$mv"
        if (readyFor == key) return
        readyFor = key
        scope.launch {
            runCatching {
                sendRequest("READY", buildJsonObject {
                    put("manifestVersion", mv) }, workEpoch = e)
            }
        }
    }

    // MARK: - Session control

    suspend fun approve() {
        val jr = lock.withLock {
            val j = _joinRequest.value
            _joinRequest.value = null
            j
        } ?: return
        sendRequest("APPROVE", buildJsonObject {
            put("memberId", jr.memberId) })
    }

    suspend fun rejectJoin() {
        val jr = lock.withLock {
            val j = _joinRequest.value
            _joinRequest.value = null
            j
        } ?: return
        sendRequest("REJECT", buildJsonObject {
            put("memberId", jr.memberId) })
    }

    /**
     * Polite leave — the server ENDs the session for both sides.
     * Faithful port of web `_leaveBound`: detach the transport so teardown
     * cannot close it first; wait (bounded) for a pending entry ACK — a
     * LEAVE sent earlier is rejected NO_SESSION and the peer sits in
     * peer-away until the resume window ends; then send LEAVE → close,
     * chained on one task so the close can never overtake the send.
     */
    fun leave() {
        creds.save(null)
        scope.launch {
            val t = lock.withLock {
                val tr = transport
                transport = null
                tr
            } ?: run {
                finishEnded("left"); return@launch
            }
            val deadline = nowMs() + 3_000
            while (entryAwaitingAck() && nowMs() < deadline) {
                delay(50)
            }
            runCatching { t.send(envelope("LEAVE",
                JsonObject(emptyMap())).text) }
            t.close()
            finishEnded("left")
        }
    }

    /** Entry frame sent but its ACK not yet arrived — the server has not
     *  bound member→link, so LEAVE would be rejected NO_SESSION. A
     *  reconnect in progress (`offlineSince` set) is NOT this case. */
    private fun entryAwaitingAck(): Boolean =
        _status.value == UiStatus.Connecting && entryCont == null &&
            offlineSince == null

    fun setActivity(state: String) {
        if (state !in setOf("active", "suspended", "selecting")) return
        suspended = (state == "suspended")
        scope.launch {
            runCatching {
                sendRequest("SET_ACTIVITY",
                    buildJsonObject { put("state", state) })
            }
        }
    }

    /** Lifecycle wiring: suspended while backgrounded (web pagehide).
     *  The local flag tracks even while unbound — during a reconnect
     *  `bound` is false and skipping the update left the flag stale, so
     *  the RESUME bind re-asserted "suspended" for an app that was already
     *  foregrounded, wedging the session paused (#216). */
    fun onForeground(active: Boolean) {
        suspended = !active
        if (!bound) return
        setActivity(if (active) "active" else "suspended")
    }

    // MARK: - Reader intents (sendIntent port — gated on phase==reading)

    private val readingGated: Boolean
        get() = _lastState.value?.phase == PairPhase.reading

    fun turn(delta: Int, crossChapter: Boolean, fromChapter: String?): Boolean {
        if (!readingGated || (delta != 1 && delta != -1)) return false
        send("TURN", buildJsonObject {
            put("delta", delta); put("crossChapter", crossChapter)
            put("fromChapter",
                fromChapter?.let { JsonPrimitive(it) } ?: JsonNull)
            put("manifestVersion", seenMv) })
        return true
    }

    fun gotoPage(page: PairPageRef): Boolean {
        if (!readingGated) return false
        send("GOTO", buildJsonObject {
            put("page", buildJsonObject {
                put("folder", page.folder); put("name", page.name) })
            put("manifestVersion", seenMv) })
        return true
    }

    fun setOffset(offset: Int): Boolean {
        if (!readingGated || (offset != 0 && offset != 1)) return false
        send("SET_OFFSET", buildJsonObject {
            put("offset", offset); put("manifestVersion", seenMv) })
        return true
    }

    fun swapSides(): Boolean {
        val cur = _lastState.value?.leaderSide
        if (!readingGated || cur == null) return false
        send("SWAP_SIDES", buildJsonObject {
            put("leaderSide",
                if (cur == PairSide.right) "left" else "right")
            put("manifestVersion", seenMv) })
        return true
    }

    /** Leader-only work pick. `page` resolves the initial position; a
     *  hidden-root veto mirrors the web's `_visBlocksFolder` gate. */
    fun openWork(folder: String, page: PairPageRef?, offset: Int? = null): Boolean {
        if (role != PairRole.leader || !paired) return false
        if (visibilityBlocks?.invoke(folder) == true) return false
        if (page == null) return false
        send("OPEN_WORK", buildJsonObject {
            put("folder", folder)
            put("page", buildJsonObject {
                put("folder", page.folder); put("name", page.name) })
            put("manifestVersion", seenMv)
            offset?.let { put("offset", it) }
        })
        return true
    }

    /** Leader picking a work through the shared resolver (openWork intent). */
    suspend fun pickWork(folder: String): Boolean {
        if (role != PairRole.leader || !paired) return false
        if (visibilityBlocks?.invoke(folder) == true) return false
        val page = openWorkResolver?.invoke(folder) ?: return false
        return openWork(folder = folder, page = page)
    }

    /** Fire-and-forget data send (web _dataSend on server-ws == _ctlSend). */
    private fun send(type: String, payload: JsonObject) {
        sendReq(envelope(type, payload))
    }

    private fun sendReq(req: OutRequest) {
        val t = transport ?: return
        scope.launch { runCatching { t.send(req.text) } }
    }

    private fun sendRaw(frame: JsonObject) {
        val t = transport ?: return
        scope.launch { runCatching { t.send(frame.toString()) } }
    }

    // MARK: - Teardown

    /** Synchronous teardown for callers already inside `lock`. */
    private fun finishEndedLocked(reason: String) {
        val wasBound = bound || _member.value != null
        teardownLocked()
        _status.value = UiStatus.Ended(reason)
        _endReason.value = reason
        if (wasBound) creds.save(null)
    }

    /** Outside-lock entry: schedule the end sequence on the store scope. */
    private fun finishEnded(reason: String) {
        scope.launch { lock.withLock { finishEndedLocked(reason) } }
    }

    /** Stop the session locally without sending LEAVE (connection gone). */
    fun disconnect() {
        creds.save(null)
        scope.launch {
            lock.withLock {
                teardownLocked()
                _status.value = UiStatus.Idle
            }
        }
    }
}
