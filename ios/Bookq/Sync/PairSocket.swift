import Foundation

/// Thin URLSessionWebSocketTask wrapper for /ws/reader-sync.
/// Inbound frames (and link lifecycle) flow to the store over one
/// AsyncStream; outbound sends are JSON text. Auth rides the upgrade
/// request's Bearer header — a 4401 close means credentials expired.
actor PairSocket {
    /// Raw wire text — the store owns JSON decoding so the stream stays
    /// Sendable across the actor boundary.
    enum Event: Sendable {
        case open
        case frame(String)
        case closed(code: Int, reason: String)
        case failed(String)
    }

    private var task: URLSessionWebSocketTask?
    private var session: URLSession?
    private var continuation: AsyncStream<Event>.Continuation?
    private var open = false

    /// Events for the CURRENT link only — reconnect makes a fresh stream.
    func connect(url: URL, token: String?) -> AsyncStream<Event> {
        closeSocket()
        var req = URLRequest(url: url)
        if let token, !token.isEmpty {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        let session = URLSession(configuration: .default)
        let task = session.webSocketTask(with: req)
        self.session = session
        self.task = task
        self.open = false
        let (stream, cont) = AsyncStream.makeStream(of: Event.self)
        self.continuation = cont
        task.resume()
        receive()
        return stream
    }


    func send(_ text: String) async throws {
        try await task?.send(.string(text))
    }

    func closeSocket() {
        task?.cancel(with: .normalClosure, reason: nil)
        task = nil
        session?.invalidateAndCancel()
        session = nil
        open = false
        continuation?.finish()
        continuation = nil
    }

    /// WebSocketTask has no close-code readback — record what we sent
    /// and surface remote closes through receive() errors.
    private func receive() {
        task?.receive { [weak self] result in
            guard let self else { return }
            Task { await self.handle(result) }
        }
    }

    private func handle(_ result: Result<URLSessionWebSocketTask.Message, Error>) {
        switch result {
        case .success(let msg):
            if !open {
                open = true
                // First successful receive proves the upgrade landed —
                // FakeTransport yields `.open` at connect for parity.
                continuation?.yield(.open)
            }
            switch msg {
            case .string(let text):
                continuation?.yield(.frame(text))
            case .data(let data):
                if let text = String(data: data, encoding: .utf8) {
                    continuation?.yield(.frame(text))
                }
            @unknown default:
                break
            }
            receive()
        case .failure(let err):
            let ns = err as NSError
            // urlSessionWebSocketTask reports the close code when the peer
            // initiates the handshake close; otherwise this is a drop.
            let code = task?.closeCode.rawValue ?? ns.code
            continuation?.yield(.closed(code: code, reason: ns.localizedDescription))
            continuation?.finish()
        }
    }

}
