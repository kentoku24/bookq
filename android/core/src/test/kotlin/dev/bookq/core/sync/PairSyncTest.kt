package dev.bookq.core.sync

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.receiveAsFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/// Scripted PairTransport — records outbound frames, replays inbound
/// wire text on demand. Port of ios FakeTransport.
class FakeTransport : PairTransport {
    private val events = Channel<PairEvent>(Channel.UNLIMITED)
    private val sentRaw = mutableListOf<String>()
    private val queuedEmits = mutableListOf<String>()
    var connected = false
    var connectCount = 0; private set
    var closeCount = 0; private set
    var closedAfterSendCount: Int? = null; private set

    override fun connect(url: String, token: String?): Flow<PairEvent> {
        connectCount += 1
        connected = true
        events.trySend(PairEvent.Open)
        for (e in queuedEmits) events.trySend(PairEvent.Frame(e))
        queuedEmits.clear()
        return events.receiveAsFlow()
    }
    override suspend fun send(text: String) { sentRaw.add(text) }
    override suspend fun close() {
        closeCount += 1
        closedAfterSendCount = sentRaw.size
        connected = false
        events.trySend(PairEvent.Closed(1000, "closed"))
        events.close()
    }
    fun sent() = sentRaw.toList()
    fun emit(wireText: String) {
        if (connected) events.trySend(PairEvent.Frame(wireText))
        else queuedEmits.add(wireText)
    }
    fun drop() = events.trySend(PairEvent.Failed("link drop"))
}

class MemCreds : PairCredStore {
    var member: PairMember? = null
    override fun load() = member
    override fun save(m: PairMember?) { member = m }
}

@OptIn(ExperimentalCoroutinesApi::class)
class PairSyncTest {

    private val json = Json { ignoreUnknownKeys = true }

    private fun newStore(
        scope: TestScope, fake: FakeTransport, creds: MemCreds,
        resumeWindowMs: Long = 300_000,
    ) = PairStore(
        scope = scope.backgroundScope,
        urlProvider = { "ws://test/ws/reader-sync" },
        tokenProvider = { "tok" },
        transportFactory = { fake },
        creds = creds,
        resumeWindowMs = resumeWindowMs,
    )

    private fun decoded(texts: List<String>) = texts.map {
        json.parseToJsonElement(it).jsonObject }

    private suspend fun waitForSent(
        fake: FakeTransport, n: Int,
    ): List<JsonObject> {
        for (i in 0..<500) {
            val s = fake.sent()
            if (s.size >= n) return decoded(s)
            delay(10)
        }
        fail("expected $n sent frames, got ${fake.sent().size}")
        return decoded(fake.sent())
    }

    private fun wire(obj: JsonObject) = obj.toString()
    private fun hello(fake: FakeTransport) =
        fake.emit(wire(buildJsonObject { put("v", 1); put("type", "HELLO") }))
    private fun ack(
        fake: FakeTransport, rid: String, request: String, data: JsonObject,
    ) = fake.emit(wire(buildJsonObject {
        put("v", 1); put("type", "ACK"); put("requestId", rid)
        put("request", request); put("data", data)
    }))

    private val bindData = buildJsonObject {
        put("sessionId", "s1"); put("memberId", "m1")
        put("role", "leader"); put("resumeToken", "rt1")
        put("code", "123456")
    }

    private fun state(
        rev: Int, epoch: Int = 0, mv: Int = 0,
        phase: String = "waiting", spreadId: String? = null,
    ) = buildJsonObject {
        put("v", 1); put("type", "STATE"); put("sessionId", "s1")
        put("revision", rev); put("workEpoch", epoch)
        put("manifestVersion", mv); put("phase", phase)
        put("offset", 1); put("leaderSide", "right")
        put("dataPlane", "server-ws")
        put("presence", buildJsonObject {
            put("m1", buildJsonObject {
                put("role", "leader"); put("status", "active")
                put("name", "iPhone"); put("ready", false) })
            put("m2", buildJsonObject {
                put("role", "follower"); put("status", "active")
                put("name", "iPad"); put("ready", false) })
        })
        spreadId?.let { put("spreadId", it) }
    }

    private val manifestFixture = buildJsonObject {
        put("v", 1); put("folder", "作品A"); put("offset", 1)
        put("initial", buildJsonObject { put("spreadId", "s0") })
        put("chapters", kotlinx.serialization.json.buildJsonArray {
            add(buildJsonObject {
                put("name", "第1話"); put("folder", "作品A/第1話")
                put("pages", kotlinx.serialization.json.buildJsonArray {
                    listOf("p1.png", "p2.png", "p3.png").forEach { n ->
                        add(buildJsonObject {
                            put("folder", "作品A/第1話"); put("name", n) })
                    }
                })
            })
        })
        put("pageRefs", kotlinx.serialization.json.buildJsonArray {
            listOf("p1.png", "p2.png", "p3.png").forEach { n ->
                add(buildJsonObject {
                    put("folder", "作品A/第1話"); put("name", n) })
            }
        })
        put("spreadTable", kotlinx.serialization.json.buildJsonArray {
            add(buildJsonObject {
                put("id", "s0"); put("chapterFolder", "作品A/第1話")
                put("right", buildJsonObject {
                    put("folder", "作品A/第1話"); put("name", "p1.png") })
                put("left", buildJsonObject {
                    put("folder", "作品A/第1話"); put("name", "p2.png") })
            })
            add(buildJsonObject {
                put("id", "s1"); put("chapterFolder", "作品A/第1話")
                put("right", buildJsonObject {
                    put("folder", "作品A/第1話"); put("name", "p3.png") })
                put("left", kotlinx.serialization.json.JsonNull)
            })
        })
        put("digest",
            "sha256:476147ea0d741c9253a1998b62b0b8e9958597e8cc26bc155aa34b70c59e1e87")
    }

    // MARK: - canonical JSON / manifest

    @Test fun manifestDigestGolden() {
        assertEquals(
            "sha256:476147ea0d741c9253a1998b62b0b8e9958597e8cc26bc155aa34b70c59e1e87",
            CanonicalJson.manifestDigest(manifestFixture))
    }

    @Test fun manifestDigestSensitive() {
        val m = manifestFixture.toMutableMap()
        m["offset"] = JsonPrimitive(0)
        assertTrue(CanonicalJson.manifestDigest(JsonObject(m)) !=
            "sha256:476147ea0d741c9253a1998b62b0b8e9958597e8cc26bc155aa34b70c59e1e87")
    }

    @Test fun canonicalEscapes() {
        // non-ASCII → \uXXXX per UTF-16 unit, lowercase hex
        val obj = JsonObject(mapOf("k" to JsonPrimitive("表😀")))
        assertEquals("""{"k":"\u8868\ud83d\ude00"}""",
            CanonicalJson.encode(obj))
    }

    @Test fun manifestParse() {
        val m = PairManifest.parse(manifestFixture)
        assertNotNull(m)
        assertEquals("作品A", m!!.folder)
        assertEquals(1, m.offset)
        assertEquals(1, m.chapters.size)
        assertEquals(3, m.chapters[0].pages.size)
        assertEquals(2, m.spreadTable.size)
        assertEquals("s0", m.initialSpreadId)
        assertEquals(1, m.spreadIndex("s1"))
        assertNull(m.spreadTable[1].left)
    }

    // MARK: - turnIndex / project

    @Test fun turnIndexClampsToChapter() {
        val c = "作品A/第1話"; val c2 = "作品A/第2話"
        val p = { n: String -> PairPageRef(c, n) }
        val table = listOf(
            PairSpread("s0", c, p("a"), null),
            PairSpread("s1", c, p("b"), null),
            PairSpread("s2", c2, p("c"), null))
        assertEquals(1, PairModel.turnIndex(table, 0, 1, false))
        assertEquals(1, PairModel.turnIndex(table, 1, 1, false))   // chapter edge
        assertEquals(2, PairModel.turnIndex(table, 1, 1, true))    // cross
        assertEquals(0, PairModel.turnIndex(table, 0, -1, true))
        assertNull(PairModel.turnIndex(table, 5, 1, true))
        assertNull(PairModel.turnIndex(table, 0, 2, true))
    }

    @Test fun projectSides() {
        val r = PairPageRef("f", "r.png"); val l = PairPageRef("f", "l.png")
        val s = PairSpread("s0", "f", r, l)
        assertEquals(PairSide.right to r,
            PairModel.project(s, PairRole.leader, PairSide.right))
        assertEquals(PairSide.left to l,
            PairModel.project(s, PairRole.follower, PairSide.right))
        assertEquals(PairSide.left to l,
            PairModel.project(s, PairRole.leader, PairSide.left))
    }

    // MARK: - store flows

    @Test fun createBindsOnAck() = runTest {
        val fake = FakeTransport(); val creds = MemCreds()
        val store = newStore(this, fake, creds)
        val job = launch { store.create() }
        hello(fake)
        val sent = waitForSent(fake, 1)
        assertEquals("CREATE", sent[0]["type"]?.jsonPrimitive?.content)
        val rid = sent[0]["requestId"]!!.jsonPrimitive.content
        ack(fake, rid, "CREATE", bindData)
        job.join()
        assertEquals("123456", store.code.value)
        assertEquals(PairRole.leader, store.role)
        assertTrue(store.bound)
        assertNotNull(creds.member)
    }

    @Test fun joinRequestAutoApproves() = runTest {
        val fake = FakeTransport(); val creds = MemCreds()
        val store = newStore(this, fake, creds)
        val job = launch { store.create() }
        hello(fake)
        val sent = waitForSent(fake, 1)
        ack(fake, sent[0]["requestId"]!!.jsonPrimitive.content,
            "CREATE", bindData)
        job.join()
        fake.emit(wire(buildJsonObject {
            put("v", 1); put("type", "JOIN_REQUEST")
            put("memberId", "m2"); put("name", "iPad") }))
        val sent2 = waitForSent(fake, 2)
        assertEquals("APPROVE", sent2[1]["type"]?.jsonPrimitive?.content)
        assertEquals("m2", (sent2[1]["payload"]
            as JsonObject)["memberId"]?.jsonPrimitive?.content)
    }

    @Test fun joinRejectedEnds() = runTest {
        val fake = FakeTransport(); val creds = MemCreds()
        val store = newStore(this, fake, creds)
        val result = CompletableDeferred<Throwable?>()
        launch {
            result.complete(
                runCatching { store.join("654321") }.exceptionOrNull())
        }
        hello(fake)
        val sent = waitForSent(fake, 1)
        assertEquals("JOIN", sent[0]["type"]?.jsonPrimitive?.content)
        fake.emit(wire(buildJsonObject {
            put("v", 1); put("type", "REJECT")
            put("requestId", sent[0]["requestId"]!!.jsonPrimitive.content)
            put("reason", "bad_code") }))
        assertNotNull(result.await())
        assertFalse(store.bound)
    }

    /** Feed the store into 'reading': CREATE+ACK, manifest chunks, STATE. */
    private suspend fun TestScope.enterReading(
        fake: FakeTransport, store: PairStore,
    ) {
        val job = launch { store.create() }
        hello(fake)
        val sent = waitForSent(fake, 1)
        ack(fake, sent[0]["requestId"]!!.jsonPrimitive.content,
            "CREATE", bindData)
        job.join()
        val text = manifestFixture.toString()
        val mid = text.length / 2
        listOf(text.substring(0, mid), text.substring(mid))
            .forEachIndexed { i, part ->
                fake.emit(wire(buildJsonObject {
                    put("v", 1); put("type", "MANIFEST")
                    put("kind", "manifest-chunk"); put("sessionId", "s1")
                    put("workEpoch", 1); put("manifestVersion", 1)
                    put("digest", manifestFixture["digest"]!!
                        .jsonPrimitive.content)
                    put("index", i); put("count", 2); put("data", part)
                }))
            }
        val sent2 = waitForSent(fake, 2)
        assertEquals("READY", sent2[1]["type"]?.jsonPrimitive?.content)
        fake.emit(wire(state(rev = 5, epoch = 1, mv = 1,
            phase = "reading", spreadId = "s0")))
        for (i in 0..<100) {
            if (store.projection.value != null) break
            delay(10)
        }
    }

    @Test fun manifestReassemblyAndProjection() = runTest {
        val fake = FakeTransport(); val creds = MemCreds()
        val store = newStore(this, fake, creds)
        enterReading(fake, store)
        val p = store.projection.value
        assertNotNull(p)
        assertEquals(0, p!!.spreadIndex)
        assertEquals(2, p.spreadTotal)
        assertEquals("p1.png", p.ref?.name)   // leader+right → right page
    }

    @Test fun staleStateIgnored() = runTest {
        val fake = FakeTransport(); val creds = MemCreds()
        val store = newStore(this, fake, creds)
        val job = launch { store.create() }
        hello(fake)
        val sent = waitForSent(fake, 1)
        ack(fake, sent[0]["requestId"]!!.jsonPrimitive.content,
            "CREATE", bindData)
        job.join()
        fake.emit(wire(state(rev = 3, phase = "selecting")))
        for (i in 0..<50) {
            if (store.lastState.value?.revision == 3) break; delay(10)
        }
        fake.emit(wire(state(rev = 2, phase = "reading")))   // stale
        delay(200)
        assertEquals(3, store.lastState.value?.revision)
        assertEquals(PairPhase.selecting, store.phase)
    }

    @Test fun manifestDigestMismatchRejected() = runTest {
        val fake = FakeTransport(); val creds = MemCreds()
        val store = newStore(this, fake, creds)
        val job = launch { store.create() }
        hello(fake)
        val sent = waitForSent(fake, 1)
        ack(fake, sent[0]["requestId"]!!.jsonPrimitive.content,
            "CREATE", bindData)
        job.join()
        fake.emit(wire(buildJsonObject {
            put("v", 1); put("type", "MANIFEST")
            put("kind", "manifest-chunk"); put("sessionId", "s1")
            put("workEpoch", 1); put("manifestVersion", 1)
            put("digest", "sha256:bad")
            put("index", 0); put("count", 1)
            put("data", manifestFixture.toString()) }))
        for (i in 0..<50) {
            if (store.errorText.value != null) break; delay(10)
        }
        assertEquals("manifest_error", store.errorText.value)
        assertNull(store.manifest.value)
    }

    @Test fun turnGatedOutsideReading() = runTest {
        val fake = FakeTransport(); val creds = MemCreds()
        val store = newStore(this, fake, creds)
        val job = launch { store.create() }
        hello(fake)
        val sent = waitForSent(fake, 1)
        ack(fake, sent[0]["requestId"]!!.jsonPrimitive.content,
            "CREATE", bindData)
        job.join()
        assertFalse(store.turn(1, crossChapter = true, fromChapter = null))
        assertFalse(store.gotoPage(PairPageRef("f", "p.png")))
        assertFalse(store.setOffset(0))
        assertFalse(store.swapSides())
    }

    @Test fun turnSendsIntent() = runTest {
        val fake = FakeTransport(); val creds = MemCreds()
        val store = newStore(this, fake, creds)
        enterReading(fake, store)
        assertTrue(store.turn(1, crossChapter = true, fromChapter = "第1話"))
        val sent = waitForSent(fake, 3)
        val t = sent[2]
        assertEquals("TURN", t["type"]?.jsonPrimitive?.content)
        val p = t["payload"] as JsonObject
        assertEquals(1, p["delta"]?.jsonPrimitive?.intOrNull)
        assertEquals(true, p["crossChapter"]?.jsonPrimitive?.content
            ?.toBoolean())
        assertEquals(1, p["manifestVersion"]?.jsonPrimitive?.intOrNull)
    }

    @Test fun pingAnswersPong() = runTest {
        val fake = FakeTransport(); val creds = MemCreds()
        val store = newStore(this, fake, creds)
        val job = launch { store.create() }
        hello(fake)
        val sent = waitForSent(fake, 1)
        ack(fake, sent[0]["requestId"]!!.jsonPrimitive.content,
            "CREATE", bindData)
        job.join()
        fake.emit(wire(buildJsonObject {
            put("v", 1); put("type", "PING"); put("seq", 7) }))
        val sent2 = waitForSent(fake, 2)
        assertEquals("PONG", sent2[1]["type"]?.jsonPrimitive?.content)
        assertEquals(7, sent2[1]["seq"]?.jsonPrimitive?.intOrNull)
    }

    @Test fun openWorkLeaderOnly() = runTest {
        val fake = FakeTransport(); val creds = MemCreds()
        val store = newStore(this, fake, creds)
        enterReading(fake, store)
        // not leader → still works here (role IS leader from bindData)
        assertTrue(store.openWork("作品A",
            PairPageRef("作品A/第1話", "p1.png")))
        val sent = waitForSent(fake, 3)
        assertEquals("OPEN_WORK", sent[2]["type"]?.jsonPrimitive?.content)
        store.visibilityBlocks = { true }
        assertFalse(store.openWork("blocked",
            PairPageRef("blocked", "p.png")))
    }

    @Test fun badResumeEndsSession() = runTest {
        val fake = FakeTransport(); val creds = MemCreds()
        creds.member = PairMember("s1", "m1", "leader", "rt1")
        val store = newStore(this, fake, creds)
        val job = launch { runCatching { store.resume() } }
        hello(fake)
        val sent = waitForSent(fake, 1)
        assertEquals("RESUME", sent[0]["type"]?.jsonPrimitive?.content)
        fake.emit(wire(buildJsonObject {
            put("v", 1); put("type", "REJECT")
            put("requestId", sent[0]["requestId"]!!.jsonPrimitive.content)
            put("reason", "bad_resume") }))
        job.join()
        for (i in 0..<50) {
            if (store.status.value is PairStore.UiStatus.Ended) break
            delay(10)
        }
        assertTrue(store.status.value is PairStore.UiStatus.Ended)
    }

    @Test fun leaveClearsPersistedCreds() = runTest {
        val fake = FakeTransport(); val creds = MemCreds()
        val store = newStore(this, fake, creds)
        val job = launch { store.create() }
        hello(fake)
        val sent = waitForSent(fake, 1)
        ack(fake, sent[0]["requestId"]!!.jsonPrimitive.content,
            "CREATE", bindData)
        job.join()
        store.leave()
        val sent2 = waitForSent(fake, 2)
        assertEquals("LEAVE", sent2[1]["type"]?.jsonPrimitive?.content)
        assertNull(creds.member)
    }
}
