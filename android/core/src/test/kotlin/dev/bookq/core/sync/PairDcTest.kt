package dev.bookq.core.sync

import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
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

/// Scripted RTC fakes — drive SignalingPeer + the PairStore dc upgrade
/// without a platform webrtc stack.
class FakeDc(override val label: String = "sync") : RtcDataChannel {
    override var open = false
    override var onOpen: (() -> Unit)? = null
    override var onMessage: ((String) -> Unit)? = null
    override var onClose: (() -> Unit)? = null
    val sent = mutableListOf<String>()
    var failSend = false
    override fun send(text: String) {
        if (failSend) throw RuntimeException("dc send failed")
        sent.add(text)
    }
    override fun close() { open = false; onClose?.invoke() }
    fun fireOpen() { open = true; onOpen?.invoke() }
    fun fireMessage(text: String) = onMessage?.invoke(text) ?: Unit
}

class FakePc : RtcPeerConnection {
    override var onIceCandidate: ((RtcIce?) -> Unit)? = null
    override var onDataChannel: ((RtcDataChannel) -> Unit)? = null
    val channels = mutableListOf<FakeDc>()
    val ices = mutableListOf<RtcIce>()
    var localSdp: RtcSdp? = null
    var remoteSdp: RtcSdp? = null
    var closed = false
    var offer = RtcSdp("offer", "o-sdp")
    var answer = RtcSdp("answer", "a-sdp")
    override fun createDataChannel(label: String) =
        FakeDc(label).also { channels.add(it) }
    override suspend fun createOffer() = offer
    override suspend fun createAnswer() = answer
    override suspend fun setLocalDescription(s: RtcSdp) { localSdp = s }
    override suspend fun setRemoteDescription(s: RtcSdp) { remoteSdp = s }
    override suspend fun addIceCandidate(c: RtcIce) { ices.add(c) }
    override fun close() { closed = true }
    fun fireChannel(ch: FakeDc) = onDataChannel?.invoke(ch) ?: Unit
    fun fireIce(c: RtcIce) = onIceCandidate?.invoke(c) ?: Unit
}

@OptIn(ExperimentalCoroutinesApi::class)
class PairDcTest {

    private val json = Json { ignoreUnknownKeys = true }

    private fun newStore(
        scope: TestScope, fake: FakeTransport, creds: MemCreds,
    ) = PairStore(
        scope = scope.backgroundScope,
        urlProvider = { "ws://test/ws/reader-sync" },
        tokenProvider = { "tok" },
        transportFactory = { fake },
        creds = creds,
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

    private val leaderBind = buildJsonObject {
        put("sessionId", "s1"); put("memberId", "m1")
        put("role", "leader"); put("resumeToken", "rt1")
        put("code", "123456")
    }
    private val followerBind = buildJsonObject {
        put("sessionId", "s1"); put("memberId", "m2")
        put("role", "follower"); put("resumeToken", "rt2")
    }

    private fun state(
        rev: Int, epoch: Int = 0, mv: Int = 0,
        phase: String = "selecting", dataPlane: String = "server-ws",
    ) = buildJsonObject {
        put("v", 1); put("type", "STATE"); put("sessionId", "s1")
        put("revision", rev); put("workEpoch", epoch)
        put("manifestVersion", mv); put("phase", phase)
        put("offset", 0); put("leaderSide", "right")
        put("dataPlane", dataPlane)
        put("presence", buildJsonObject {
            put("m1", buildJsonObject {
                put("role", "leader"); put("status", "active")
                put("name", "A"); put("ready", false) })
            put("m2", buildJsonObject {
                put("role", "follower"); put("status", "active")
                put("name", "B"); put("ready", false) })
        })
    }

    /** Bind as leader (CREATE) — returns once the ACK landed. */
    private suspend fun TestScope.bindLeader(
        fake: FakeTransport, store: PairStore,
    ) {
        val job = launch { store.create() }
        hello(fake)
        val sent = waitForSent(fake, 1)
        ack(fake, sent[0]["requestId"]!!.jsonPrimitive.content,
            "CREATE", leaderBind)
        job.join()
    }

    /** Bind as follower (JOIN). */
    private suspend fun TestScope.bindFollower(
        fake: FakeTransport, store: PairStore,
    ) {
        val job = launch { store.join("232914") }
        hello(fake)
        val sent = waitForSent(fake, 1)
        ack(fake, sent[0]["requestId"]!!.jsonPrimitive.content,
            "JOIN", followerBind)
        job.join()
    }

    private fun signalsOf(sent: List<JsonObject>) = sent.filter {
        it["type"]?.jsonPrimitive?.content == "SIGNAL" }
    private fun kindOf(f: JsonObject) = (f["payload"] as? JsonObject)
        ?.get("kind")?.jsonPrimitive?.content

    // MARK: - signaling

    @Test fun leaderOffersOncePaired() = runTest {
        val fake = FakeTransport(); val creds = MemCreds()
        val pc = FakePc()
        val store = newStore(this, fake, creds)
        store.rtcFactory = { pc }
        bindLeader(fake, store)
        fake.emit(wire(state(rev = 1)))
        val sent = waitForSent(fake, 2)
        val sig = signalsOf(sent)
        assertEquals(1, sig.size)
        assertEquals("offer", kindOf(sig[0]))
        assertEquals(1, pc.channels.size)
        assertEquals("o-sdp", pc.localSdp?.sdp)
    }

    @Test fun followerWaitsForOffer() = runTest {
        val fake = FakeTransport(); val creds = MemCreds()
        val pc = FakePc()
        val store = newStore(this, fake, creds)
        store.rtcFactory = { pc }
        bindFollower(fake, store)
        fake.emit(wire(state(rev = 1)))
        delay(300)
        // non-initiator: peer created, no channel, no offer sent
        assertTrue(pc.channels.isEmpty())
        assertTrue(signalsOf(decoded(fake.sent())).isEmpty())
    }

    @Test fun offerIsAnsweredAndIceRelayed() = runTest {
        val fake = FakeTransport(); val creds = MemCreds()
        val pc = FakePc()
        val store = newStore(this, fake, creds)
        store.rtcFactory = { pc }
        bindFollower(fake, store)
        fake.emit(wire(state(rev = 1)))
        delay(100)
        fake.emit(wire(buildJsonObject {
            put("v", 1); put("type", "SIGNAL"); put("sessionId", "s1")
            put("from", "leader"); put("kind", "offer")
            put("data", buildJsonObject {
                put("type", "offer"); put("sdp", "peer-sdp") })
        }))
        val sent = waitForSent(fake, 2)
        val sig = signalsOf(sent)
        assertEquals("answer", kindOf(sig.last()))
        assertEquals("offer", pc.remoteSdp?.type)
        assertEquals("a-sdp", pc.localSdp?.sdp)
        // inbound ice → pc.addIceCandidate
        fake.emit(wire(buildJsonObject {
            put("v", 1); put("type", "SIGNAL"); put("sessionId", "s1")
            put("from", "leader"); put("kind", "ice")
            put("data", buildJsonObject {
                put("candidate", "cand:x"); put("sdpMid", "0")
                put("sdpMLineIndex", 0) })
        }))
        for (i in 0..<50) { if (pc.ices.isNotEmpty()) break; delay(10) }
        assertEquals("cand:x", pc.ices[0].candidate)
        // outbound ice → SIGNAL{kind:ice}
        pc.fireIce(RtcIce("cand:y", "0", 0))
        val sent2 = waitForSent(fake, 3)
        assertEquals("ice", kindOf(signalsOf(sent2).last()))
    }

    @Test fun dcOpenReportsTransportStateUp() = runTest {
        val fake = FakeTransport(); val creds = MemCreds()
        val pc = FakePc()
        val store = newStore(this, fake, creds)
        store.rtcFactory = { pc }
        bindLeader(fake, store)
        fake.emit(wire(state(rev = 1)))
        val sent = waitForSent(fake, 2)   // CREATE + SIGNAL offer
        assertNotNull(pc.channels.firstOrNull())
        pc.channels[0].fireOpen()
        val sent2 = waitForSent(fake, 3)
        val ts = sent2[2]
        assertEquals("TRANSPORT_STATE", ts["type"]?.jsonPrimitive?.content)
        assertEquals("up", (ts["payload"] as JsonObject)["state"]
            ?.jsonPrimitive?.content)
    }

    // MARK: - plane switching + leader-sequenced send path

    /** Drive follower into webrtc-dc plane: dc open (report up), then the
     *  server confirms the flip with a TRANSPORT_STATE broadcast. */
    private suspend fun TestScope.followerOnDc(
        fake: FakeTransport, store: PairStore, pc: FakePc,
    ): FakeDc {
        bindFollower(fake, store)
        fake.emit(wire(state(rev = 1)))
        delay(100)
        val dc = FakeDc()
        pc.fireChannel(dc)
        delay(100)
        dc.fireOpen()
        waitForSent(fake, 2)   // JOIN + TRANSPORT_STATE{up}
        fake.emit(wire(state(rev = 2, phase = "reading",
            dataPlane = "webrtc-dc")))
        fake.emit(wire(buildJsonObject {
            put("v", 1); put("type", "TRANSPORT_STATE")
            put("sessionId", "s1"); put("dataPlane", "webrtc-dc")
            put("revision", 2) }))
        for (i in 0..<50) {
            if (store.effPlane.value == "webrtc-dc") break
            delay(10)
        }
        return dc
    }

    @Test fun followerDataOpsRideDc() = runTest {
        val fake = FakeTransport(); val creds = MemCreds()
        val pc = FakePc()
        val store = newStore(this, fake, creds)
        store.rtcFactory = { pc }
        val dc = followerOnDc(fake, store, pc)
        assertEquals("webrtc-dc", store.effPlane.value)
        store.setActivity("selecting")          // ctl — stays on ws
        val wsBefore = fake.sent().size
        assertTrue(store.turn(1, crossChapter = true, fromChapter = null))
        for (i in 0..<50) { if (dc.sent.isNotEmpty()) break; delay(10) }
        assertEquals(1, dc.sent.size)
        val f = decoded(dc.sent)[0]
        assertEquals("TURN", f["type"]?.jsonPrimitive?.content)
        // same frame shape as ws: request envelope with payload
        assertNotNull(f["requestId"])
        assertNotNull(f["payload"])
        // ws only got the ctl SET_ACTIVITY meanwhile
        val wsSent = decoded(fake.sent()).drop(wsBefore)
        assertTrue(wsSent.all {
            it["type"]?.jsonPrimitive?.content != "TURN" })
    }

    @Test fun leaderRelaysDcFrameWithOrigin() = runTest {
        val fake = FakeTransport(); val creds = MemCreds()
        val pc = FakePc()
        val store = newStore(this, fake, creds)
        store.rtcFactory = { pc }
        bindLeader(fake, store)
        fake.emit(wire(state(rev = 1)))
        waitForSent(fake, 2)
        fake.emit(wire(state(rev = 2, dataPlane = "webrtc-dc")))
        fake.emit(wire(buildJsonObject {
            put("v", 1); put("type", "TRANSPORT_STATE")
            put("sessionId", "s1"); put("dataPlane", "webrtc-dc")
            put("revision", 2) }))
        val dc = pc.channels[0]; dc.fireOpen()
        waitForSent(fake, 3)               // TRANSPORT_STATE{up}
        for (i in 0..<50) {
            if (store.effPlane.value == "webrtc-dc") break
            delay(10)
        }
        dc.fireMessage(wire(buildJsonObject {
            put("v", 1); put("requestId", "r-f1"); put("type", "TURN")
            put("sessionId", "s1"); put("workEpoch", 0)
            put("baseRevision", 2)
            put("payload", buildJsonObject { put("delta", 1) })
        }))
        val sent = waitForSent(fake, 4)
        val fwd = sent[3]
        assertEquals("TURN", fwd["type"]?.jsonPrimitive?.content)
        assertEquals("r-f1", fwd["requestId"]?.jsonPrimitive?.content)
        assertEquals("follower", (fwd["payload"] as JsonObject)["origin"]
            ?.jsonPrimitive?.content)
    }

    @Test fun dcDropReportsDownAndFallsBack() = runTest {
        val fake = FakeTransport(); val creds = MemCreds()
        val pc = FakePc()
        val store = newStore(this, fake, creds)
        store.rtcFactory = { pc }
        val dc = followerOnDc(fake, store, pc)
        assertEquals("webrtc-dc", store.effPlane.value)
        dc.close()
        val sent = waitForSent(fake, 3)
        assertEquals("down", (sent[2]["payload"] as JsonObject)["state"]
            ?.jsonPrimitive?.content)
        for (i in 0..<50) {
            if (store.effPlane.value == "server-ws") break
            delay(10)
        }
        assertEquals("server-ws", store.effPlane.value)
    }

    @Test fun dcSendFailureFallsBackToWs() = runTest {
        val fake = FakeTransport(); val creds = MemCreds()
        val pc = FakePc()
        val store = newStore(this, fake, creds)
        store.rtcFactory = { pc }
        val dc = followerOnDc(fake, store, pc)
        dc.failSend = true
        val before = fake.sent().size
        assertTrue(store.gotoPage(PairPageRef("f", "p4.png")))
        val sent = waitForSent(fake, before + 1)
        assertEquals("GOTO", sent.last()["type"]?.jsonPrimitive?.content)
    }

    @Test fun restartDataChannelRespins() = runTest {
        val fake = FakeTransport(); val creds = MemCreds()
        val pcs = mutableListOf<FakePc>()
        val store = newStore(this, fake, creds)
        store.rtcFactory = { FakePc().also { pcs.add(it) } }
        bindLeader(fake, store)
        fake.emit(wire(state(rev = 1)))
        waitForSent(fake, 2)
        assertEquals(1, pcs.size)
        store.restartDataChannel()
        val sent = waitForSent(fake, 3)
        assertTrue(pcs[0].closed)
        assertEquals(2, pcs.size)
        assertEquals("offer", kindOf(signalsOf(sent).last()))
    }

    // MARK: - UI_SYNC

    @Test fun uiSyncSendsValidatedPayload() = runTest {
        val fake = FakeTransport(); val creds = MemCreds()
        val store = newStore(this, fake, creds)
        bindLeader(fake, store)
        fake.emit(wire(state(rev = 1)))
        delay(100)
        assertFalse(store.sendUiSync("p", -1.0, 800.0, 2000.0, "tile"))
        assertFalse(store.sendUiSync("p", 10.0, 0.0, 2000.0, "tile"))
        assertFalse(store.sendUiSync("p", 10.0, 200001.0, 2e8, "tile"))
        // invalid mode is dropped, not rejected (web sendUiSync parity)
        assertTrue(store.sendUiSync("p", 10.0, 800.0, 2e8, "bogus"))
        val sent0 = waitForSent(fake, 2)
        val p0 = sent0[1]["payload"] as JsonObject
        assertNull(p0["mode"])
        assertNull(p0["scrollH"])     // 2e8 > 1e8 — dropped, not rejected
        assertTrue(store.sendUiSync("folder/x", 250.0, 800.0, 2000.0,
            "tile"))
        val sent = waitForSent(fake, 3)
        val u = sent[2]
        assertEquals("UI_SYNC", u["type"]?.jsonPrimitive?.content)
        val p = u["payload"] as JsonObject
        assertEquals("browse", p["view"]?.jsonPrimitive?.content)
        assertEquals("folder/x", p["path"]?.jsonPrimitive?.content)
        assertEquals(250.0, p["top"]?.jsonPrimitive?.content?.toDouble())
        assertEquals(2000.0, p["scrollH"]?.jsonPrimitive?.content
            ?.toDouble())
    }

    @Test fun uiSyncInboundEmits() = runTest {
        val fake = FakeTransport(); val creds = MemCreds()
        val store = newStore(this, fake, creds)
        val got = mutableListOf<UiSync.Frame>()
        val job = launch { store.uiSync.collect { got.add(it) } }
        bindLeader(fake, store)
        fake.emit(wire(buildJsonObject {
            put("v", 1); put("type", "UI_SYNC"); put("sessionId", "s1")
            put("from", "follower")
            put("payload", buildJsonObject {
                put("view", "browse"); put("path", "f/a")
                put("top", 120.0); put("viewH", 800.0)
                put("scrollH", 4000.0); put("mode", "tile")
            })
        }))
        for (i in 0..<50) { if (got.isNotEmpty()) break; delay(10) }
        job.cancel()
        assertEquals(1, got.size)
        assertEquals("f/a", got[0].path)
        assertEquals(4000.0, got[0].scrollH!!, 0.01)
        assertEquals("follower", got[0].from)
    }
}
