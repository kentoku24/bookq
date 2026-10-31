import Foundation
import SwiftUI

/// Dual-reader sync client — Swift port of viewer/pairClient.js +
/// pairController.js (server-ws plane only; the webrtc-dc upgrade is
/// deliberately absent, so `dataPlane` stays "server-ws").
///
/// Lifecycle: idle → connecting → waiting(leader)/pending(follower) →
/// paired (phase from STATE drives the reader) → ended. RESUME rebinds a
/// member within the server's 300s window; nothing is ever replayed.
protocol PairTransporting: Actor {
    func connect(url: URL, token: String?) -> AsyncStream<PairSocket.Event>
    /// Pre-encoded JSON text — wire frames are regular JSON (canonical
    /// encoding is only for manifest digest, never for transport).
    func send(_ text: String) async throws
    func closeSocket()
}

extension PairSocket: PairTransporting {}

enum PairError: LocalizedError {
    case rejected(reason: String, snapshot: [String: Any]?)
    case disconnected
    case malformed(String)

    var errorDescription: String? {
        switch self {
        case .rejected(let r, _): return "rejected: \(r)"
        case .disconnected: return "disconnected"
        case .malformed(let m): return m
        }
    }
}

@MainActor
final class PairStore: ObservableObject {
    enum Status: Equatable {
        case idle
        case connecting
        case waiting        // leader holds the join code
        case pending        // follower awaiting leader approval
        case paired         // session bound — `phase` carries the rest
        case ended(reason: String)
    }

    // MARK: - Published surface

    @Published private(set) var status: Status = .idle
    @Published private(set) var member: Member?
    @Published private(set) var code: String?
    @Published private(set) var joinRequest: (memberId: String, name: String)?
    @Published private(set) var lastState: PairSnapshot?
    @Published private(set) var manifest: PairManifest?
    @Published private(set) var projection: PairProjection?
    @Published private(set) var errorText: String?
    @Published private(set) var endReason: String?

    struct Member: Codable, Equatable {
        var sessionId: String
        var memberId: String
        var role: String        // "leader" | "follower"
        var resumeToken: String
    }

    var paired: Bool {
        guard case .paired = status, let s = lastState else { return false }
        return s.phase != .waiting
    }
    var role: PairRole? { member.flatMap { PairRole(rawValue: $0.role) } }
    var phase: PairPhase? { lastState?.phase }
    var bound: Bool { member != nil && status != .idle && status != .connecting }

    /// Peer display info (the other member in presence).
    var peer: PairPresenceEntry? {
        guard let m = member, let st = lastState else { return nil }
        return presence.first { $0.key != m.memberId }?.value
    }
    @Published private(set) var presence: [String: PairPresenceEntry] = [:]

    // MARK: - Wiring

    private let transportFactory: () -> PairTransporting
    private let urlProvider: () -> URL?
    private let tokenProvider: () -> String?
    /// Leader bootstrap / openWork initial-page resolution.
    var openWorkResolver: ((String) async -> PairPageRef?)?
    /// Folder a hidden-root check must veto before it reaches the wire.
    var visibilityBlocks: ((String) -> Bool)?
    /// Leader bootstrap: which work to share when paired with no manifest.
    var bootstrapWork: (() async -> (folder: String, page: PairPageRef)?)?

    private var transport: PairTransporting?
    private var eventTask: Task<Void, Never>?
    private var linkGen = 0
    private var pending: [String: CheckedContinuation<Void, Error>] = [:]
    private var ridSeq = 0

    // protocol mirrors (pairClient.js _seen/_applied/_readyFor/_deferredState)
    private var seen = (epoch: -1, rev: -1)
    private var applied = (epoch: -1, rev: -1)
    private var seenMv = 0
    private var readyFor: String?
    private var deferredState: [String: Any]?
    private var manifestMV: Int?
    private var manifestEpoch: Int?
    private var chunks: (key: String, digest: String, count: Int,
                         parts: [String?], got: Int,
                         epoch: Int, mv: Int)?
    private var appliedPending: [Int: String] = [:]   // revision -> requestId

    // reconnect machine
    private var offlineSince: Date?
    private var retryCount = 0
    private var retryTask: Task<Void, Never>?
    private var livenessTask: Task<Void, Never>?
    private var lastRx = Date.distantPast
    private var attemptingResume = false
    private var entryType: String?
    private var entryPayload: [String: Any] = [:]
    private let resumeWindow: TimeInterval
    private var bootstrapTried = false
    private var suspended = false

    private let persistKey: () -> String
    private var credStore: Member? {
        get {
            guard let data = UserDefaults.standard.data(forKey: persistKey()),
                  let m = try? JSONDecoder().decode(Member.self, from: data)
            else { return nil }
            return m
        }
        set {
            let key = persistKey()
            if let n = newValue, let d = try? JSONEncoder().encode(n) {
                UserDefaults.standard.set(d, forKey: key)
            } else {
                UserDefaults.standard.removeObject(forKey: key)
            }
        }
    }

    init(url: @escaping () -> URL?, token: @escaping () -> String?,
         transportFactory: @escaping () -> PairTransporting = { PairSocket() },
         persistKey: @escaping () -> String = { "bookq.pairSync.v1" },
         resumeWindow: TimeInterval = 300) {
        self.urlProvider = url
        self.tokenProvider = token
        self.transportFactory = transportFactory
        self.persistKey = persistKey
        self.resumeWindow = resumeWindow
    }

    // MARK: - Entry ops

    /// Silent boot resume (web boot()): adopt persisted creds if any.
    func bootResume() {
        guard status == .idle, credStore != nil else { return }
        Task { _ = try? await resume() }
    }

    func create() async throws {
        try await entry(.create, type: "CREATE", payload: [:], sessionId: nil)
    }

    func join(code: String) async throws {
        guard code.range(of: #"^\d{6}$"#, options: .regularExpression) != nil else {
            throw PairError.malformed("code must be 6 digits")
        }
        try await entry(.join, type: "JOIN", payload: ["code": code], sessionId: nil)
    }

    /// `pick` = matched memberId hint (web "この端末と同期" entry point).
    func match(pick: String? = nil) async throws {
        var payload: [String: Any] = [:]
        if let pick { payload["pick"] = pick }
        try await entry(.match, type: "MATCH", payload: payload,
                        sessionId: nil)
    }

    func resume() async throws {
        guard let creds = member ?? credStore else {
            finishEnded(reason: "credentials_lost"); return
        }
        if offlineSince == nil { offlineSince = Date() }
        try await entry(.resume, type: "RESUME",
                            payload: ["memberId": creds.memberId,
                                      "resumeToken": creds.resumeToken],
                            sessionId: creds.sessionId, memberOverride: creds)
    }

    private enum EntryMode { case create, join, match, resume }

    private func entry(_ mode: EntryMode, type: String,
                       payload: [String: Any], sessionId: String?,
                       memberOverride: Member? = nil) async throws {
        // Leave politely first, then reset (web _leaveBound + _teardownLink).
        if bound { _ = try? await sendRequest("LEAVE", payload: [:]) }
        // The resume window must survive the entry teardown — teardown()
        // clears offlineSince/retryCount, which the reconnect machine owns.
        let priorOffline = offlineSince
        let priorRetry = retryCount
        let priorSuspended = suspended
        teardown()
        offlineSince = priorOffline
        retryCount = priorRetry
        suspended = priorSuspended
        attemptingResume = (mode == .resume)
        if let mo = memberOverride { member = mo }
        entryType = type
        entryPayload = payload
        status = .connecting
        openSocket()
        // The entry request is sent from onHello; ACK/REJECT resolves it.
        try await withCheckedThrowingContinuation { cont in
            entryCont = cont
            armEntryTimeout()
        }
    }

    private var entryCont: CheckedContinuation<Void, Error>?

    private func armEntryTimeout() {
        let budget: TimeInterval = 10
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(budget * 1_000_000_000))
            guard let self, !Task.isCancelled else { return }
            await MainActor.run { [weak self] in
                guard let self, let c = self.entryCont else { return }
                self.entryCont = nil
                self.linkDead()
                c.resume(throwing: PairError.disconnected)
            }
        }
    }

    // MARK: - Socket lifecycle

    private func openSocket() {
        linkGen += 1
        guard let url = urlProvider() else {
            finishEnded(reason: "error"); return
        }
        let t = transportFactory()
        transport = t
        let token = tokenProvider()
        let gen = linkGen
        eventTask?.cancel()
        eventTask = Task { [weak self] in
            let stream = await t.connect(url: url, token: token)
            guard let self else { return }
            for await ev in stream {
                guard !Task.isCancelled else { return }
                await self.handle(ev, gen: gen)
            }
        }
    }

    private func handle(_ ev: PairSocket.Event, gen: Int) {
        guard gen == linkGen else { return }
        switch ev {
        case .open:
            break
        case .frame(let text):
            lastRx = Date(); armLiveness()
            guard let data = text.data(using: .utf8),
                  let f = (try? JSONSerialization.jsonObject(with: data))
                          as? [String: Any]
            else { return }   // undecodable — drop, link stays up
            dispatch(f)
        case .closed, .failed:
            linkDead()
        }
    }

    private func armLiveness() {
        livenessTask?.cancel()
        livenessTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 20_000_000_000)
            guard let self, !Task.isCancelled else { return }
            await MainActor.run { [weak self] in
                guard let self else { return }
                // 4 missed server PINGs — treat as a black-holed path.
                if Date().timeIntervalSince(self.lastRx) >= 20 {
                    self.linkDead()
                }
            }
        }
    }

    private func linkDead() {
        // Reject all in-flight requests; nothing is replayed (§3.5).
        let pend = pending; pending.removeAll()
        for (_, c) in pend { c.resume(throwing: PairError.disconnected) }
        if let c = entryCont {
            entryCont = nil
            c.resume(throwing: PairError.disconnected)
        }
        if member != nil {
            if offlineSince == nil { offlineSince = Date() }
            status = .connecting
            scheduleRetry()
        } else {
            status = .idle
        }
    }

    private func scheduleRetry() {
        retryTask?.cancel()
        let base = [0.5, 1.0, 2.0, 5.0][min(retryCount, 3)]
        let jitter = Double.random(in: 0..<0.5)
        retryCount += 1
        // Beyond the resume window the session is gone — stop trying.
        if let off = offlineSince,
           Date().timeIntervalSince(off) + base + jitter > resumeWindow {
            finishEnded(reason: "resume_expired")
            return
        }
        retryTask = Task { [weak self] in
            try? await Task.sleep(
                nanoseconds: UInt64((base + jitter) * 1_000_000_000))
            guard let self, !Task.isCancelled else { return }
            await MainActor.run { [weak self] in
                guard let self, self.member != nil else { return }
                Task { try? await self.resume() }
            }
        }
    }

    private func teardown() {
        eventTask?.cancel(); eventTask = nil
        livenessTask?.cancel(); livenessTask = nil
        retryTask?.cancel(); retryTask = nil
        let t = transport; transport = nil
        if let t { Task { await t.closeSocket() } }
        for (_, c) in pending { c.resume(throwing: PairError.disconnected) }
        pending.removeAll()
        // A superseding entry/teardown must release the in-flight entry
        // waiter — nilling it leaks the awaiting create()/join() forever.
        entryCont?.resume(throwing: PairError.disconnected)
        entryCont = nil
        member = nil; code = nil; joinRequest = nil
        lastState = nil; manifest = nil; projection = nil
        presence = [:]
        seen = (-1, -1); applied = (-1, -1); seenMv = 0
        readyFor = nil; deferredState = nil
        manifestMV = nil; manifestEpoch = nil; chunks = nil
        appliedPending = [:]
        offlineSince = nil; retryCount = 0
        bootstrapTried = false
        suspended = false
        attemptingResume = false
    }

    // MARK: - Frame dispatch (inbound)

    private func dispatch(_ f: [String: Any]) {
        guard (f["v"] as? Int) == 1 else {
            finishEnded(reason: "incompatible"); return
        }
        switch f["type"] as? String {
        case "HELLO": onHello()
        case "PING":
            sendRaw(["v": 1, "type": "PONG", "seq": f["seq"] ?? NSNull()])
        case "PONG": break
        case "ACK": onAck(f)
        case "REJECT": onReject(f)
        case "JOIN_REQUEST":
            joinRequest = (memberId: f["memberId"] as? String ?? "",
                           name: f["name"] as? String ?? "")
            // Auto-approve (pairController parity): one APPROVE per request.
            Task { try? await approve() }
        case "JOIN_CANCEL":
            joinRequest = nil
        case "STATE": onState(f)
        case "MANIFEST": onManifest(f)
        case "APPLIED":
            if let rev = f["revision"] as? Int,
               let rid = f["requestId"] as? String {
                appliedPending[rev] = rid
            }
        case "END":
            let reason = f["reason"] as? String ?? "session_lost"
            finishEnded(reason: reason == "shutdown" ? "session_lost" : reason)
        case "ERROR":
            errorText = f["code"] as? String ?? "error"
        case "SIGNAL", "UI_SYNC", "TRANSPORT_STATE":
            break   // webrtc-dc / mirrored-browse not implemented
        default: break
        }
    }

    private func onHello() {
        guard let cont = entryCont, let type = entryType else { return }
        let req = envelope(type: type, payload: entryPayload,
                           sessionId: member?.sessionId)
        pending[req.requestId] = cont
        entryCont = nil
        sendReq(req)
    }

    // MARK: - Requests

    /// requestId + pre-encoded frame text — Sendable so it can cross
    /// into the send Task without dragging `[String: Any]` along.
    private struct OutRequest: Sendable {
        var requestId: String
        var text: String
    }

    private func rid() -> String {
        ridSeq += 1
        return "r\(ridSeq)\(Int.random(in: 0..<2176782336).radix36)"
    }

    private func envelope(type: String, payload: [String: Any],
                          sessionId: String? = nil,
                          workEpoch: Int? = nil, baseRevision: Int? = nil) -> OutRequest {
        let requestId = rid()
        let f: [String: Any] = [
            "v": 1, "requestId": requestId,
            "sessionId": sessionId ?? member?.sessionId ?? NSNull(),
            "workEpoch": workEpoch ?? max(seen.epoch, 0),
            "baseRevision": baseRevision ?? max(seen.rev, 0),
            "type": type, "payload": payload,
        ]
        let d = (try? JSONSerialization.data(withJSONObject: f)) ?? Data()
        return OutRequest(requestId: requestId,
                          text: String(decoding: d, as: UTF8.self))
    }

    /// Send a session-bound request; resolves with ACK data or throws
    /// PairError.rejected/disconnected.
    @discardableResult
    func sendRequest(_ type: String, payload: [String: Any],
                     workEpoch: Int? = nil,
                     baseRevision: Int? = nil) async throws {
        let req = envelope(type: type, payload: payload,
                           workEpoch: workEpoch, baseRevision: baseRevision)
        try await withCheckedThrowingContinuation { cont in
            pending[req.requestId] = cont
            Task { [weak self] in
                do {
                    try await self?.transport?.send(req.text)
                } catch {
                    await MainActor.run { [weak self] in
                        guard let self else { return }
                        if let c = self.pending.removeValue(forKey: req.requestId) {
                            c.resume(throwing: error)
                        }
                    }
                }
            }
        }
    }

    private func onAck(_ f: [String: Any]) {
        guard let rid = f["requestId"] as? String,
              let cont = pending.removeValue(forKey: rid) else { return }
        let data = f["data"] as? [String: Any] ?? [:]
        bindFromAck(data, requestType: f["request"] as? String)
        cont.resume(returning: ())
    }

    private func onReject(_ f: [String: Any]) {
        let reason = f["reason"] as? String ?? "malformed"
        let rid = f["requestId"] as? String
        let cont = rid.flatMap { pending.removeValue(forKey: $0) }
        // An unbound member means this reject answers the entry request.
        let wasEntry = cont != nil && member == nil && !attemptingResume
        // reject + latest snapshot (§3.4): adopt committed state verbatim.
        if let snap = f["snapshot"] as? [String: Any] {
            adoptSnapshot(snap)
        }
        if attemptingResume {
            // RESUME rejects: resume_in_use → retry, others → terminal.
            switch reason {
            case "resume_in_use":
                cont?.resume(throwing: PairError.rejected(reason: reason, snapshot: nil))
                scheduleRetry()
            case "bad_resume":
                cont?.resume(throwing: PairError.rejected(reason: reason, snapshot: nil))
                finishEnded(reason: "session_lost")
            default:
                cont?.resume(throwing: PairError.rejected(reason: reason, snapshot: nil))
                finishEnded(reason: reason)
            }
            return
        }
        cont?.resume(throwing: PairError.rejected(
            reason: reason, snapshot: f["snapshot"] as? [String: Any]))
        if wasEntry {
            // Dead-end entry (bad code etc.) — don't hold the socket.
            errorText = reason
            teardown()
            status = .idle
        }
    }

    private func bindFromAck(_ d: [String: Any], requestType: String?) {
        guard let sid = d["sessionId"] as? String,
              let mid = d["memberId"] as? String,
              let role = d["role"] as? String,
              let tok = d["resumeToken"] as? String else { return }
        // MATCH ambiguous: no bind fields — caller handles candidates.
        if d["ambiguous"] as? Bool == true { return }
        member = Member(sessionId: sid, memberId: mid, role: role,
                        resumeToken: tok)
        credStore = member
        if let c = d["code"] as? String { code = c }
        readyFor = nil
        retryCount = 0; offlineSince = nil; attemptingResume = false
        // JOIN binds a *pending* candidate — paired only once the leader's
        // APPROVE lands and STATE shows both members active.
        status = (requestType == "JOIN") ? .pending : .paired
        // The server re-marks a resumed member 'active' — a locally
        // asserted suspension must be re-sent or the session un-pauses
        // for a member nobody can see.
        if requestType == "RESUME", suspended { setActivity("suspended") }
    }

    // MARK: - STATE / MANIFEST / projection

    private func adoptSnapshot(_ snap: [String: Any]) {
        let pseudo: [String: Any] = [
            "v": 1, "type": "STATE",
            "sessionId": snap["sessionId"] ?? member?.sessionId ?? NSNull(),
            "revision": snap["revision"] ?? 0,
            "workEpoch": snap["workEpoch"] ?? 0,
            "manifestVersion": snap["manifestVersion"] ?? 0,
            "phase": snap["phase"] ?? "waiting",
            "spreadId": snap["spreadId"] ?? NSNull(),
            "offset": snap["offset"] ?? 0,
            "leaderSide": snap["leaderSide"] ?? "left",
            "presence": lastStatePresence(),
            "dataPlane": snap["dataPlane"] ?? "server-ws",
        ]
        onState(pseudo)
    }

    private func lastStatePresence() -> [String: Any] {
        var out: [String: Any] = [:]
        for (k, v) in presence {
            out[k] = ["role": v.role.rawValue, "status": v.status,
                      "name": v.name, "ready": v.ready]
        }
        return out
    }

    private func onState(_ f: [String: Any]) {
        guard let rev = f["revision"] as? Int,
              let epoch = f["workEpoch"] as? Int else { return }
        if let m = member, let sid = f["sessionId"] as? String,
           sid != m.sessionId { return }
        let newer = epoch > seen.epoch || (epoch == seen.epoch && rev > seen.rev)
        guard newer else { return }   // dup / out-of-order
        seen = (epoch, rev)
        seenMv = f["manifestVersion"] as? Int ?? 0
        errorText = nil

        var pres: [String: PairPresenceEntry] = [:]
        for (k, v) in (f["presence"] as? [String: Any] ?? [:]) {
            guard let d = v as? [String: Any] else { continue }
            pres[k] = PairPresenceEntry(
                role: PairRole(rawValue: d["role"] as? String ?? "") ?? .follower,
                status: d["status"] as? String ?? "",
                name: d["name"] as? String ?? "",
                ready: d["ready"] as? Bool ?? false)
        }
        presence = pres

        let snap = PairSnapshot(
            sessionId: f["sessionId"] as? String,
            revision: rev, workEpoch: epoch,
            manifestVersion: seenMv,
            phase: PairPhase(rawValue: f["phase"] as? String ?? "") ?? .waiting,
            spreadId: f["spreadId"] as? String,
            offset: f["offset"] as? Int ?? 0,
            leaderSide: PairSide(rawValue: f["leaderSide"] as? String ?? "") ?? .left,
            dataPlane: f["dataPlane"] as? String ?? "server-ws")
        lastState = snap

        // Status follows the committed phase.
        if member != nil {
            switch snap.phase {
            case .waiting:
                status = role == .leader ? .waiting : .pending
            default:
                status = .paired
            }
        }

        // Apply only when we hold the manifest this STATE references.
        let needMv = snap.manifestVersion
        let haveManifest = manifest != nil &&
            manifestEpoch == epoch && manifestMV == needMv
        if needMv > 0 && !haveManifest {
            deferredState = f
            return
        }
        if applied.epoch == epoch && applied.rev >= rev { return }
        if epoch < applied.epoch { return }
        applied = (epoch, rev)
        applyState(snap)
        if let rid = appliedPending.removeValue(forKey: rev) {
            sendApplied(requestId: rid, rev: rev, epoch: epoch)
        }
        maybeBootstrap(snap)
    }

    /// pairedView.apply — project the committed spread onto this device.
    private func applyState(_ s: PairSnapshot) {
        guard s.phase == .reading || s.phase == .loading || s.phase == .paused,
              let manifest, let sid = s.spreadId,
              let si = manifest.spreadIndex(byId: sid),
              let role else {
            if s.phase == .selecting { projection = nil }
            return
        }
        let spread = manifest.spreadTable[si]
        let proj = PairModel.project(spread: spread, role: role,
                                     leaderSide: s.leaderSide)
        let anchor = spread.right
        let chName = manifest.chapters
            .first(where: { $0.folder == spread.chapterFolder })?.name
            ?? spread.chapterFolder
        projection = PairProjection(
            spreadIndex: si, spreadTotal: manifest.spreadTable.count,
            side: proj.side, ref: proj.ref, anchor: anchor,
            chapterName: chName, chapterFolder: spread.chapterFolder)
    }

    private func sendApplied(requestId: String, rev: Int, epoch: Int) {
        let req = envelope(type: "APPLIED", payload: ["requestId": requestId],
                           workEpoch: epoch, baseRevision: rev)
        sendReq(req)
    }

    /// Leader bootstrap (pairController._maybeBootstrapWork): a fresh pair
    /// with no manifest shares the leader's current/last-read work.
    private func maybeBootstrap(_ s: PairSnapshot) {
        guard !bootstrapTried, role == .leader, s.phase == .selecting,
              s.manifestVersion == 0 else { return }
        bootstrapTried = true
        Task { [weak self] in
            guard let self, let pick = await self.bootstrapWork?() else { return }
            await MainActor.run { [weak self] in
                guard let self else { return }
                _ = self.openWork(folder: pick.folder, page: pick.page)
            }
        }
    }

    private func onManifest(_ f: [String: Any]) {
        guard (f["kind"] as? String) == "manifest-chunk",
              let idx = f["index"] as? Int, let count = f["count"] as? Int,
              count >= 1, idx >= 0, idx < count,
              let data = f["data"] as? String,
              let digest = f["digest"] as? String else {
            errorText = "manifest_error"; return
        }
        let epoch = f["workEpoch"] as? Int ?? -1
        let mv = f["manifestVersion"] as? Int ?? -1
        // Re-delivery of the held manifest → re-READY (post-RESUME).
        if manifest != nil && manifestEpoch == epoch && manifestMV == mv {
            sendReady(); return
        }
        // Stale delivery — ignore entirely.
        if let haveE = manifestEpoch, let haveM = manifestMV,
           (epoch < haveE || (epoch == haveE && mv < haveM)) { return }
        let key = "\(f["sessionId"] as? String ?? "")|\(epoch)|\(mv)"
        if chunks?.key != key {
            chunks = (key: key, digest: digest, count: count,
                      parts: [String?](repeating: nil, count: count), got: 0,
                      epoch: epoch, mv: mv)
        }
        guard var b = chunks else { return }
        if b.digest != digest || b.count != count {
            chunks = nil; errorText = "manifest_error"; return
        }
        if b.parts[idx] == nil {
            b.parts[idx] = data; b.got += 1
            chunks = b
        }
        if b.got == b.count { finishManifest(b) }
    }

    private func finishManifest(_ b: (key: String, digest: String, count: Int,
                                      parts: [String?], got: Int,
                                      epoch: Int, mv: Int)) {
        chunks = nil
        let text = b.parts.compactMap { $0 }.joined()
        guard text.utf8.count <= 32 * 1024 * 1024,
              let data = text.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              (obj["digest"] as? String) == b.digest,
              CanonicalJSON.manifestDigest(obj) == b.digest,
              let parsed = PairManifest.parse(obj) else {
            errorText = "manifest_error"; return
        }
        manifest = parsed
        manifestEpoch = b.epoch
        manifestMV = b.mv
        sendReady()
        if let d = deferredState {
            deferredState = nil
            onState(d)
        }
    }

    /// READY once per (workEpoch, manifestVersion) — rides the manifest's
    /// own epoch (the commit STATE may lag the chunks).
    private func sendReady() {
        guard member != nil, let e = manifestEpoch, let mv = manifestMV else { return }
        let key = "\(e):\(mv)"
        if readyFor == key { return }
        readyFor = key
        Task {
            _ = try? await sendRequest("READY", payload: ["manifestVersion": mv],
                                       workEpoch: e)
        }
    }

    // MARK: - Session control

    func approve() async throws {
        guard let jr = joinRequest else { return }
        joinRequest = nil
        try await sendRequest("APPROVE", payload: ["memberId": jr.memberId])
    }

    func rejectJoin() async throws {
        guard let jr = joinRequest else { return }
        joinRequest = nil
        try await sendRequest("REJECT", payload: ["memberId": jr.memberId])
    }

    /// Polite leave — the server ENDs the session for both sides. Send
    /// happens BEFORE teardown (web _leaveBound) or the transport nil
    /// swallows the frame.
    func leave() {
        if let t = transport {
            let req = envelope(type: "LEAVE", payload: [:])
            Task { try? await t.send(req.text) }
        }
        credStore = nil
        finishEnded(reason: "left")
    }

    func setActivity(_ state: String) {
        guard ["active", "suspended", "selecting"].contains(state) else { return }
        suspended = (state == "suspended")
        Task { _ = try? await sendRequest("SET_ACTIVITY", payload: ["state": state]) }
    }

    /// scenePhase wiring: suspended while backgrounded (web pagehide).
    func scenePhase(_ phase: ScenePhase) {
        guard bound else { return }
        setActivity(phase == .active ? "active" : "suspended")
    }

    // MARK: - Reader intents (sendIntent port — gated on phase==reading)

    private var readingGated: Bool { lastState?.phase == .reading }

    @discardableResult
    func turn(delta: Int, crossChapter: Bool, fromChapter: String?) -> Bool {
        guard readingGated, delta == 1 || delta == -1 else { return false }
        send(type: "TURN", payload: [
            "delta": delta, "crossChapter": crossChapter,
            "fromChapter": fromChapter ?? NSNull(),
            "manifestVersion": seenMv])
        return true
    }

    @discardableResult
    func goto(page: PairPageRef) -> Bool {
        guard readingGated else { return false }
        send(type: "GOTO", payload: [
            "page": ["folder": page.folder, "name": page.name],
            "manifestVersion": seenMv])
        return true
    }

    @discardableResult
    func setOffset(_ offset: Int) -> Bool {
        guard readingGated, offset == 0 || offset == 1 else { return false }
        send(type: "SET_OFFSET", payload: ["offset": offset,
                                           "manifestVersion": seenMv])
        return true
    }

    @discardableResult
    func swapSides() -> Bool {
        guard readingGated, let cur = lastState?.leaderSide else { return false }
        send(type: "SWAP_SIDES",
             payload: ["leaderSide": cur == .right ? "left" : "right",
                       "manifestVersion": seenMv])
        return true
    }

    /// Leader-only work pick. `page` resolves the initial position; a
    /// hidden-root veto mirrors the web's `_visBlocksFolder` gate.
    @discardableResult
    func openWork(folder: String, page: PairPageRef?, offset: Int? = nil) -> Bool {
        guard role == .leader, paired else { return false }
        if visibilityBlocks?(folder) == true { return false }
        guard let page else { return false }
        var p: [String: Any] = [
            "folder": folder,
            "page": ["folder": page.folder, "name": page.name],
            "manifestVersion": seenMv]
        if let offset { p["offset"] = offset }
        send(type: "OPEN_WORK", payload: p)
        return true
    }

    /// Fire-and-forget data send (web _dataSend on server-ws == _ctlSend).
    private func send(type: String, payload: [String: Any]) {
        sendReq(envelope(type: type, payload: payload))
    }

    /// Send a framed request without tracking its ACK (fire-and-forget).
    private func sendReq(_ req: OutRequest) {
        let t = transport
        Task { try? await t?.send(req.text) }
    }

    /// Encode + send an ad-hoc frame (PONG) outside the request pipeline.
    private func sendRaw(_ frame: [String: Any]) {
        guard let d = try? JSONSerialization.data(withJSONObject: frame) else { return }
        let text = String(decoding: d, as: UTF8.self)
        let t = transport
        Task { try? await t?.send(text) }
    }

    /// Leader picking a work through the shared resolver (openWork intent).
    func pickWork(folder: String) async -> Bool {
        guard role == .leader, paired else { return false }
        if visibilityBlocks?(folder) == true { return false }
        guard let page = await openWorkResolver?(folder) else { return false }
        return openWork(folder: folder, page: page)
    }

    // MARK: - Teardown

    private func finishEnded(reason: String) {
        let wasBound = bound || member != nil
        teardown()
        status = .ended(reason: reason)
        endReason = reason
        if wasBound { credStore = nil }
    }

    /// Stop the session locally without sending LEAVE (connection gone).
    func disconnect() {
        credStore = nil
        teardown()
        status = .idle
    }
}

extension Int {
    var radix36: String { String(self, radix: 36) }
}
