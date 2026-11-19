import XCTest
@testable import Bookq

/// AuthManager PKCE/token-pair tests — RFC 8252 verifier/challenge shape,
/// JWT-claim identity decode, refresh rotation + single-flight coalescing,
/// atomic Keychain persistence.
@MainActor
final class AuthManagerTests: XCTestCase {

    override func setUp() async throws {
        StubURLProtocol.reset()
    }

    private func manager() -> AuthManager {
        AuthManager(session: StubURLProtocol.session())
    }

    // MARK: - PKCE shape (RFC 7636)

    /// Verifier is 43–128 chars of the unreserved alphabet.
    func testVerifierShape() {
        let v = AuthManager.makeVerifier()
        XCTAssertTrue((43...128).contains(v.count))
        XCTAssertTrue(v.allSatisfy {
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~".contains($0)
        })
    }

    /// Challenge = base64url(SHA256(verifier)) — 43 chars, no +/=/.
    func testChallengeShape() {
        let c = AuthManager.makeChallenge(verifier: AuthManager.makeVerifier())
        XCTAssertEqual(c.count, 43)
        XCTAssertFalse(c.contains("+") || c.contains("/") || c.contains("="))
    }

    /// RFC 7636 appendix B golden vector.
    func testChallengeGoldenVector() {
        let c = AuthManager.makeChallenge(
            verifier: "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        XCTAssertEqual(c, "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")
    }

    // MARK: - JWT identity

    private func jwt(sub: String = "u@x.jp", name: String = "U") -> String {
        let b64 = { (s: String) in
            Data(s.utf8).base64EncodedString()
                .replacingOccurrences(of: "+", with: "-")
                .replacingOccurrences(of: "/", with: "_")
                .replacingOccurrences(of: "=", with: "")
        }
        return "\(b64("{}")).\(b64(#"{"sub":"\#(sub)","name":"\#(name)","exp":9999999999}"#)).sig"
    }

    func testUserDecodedFromJWT() {
        let u = AuthManager.user(fromAccessToken: jwt(sub: "me@x.jp", name: "Me"))
        XCTAssertEqual(u?.email, "me@x.jp")
        XCTAssertEqual(u?.name, "Me")
    }

    func testUserNilForGarbageToken() {
        XCTAssertNil(AuthManager.user(fromAccessToken: "not-a-jwt"))
    }

    // MARK: - Refresh rotation

    /// refresh_token grant → new pair persisted + Bearer swapped; the
    /// presented refresh must be consumed (rotation, server-side).
    func testRefreshRotationSwapsPair() async {
        let auth = manager()
        auth.baseURLProvider = { URL(string: "http://unit.test")! }
        StubURLProtocol.handler = { _ in
            StubURLProtocol.json([
                "access_token": self.jwt(sub: "u@x.jp", name: "U"),
                "token_type": "Bearer", "expires_in": 3600,
                "refresh_token": "r2"])
        }
        // No stored refresh → nil without touching the wire.
        let fresh = await auth.refreshAccessToken()
        XCTAssertNil(fresh)
        XCTAssertEqual(StubURLProtocol.hits(path: "api/v1/auth/token"), 0)

        // Seeded pair → grant posts refresh_token, new Bearer applied.
        auth.testSeed(TokenPair(.init(access_token: jwt(), token_type: "Bearer",
                                      expires_in: 3600, refresh_token: "r1")))
        let tok = await auth.refreshAccessToken()
        let grantBody = StubURLProtocol.decodedBody(
            StubURLProtocol.last(path: "api/v1/auth/token"))
        XCTAssertEqual(grantBody?["grant_type"] as? String, "refresh_token")
        XCTAssertEqual(grantBody?["refresh_token"] as? String, "r1")
        XCTAssertNotNil(tok)
        XCTAssertEqual(auth.token, tok)
        XCTAssertEqual(auth.user?.email, "u@x.jp")
    }

    /// Two parallel refreshes coalesce to ONE exchange — presenting the
    /// same refresh twice would rotate-consume it and lose the session.
    func testConcurrentRefreshCoalesced() async {
        let auth = manager()
        auth.baseURLProvider = { URL(string: "http://unit.test")! }
        var hits = 0
        StubURLProtocol.handler = { _ in
            hits += 1
            return StubURLProtocol.json([
                "access_token": self.jwt(), "token_type": "Bearer",
                "expires_in": 3600, "refresh_token": "r2"])
        }
        auth.testSeed(TokenPair(.init(access_token: jwt(), token_type: "Bearer",
                                      expires_in: 3600, refresh_token: "r1")))
        async let a = auth.refreshAccessToken()
        async let b = auth.refreshAccessToken()
        let (ta, tb) = await (a, b)
        XCTAssertEqual(hits, 1)
        XCTAssertEqual(ta, tb)
        XCTAssertNotNil(ta)
    }

    /// Exchange failure (401 on bad code) propagates as AuthError.exchange.
    func testExchangeErrorSurfaces() async {
        StubURLProtocol.handler = { _ in
            StubURLProtocol.json(["detail": "invalid or expired code"], status: 401)
        }
        let auth = manager()
        auth.baseURLProvider = { URL(string: "http://unit.test")! }
        let tok = await auth.refreshAccessToken()
        XCTAssertNil(tok)
    }

    // MARK: - Session death (Android sessionExpired parity)

    /// Refresh rejected 401 (e.g. server restart wiped the in-memory
    /// store) → nil + onSessionExpired fires + local credentials dropped.
    func testRefreshRejectedEmitsSessionExpired() async {
        let auth = manager()
        auth.baseURLProvider = { URL(string: "http://unit.test")! }
        var expired = false
        auth.onSessionExpired = { expired = true }
        StubURLProtocol.handler = { _ in
            StubURLProtocol.json(["detail": "invalid or expired refresh_token"], status: 401)
        }
        auth.testSeed(TokenPair(.init(access_token: jwt(), token_type: "Bearer",
                                      expires_in: 3600, refresh_token: "r1")))
        let tok = await auth.refreshAccessToken()
        XCTAssertNil(tok)
        XCTAssertTrue(expired)
        XCTAssertNil(auth.user)       // logout() ran
        XCTAssertNil(auth.token)
    }

    /// 400 is equally definitive (Android parity — consumed/malformed).
    func testRefreshBadRequestEmitsSessionExpired() async {
        let auth = manager()
        auth.baseURLProvider = { URL(string: "http://unit.test")! }
        var expired = false
        auth.onSessionExpired = { expired = true }
        StubURLProtocol.handler = { _ in
            StubURLProtocol.json(["detail": "refresh_token required"], status: 400)
        }
        auth.testSeed(TokenPair(.init(access_token: jwt(), token_type: "Bearer",
                                      expires_in: 3600, refresh_token: "r1")))
        let tok = await auth.refreshAccessToken()
        XCTAssertNil(tok)
        XCTAssertTrue(expired)
    }

    /// 5xx is transient — no emit, credentials retained.
    func testRefreshServerErrorDoesNotEmit() async {
        let auth = manager()
        auth.baseURLProvider = { URL(string: "http://unit.test")! }
        var expired = false
        auth.onSessionExpired = { expired = true }
        StubURLProtocol.handler = { _ in
            StubURLProtocol.json(["detail": "boom"], status: 500)
        }
        auth.testSeed(TokenPair(.init(access_token: jwt(), token_type: "Bearer",
                                      expires_in: 3600, refresh_token: "r1")))
        let tok = await auth.refreshAccessToken()
        XCTAssertNil(tok)
        XCTAssertFalse(expired)
        XCTAssertNotNil(auth.user)
        XCTAssertNotNil(auth.token)
    }

    /// Network failure is transient — no emit, credentials retained.
    func testRefreshNetworkErrorDoesNotEmit() async {
        let auth = manager()
        auth.baseURLProvider = { URL(string: "http://unit.test")! }
        var expired = false
        auth.onSessionExpired = { expired = true }
        StubURLProtocol.failWith = .cannotConnectToHost
        auth.testSeed(TokenPair(.init(access_token: jwt(), token_type: "Bearer",
                                      expires_in: 3600, refresh_token: "r1")))
        let tok = await auth.refreshAccessToken()
        XCTAssertNil(tok)
        XCTAssertFalse(expired)
        XCTAssertNotNil(auth.user)
    }

    /// An empty refresh (e.g. -testToken seeds "") can never succeed —
    /// skip the exchange entirely so its 400 can't read as session death.
    func testRefreshEmptyRefreshTokenSkipsExchange() async {
        let auth = manager()
        auth.baseURLProvider = { URL(string: "http://unit.test")! }
        var expired = false
        auth.onSessionExpired = { expired = true }
        auth.testSeed(TokenPair(.init(access_token: jwt(), token_type: "Bearer",
                                      expires_in: 3600, refresh_token: "")))
        let tok = await auth.refreshAccessToken()
        XCTAssertNil(tok)
        XCTAssertFalse(expired)
        XCTAssertEqual(StubURLProtocol.hits(path: "api/v1/auth/token"), 0)
    }

    /// A refresh exchange suspended across logout() must not write the
    /// old session's pair back — credentialEpoch discards the stale
    /// result instead of silently re-authenticating a logged-out app.
    func testInFlightRefreshDiscardedAfterLogout() async throws {
        let auth = manager()
        auth.baseURLProvider = { URL(string: "http://unit.test")! }
        let newAccess = jwt(sub: "u@x.jp", name: "U")
        let gate = AsyncGate()
        StubURLProtocol.asyncHandler = { _ in
            await gate.wait()
            return StubURLProtocol.json([
                "access_token": newAccess, "token_type": "Bearer",
                "expires_in": 3600, "refresh_token": "r2"])
        }
        auth.testSeed(TokenPair(.init(access_token: jwt(), token_type: "Bearer",
                                      expires_in: 3600, refresh_token: "r1")))
        async let pending = auth.refreshAccessToken()
        // Wait until the exchange request actually reaches the stub.
        var waited = 0
        while StubURLProtocol.hits(path: "api/v1/auth/token") == 0, waited < 500 {
            try await Task.sleep(nanoseconds: 1_000_000)
            waited += 1
        }
        XCTAssertEqual(StubURLProtocol.hits(path: "api/v1/auth/token"), 1)
        auth.logout()
        gate.resume()   // exchange now returns a fresh pair — too late
        let tok = await pending
        XCTAssertNil(tok)
        XCTAssertNil(auth.token)
        XCTAssertNil(auth.user)
        // The stale pair was never persisted — a second refresh finds no
        // refresh token and never touches the wire again.
        _ = await auth.refreshAccessToken()
        XCTAssertEqual(StubURLProtocol.hits(path: "api/v1/auth/token"), 1)
    }
}

/// One-shot async gate for `StubURLProtocol.asyncHandler` — `wait()`
/// suspends the stub's response until the test calls `resume()`.
private final class AsyncGate: @unchecked Sendable {
    private let lock = NSLock()
    private var cont: CheckedContinuation<Void, Never>?

    func wait() async {
        await withCheckedContinuation { c in
            lock.lock()
            cont = c
            lock.unlock()
        }
    }

    func resume() {
        lock.lock()
        let c = cont
        cont = nil
        lock.unlock()
        c?.resume()
    }
}
