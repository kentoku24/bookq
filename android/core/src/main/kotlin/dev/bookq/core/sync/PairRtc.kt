package dev.bookq.core.sync

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.intOrNull

/* WebRTC signaling contracts — Kotlin port of viewer/pairTransport.js
 * `createSignalingPeer` (#300). The platform peer connection lives in
 * :data (stream-webrtc-android); :core holds only the signaling logic so
 * tests can drive it with fakes. ICE is host-candidate only: no
 * STUN/TURN is ever configured (§3.1 LAN assumption). */

data class RtcSdp(val type: String, val sdp: String)

data class RtcIce(
    val candidate: String,
    val sdpMid: String? = null,
    val sdpMLineIndex: Int? = null,
) {
    fun toJson(): JsonObject = buildJsonObject {
        put("candidate", JsonPrimitive(candidate))
        put("sdpMid", sdpMid?.let { JsonPrimitive(it) } ?: JsonNull)
        put("sdpMLineIndex", sdpMLineIndex?.let { JsonPrimitive(it) }
            ?: JsonNull)
    }

    companion object {
        fun fromJson(el: JsonElement?): RtcIce? {
            val o = el as? JsonObject ?: return null
            val c = (o["candidate"] as? JsonPrimitive)?.content ?: return null
            return RtcIce(
                candidate = c,
                sdpMid = (o["sdpMid"] as? JsonPrimitive)?.content,
                sdpMLineIndex = (o["sdpMLineIndex"] as? JsonPrimitive)
                    ?.intOrNull)
        }
    }
}

/** DataChannel contract — string frames only (the pair wire is JSON). */
interface RtcDataChannel {
    val label: String
    val open: Boolean
    var onOpen: (() -> Unit)?
    var onMessage: ((String) -> Unit)?
    var onClose: (() -> Unit)?
    fun send(text: String)
    fun close()
}

/** PeerConnection contract — subset of RTCPeerConnection used by the
 *  signaling glue. SDP frames serialize as {type, sdp}; ICE as
 *  {candidate, sdpMid, sdpMLineIndex}, matching the browser toJSON. */
interface RtcPeerConnection {
    var onIceCandidate: ((RtcIce?) -> Unit)?
    var onDataChannel: ((RtcDataChannel) -> Unit)?
    fun createDataChannel(label: String): RtcDataChannel
    suspend fun createOffer(): RtcSdp
    suspend fun createAnswer(): RtcSdp
    suspend fun setLocalDescription(s: RtcSdp)
    suspend fun setRemoteDescription(s: RtcSdp)
    suspend fun addIceCandidate(c: RtcIce)
    fun close()
}

private fun RtcSdp.toJson(): JsonObject = buildJsonObject {
    put("type", JsonPrimitive(type)); put("sdp", JsonPrimitive(sdp))
}

private fun rtcSdpFromJson(el: JsonElement?): RtcSdp? {
    val o = el as? JsonObject ?: return null
    val t = (o["type"] as? JsonPrimitive)?.content ?: return null
    val s = (o["sdp"] as? JsonPrimitive)?.content ?: return null
    return RtcSdp(type = t, sdp = s)
}

/** Port of pairTransport.js createSignalingPeer. `send` emits the
 *  {kind:'offer'|'answer'|'ice', data} SIGNAL payload on the control
 *  channel; `onChannel` fires once per side with the 'sync' channel. */
class SignalingPeer(
    private val pc: RtcPeerConnection,
    private val scope: CoroutineScope,
    private val initiator: Boolean,
    private val send: (kind: String, data: JsonElement) -> Unit,
    private val onChannel: (RtcDataChannel) -> Unit,
) {
    var dc: RtcDataChannel? = null
        private set

    init {
        pc.onIceCandidate = { c -> if (c != null) send("ice", c.toJson()) }
        pc.onDataChannel = { ch -> dc = ch; onChannel(ch) }
    }

    /** Initiator side: own the 'sync' channel and produce the offer. */
    fun start() {
        if (!initiator) return
        val ch = pc.createDataChannel("sync")
        dc = ch
        onChannel(ch)
        scope.launch {
            runCatching {
                val offer = pc.createOffer()
                pc.setLocalDescription(offer)
                send("offer", offer.toJson())
            }   // signaling failure -> caller falls back
        }
    }

    fun applySignal(kind: String, data: JsonElement?) {
        when (kind) {
            "offer" -> scope.launch {
                runCatching {
                    val sdp = rtcSdpFromJson(data) ?: return@runCatching
                    pc.setRemoteDescription(sdp)
                    val ans = pc.createAnswer()
                    pc.setLocalDescription(ans)
                    send("answer", ans.toJson())
                }
            }
            "answer" -> scope.launch {
                runCatching {
                    val sdp = rtcSdpFromJson(data) ?: return@runCatching
                    pc.setRemoteDescription(sdp)
                }
            }
            "ice" -> scope.launch {
                runCatching {
                    val c = RtcIce.fromJson(data) ?: return@runCatching
                    pc.addIceCandidate(c)
                }
            }
        }
    }

    fun close() = runCatching { pc.close() }
}
