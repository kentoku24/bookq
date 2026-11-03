package dev.bookq.data

import android.content.Context
import dev.bookq.core.sync.RtcDataChannel
import dev.bookq.core.sync.RtcIce
import dev.bookq.core.sync.RtcPeerConnection
import dev.bookq.core.sync.RtcSdp
import java.nio.ByteBuffer
import java.nio.charset.StandardCharsets
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import kotlinx.coroutines.suspendCancellableCoroutine
import org.webrtc.DataChannel
import org.webrtc.IceCandidate
import org.webrtc.MediaConstraints
import org.webrtc.PeerConnection
import org.webrtc.PeerConnectionFactory
import org.webrtc.SdpObserver
import org.webrtc.SessionDescription

/* Android WebRTC adapter — stream-webrtc-android supplies the org.webrtc
 * stack (#300). Host candidates only: RTCConfiguration is built with an
 * empty iceServers list, so no STUN/TURN is ever configured (§3.1 LAN
 * assumption, pairTransport.js parity). */

class PairRtc(context: Context) {

    private val factory: PeerConnectionFactory

    init {
        PeerConnectionFactory.initialize(
            PeerConnectionFactory.InitializationOptions
                .builder(context.applicationContext)
                .createInitializationOptions())
        factory = PeerConnectionFactory.builder()
            .createPeerConnectionFactory()
    }

    /** Fresh PeerConnection implementing the core signaling contract. */
    fun peerConnection(): RtcPeerConnection =
        AndroidPeerConnection(factory)

    fun dispose() {
        runCatching { factory.dispose() }
    }
}

private class AndroidDataChannel(
    private val dc: DataChannel,
) : RtcDataChannel {
    override val label: String get() = dc.label()
    override val open: Boolean
        get() = dc.state() == DataChannel.State.OPEN
    override var onOpen: (() -> Unit)? = null
    override var onMessage: ((String) -> Unit)? = null
    override var onClose: (() -> Unit)? = null

    init {
        dc.registerObserver(object : DataChannel.Observer {
            override fun onBufferedAmountChange(previousAmount: Long) {}
            override fun onStateChange() {
                when (dc.state()) {
                    DataChannel.State.OPEN -> onOpen?.invoke()
                    DataChannel.State.CLOSED -> onClose?.invoke()
                    else -> Unit
                }
            }
            override fun onMessage(buffer: DataChannel.Buffer) {
                if (buffer.binary) return
                val bytes = ByteArray(buffer.data.remaining())
                buffer.data.get(bytes)
                onMessage?.invoke(
                    String(bytes, StandardCharsets.UTF_8))
            }
        })
    }

    override fun send(text: String) {
        val buf = DataChannel.Buffer(
            ByteBuffer.wrap(text.toByteArray(StandardCharsets.UTF_8)),
            /* binary = */ false)
        if (!dc.send(buf)) throw RuntimeException("dc.send() returned false")
    }

    override fun close() {
        runCatching { dc.close() }
    }
}

private class AndroidPeerConnection(
    factory: PeerConnectionFactory,
) : RtcPeerConnection {

    override var onIceCandidate: ((RtcIce?) -> Unit)? = null
    override var onDataChannel: ((RtcDataChannel) -> Unit)? = null

    private val pc: PeerConnection = factory.createPeerConnection(
        PeerConnection.RTCConfiguration(emptyList()).apply {
            // host-candidate only (no STUN/TURN); gather on all adapters
            continualGatheringPolicy =
                PeerConnection.ContinualGatheringPolicy.GATHER_ONCE
        },
        object : PeerConnection.Observer {
            override fun onIceCandidate(candidate: IceCandidate) {
                onIceCandidate?.invoke(RtcIce(
                    candidate = candidate.sdp,
                    sdpMid = candidate.sdpMid,
                    sdpMLineIndex = candidate.sdpMLineIndex))
            }
            override fun onDataChannel(dc: DataChannel) {
                onDataChannel?.invoke(AndroidDataChannel(dc))
            }
            override fun onSignalingChange(s: PeerConnection.SignalingState) {}
            override fun onIceConnectionChange(
                s: PeerConnection.IceConnectionState) {}
            override fun onStandardizedIceConnectionChange(
                s: PeerConnection.IceConnectionState) {}
            override fun onConnectionChange(
                s: PeerConnection.PeerConnectionState) {}
            override fun onIceConnectionReceivingChange(receiving: Boolean) {}
            override fun onIceGatheringChange(
                s: PeerConnection.IceGatheringState) {}
            override fun onIceCandidatesRemoved(c: Array<out IceCandidate>) {}
            override fun onAddStream(s: org.webrtc.MediaStream) {}
            override fun onRemoveStream(s: org.webrtc.MediaStream) {}
            override fun onRenegotiationNeeded() {}
            override fun onAddTrack(
                r: org.webrtc.RtpReceiver,
                s: Array<out org.webrtc.MediaStream>) {}
            override fun onTrack(t: org.webrtc.RtpTransceiver) {}
        }) ?: throw RuntimeException("createPeerConnection returned null")

    override fun createDataChannel(label: String): RtcDataChannel =
        AndroidDataChannel(pc.createDataChannel(label, DataChannel.Init()))

    private suspend fun sdpOp(
        block: (SdpObserver) -> Unit,
    ): SessionDescription = suspendCancellableCoroutine { cont ->
        block(object : SdpObserver {
            override fun onCreateSuccess(s: SessionDescription) =
                cont.resume(s)
            override fun onSetSuccess() {}
            override fun onCreateFailure(error: String?) =
                cont.resumeWithException(
                    RuntimeException("sdp create: $error"))
            override fun onSetFailure(error: String?) =
                cont.resumeWithException(
                    RuntimeException("sdp set: $error"))
        })
    }

    private suspend fun sdpSet(
        block: (SdpObserver) -> Unit,
    ): Unit = suspendCancellableCoroutine { cont ->
        block(object : SdpObserver {
            override fun onCreateSuccess(s: SessionDescription) {}
            override fun onSetSuccess() = cont.resume(Unit)
            override fun onCreateFailure(error: String?) {}
            override fun onSetFailure(error: String?) =
                cont.resumeWithException(
                    RuntimeException("sdp set: $error"))
        })
    }

    override suspend fun createOffer(): RtcSdp {
        val s = sdpOp { pc.createOffer(it, MediaConstraints()) }
        return RtcSdp(type = s.type.canonicalForm(), sdp = s.description)
    }

    override suspend fun createAnswer(): RtcSdp {
        val s = sdpOp { pc.createAnswer(it, MediaConstraints()) }
        return RtcSdp(type = s.type.canonicalForm(), sdp = s.description)
    }

    override suspend fun setLocalDescription(s: RtcSdp) = sdpSet {
        pc.setLocalDescription(it,
            SessionDescription(sdpType(s.type), s.sdp))
    }

    override suspend fun setRemoteDescription(s: RtcSdp) = sdpSet {
        pc.setRemoteDescription(it,
            SessionDescription(sdpType(s.type), s.sdp))
    }

    private fun sdpType(t: String) =
        SessionDescription.Type.fromCanonicalForm(t)

    override suspend fun addIceCandidate(c: RtcIce) {
        pc.addIceCandidate(IceCandidate(
            c.sdpMid, c.sdpMLineIndex ?: 0, c.candidate))
    }

    override fun close() {
        runCatching { pc.close(); pc.dispose() }
    }
}
