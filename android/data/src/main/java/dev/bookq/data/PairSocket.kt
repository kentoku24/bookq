package dev.bookq.data

import dev.bookq.core.sync.PairEvent
import dev.bookq.core.sync.PairException
import dev.bookq.core.sync.PairTransport
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener

/**
 * Thin OkHttp WebSocket wrapper for /ws/reader-sync — port of
 * ios/Bookq/Sync/PairSocket.swift. Inbound frames (and link lifecycle)
 * flow to the store over one Flow; outbound sends are JSON text. Auth
 * rides the upgrade request's Bearer header — a 4401 close means
 * credentials expired.
 */
class PairSocket(
    private val client: OkHttpClient,
) : PairTransport {

    @Volatile private var socket: WebSocket? = null

    /** Events for the CURRENT link only — reconnect makes a fresh flow. */
    override fun connect(url: String, token: String?): Flow<PairEvent> =
        callbackFlow {
            val req = Request.Builder().url(url).apply {
                if (!token.isNullOrEmpty()) {
                    header("Authorization", "Bearer $token")
                }
            }.build()
            val ws = client.newWebSocket(req, object : WebSocketListener() {
                override fun onOpen(webSocket: WebSocket, response: Response) {
                    trySend(PairEvent.Open)
                }
                override fun onMessage(webSocket: WebSocket, text: String) {
                    trySend(PairEvent.Frame(text))
                }
                override fun onClosing(
                    webSocket: WebSocket, code: Int, reason: String,
                ) {
                    trySend(PairEvent.Closed(code, reason))
                    webSocket.close(1000, null)
                }
                override fun onClosed(
                    webSocket: WebSocket, code: Int, reason: String,
                ) {
                    trySend(PairEvent.Closed(code, reason))
                }
                override fun onFailure(
                    webSocket: WebSocket, t: Throwable, response: Response?,
                ) {
                    trySend(PairEvent.Failed(t.message ?: "ws failure"))
                }
            })
            socket = ws
            awaitClose {
                socket = null
                ws.close(1000, null)
            }
        }

    override suspend fun send(text: String) {
        // OkHttp send() returns false once the socket is closing —
        // surface that as a link drop so the caller's ACK wait unwinds.
        if (!(socket?.send(text) ?: false)) {
            throw PairException.Disconnected()
        }
    }

    override suspend fun close() {
        socket?.close(1000, null)
        socket = null
    }
}
