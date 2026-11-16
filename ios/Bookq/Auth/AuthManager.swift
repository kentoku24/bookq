import AuthenticationServices
import Combine
import CryptoKit
import Foundation

/// Lock-protected token box — readable from any isolation domain so the
/// @Sendable request builder can attach Bearer headers off-actor.
final class TokenBox: @unchecked Sendable {
    private let lock = NSLock()
    private var value: String?

    var token: String? {
        get { lock.lock(); defer { lock.unlock() }; return value }
        set { lock.lock(); defer { lock.unlock() }; value = newValue }
    }
}

/// Access + refresh pair — persisted as ONE Keychain item so the pair is
/// written atomically (a refresh response must never land half-saved).
struct TokenPair: Codable {
    var accessToken: String
    var refreshToken: String

    init(_ r: TokenPairResponse) {
        accessToken = r.access_token
        refreshToken = r.refresh_token ?? ""
    }
}

/// Holds the native bearer token pair and runs the RFC 8252 PKCE login:
/// `GET /auth/login?client=native&code_challenge=…` → `bookq://auth?code=…`
/// → `POST /api/v1/auth/token` (see `app/auth.py`, `app/auth_token.py`).
@MainActor
final class AuthManager: ObservableObject {
    static let callbackScheme = "bookq"
    private let account = "native-token-pair"

    /// Nonisolated token storage for the API layer (see TokenBox).
    nonisolated let tokenBox = TokenBox()
    @Published private(set) var user: AuthUser?

    /// Injected for tests — defaults to a cookie-free ephemeral session
    /// (the token endpoint is Bearer bootstrap; no cookie auth mixing).
    private let session: URLSession
    init(session: URLSession? = nil) {
        if let session { self.session = session; return }
        let c = URLSessionConfiguration.ephemeral
        c.httpCookieStorage = nil
        c.httpShouldSetCookies = false
        self.session = URLSession(configuration: c)
    }

    var token: String? { tokenBox.token }
    private var refreshToken: String?

    /// Restore the persisted pair at launch.
    func restore() {
        guard let data = KeychainStore.load(account: account).flatMap({ Data($0.utf8) }),
              let pair = try? JSONDecoder().decode(TokenPair.self, from: data)
        else { return }
        tokenBox.token = pair.accessToken
        refreshToken = pair.refreshToken
        apply(user: Self.user(fromAccessToken: pair.accessToken))
    }

    /// RFC 8252: PKCE login via system browser → `bookq://auth?code=&state=`
    /// → token exchange → Keychain.
    func login(baseURL: URL) async throws {
        let verifier = Self.makeVerifier()
        let challenge = Self.makeChallenge(verifier: verifier)
        let state = Self.makeVerifier()

        var comps = URLComponents(url: baseURL.appendingPathComponent("auth/login"),
                                  resolvingAgainstBaseURL: false)!
        comps.queryItems = [
            URLQueryItem(name: "client", value: "native"),
            URLQueryItem(name: "code_challenge", value: challenge),
            URLQueryItem(name: "code_challenge_method", value: "S256"),
            URLQueryItem(name: "state", value: state),
        ]
        let loginURL = comps.url!

        let callback: URL = try await withCheckedThrowingContinuation { cont in
            let session = ASWebAuthenticationSession(
                url: loginURL,
                callbackURLScheme: Self.callbackScheme
            ) { url, error in
                if let error {
                    let asErr = error as? ASWebAuthenticationSessionError
                    if asErr?.code == .canceledLogin {
                        cont.resume(throwing: AuthError.cancelled)
                    } else {
                        cont.resume(throwing: error)
                    }
                } else if let url {
                    cont.resume(returning: url)
                } else {
                    cont.resume(throwing: AuthError.noToken)
                }
            }
            session.presentationContextProvider = PresentationAnchor.shared
            session.prefersEphemeralWebBrowserSession = false
            session.start()
        }

        guard let parts = URLComponents(url: callback, resolvingAgainstBaseURL: false),
              let code = parts.queryItems?.first(where: { $0.name == "code" })?.value,
              !code.isEmpty
        else { throw AuthError.noToken }
        // CSRF guard: the echoed state must match the one we sent.
        guard parts.queryItems?.first(where: { $0.name == "state" })?.value == state
        else { throw AuthError.stateMismatch }

        let resp = try await exchange(baseURL: baseURL, body: [
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
        ])
        let pair = TokenPair(resp)
        try persist(pair)
        // A refresh exchange from the previous session may still be in
        // flight — bump the epoch so its result is discarded on resume.
        credentialEpoch += 1
        self.tokenBox.token = pair.accessToken
        self.refreshToken = pair.refreshToken
        apply(user: Self.user(fromAccessToken: pair.accessToken))
    }

    /// Rotation contract: the presented refresh token is consumed whether
    /// or not it was still valid — a replayed old token dies. Concurrent
    /// 401s coalesce on one in-flight exchange.
    private var refreshTask: Task<String?, Never>?

    /// Bumped on every credential replace/clear (logout, login, testSeed).
    /// An exchange suspended across one of those must not write the old
    /// session's pair back — nor clobber a newer login — when it resumes.
    private var credentialEpoch = 0

    /// Return a fresh access token, or nil when refresh is impossible /
    /// failed (caller then drops to the login screen).
    func refreshAccessToken() async -> String? {
        if let t = refreshTask { return await t.value }
        // An empty refresh can never succeed — exchanging it would earn a
        // 400 that reads as session death (e.g. -testToken seeds "").
        guard let refreshToken, !refreshToken.isEmpty else { return nil }
        let epoch = credentialEpoch
        let t = Task { [weak self] () -> String? in
            guard let self, let base = self.baseURLProvider?() else { return nil }
            do {
                let resp = try await self.exchange(baseURL: base, body: [
                    "grant_type": "refresh_token",
                    "refresh_token": refreshToken,
                ])
                let pair = TokenPair(resp)
                guard self.credentialEpoch == epoch else { return nil }
                try self.persist(pair)
                self.tokenBox.token = pair.accessToken
                self.refreshToken = pair.refreshToken
                self.apply(user: Self.user(fromAccessToken: pair.accessToken))
                return pair.accessToken
            } catch AuthError.exchange(let status) where status == 400 || status == 401 {
                // Refresh rejected server-side — the presented token was
                // consumed or is unknown (e.g. a restart wiped the
                // in-memory store). Android sessionExpired parity: drop
                // local credentials and notify; the app routes to Login.
                // Network failures and 5xx fall through as transient nil.
                // A stale exchange (credentials replaced mid-flight) must
                // not re-logout the newer session or re-emit the signal.
                guard self.credentialEpoch == epoch else { return nil }
                self.logout()
                self.onSessionExpired?()
                return nil
            } catch {
                return nil
            }
        }
        refreshTask = t
        defer { refreshTask = nil }
        return await t.value
    }

    /// Server base URL the refresh exchange must target — set by AppModel.
    var baseURLProvider: (() -> URL?)?

    /// Definitive session-death signal — fired once when the refresh
    /// exchange is rejected (400/401). AppModel routes a live session to
    /// `.login`; transient failures (network, 5xx) never fire it.
    var onSessionExpired: (@MainActor () -> Void)?

    /// Test hook — inject a pair as if login/refresh had returned it.
    func testSeed(_ pair: TokenPair) {
        credentialEpoch += 1
        tokenBox.token = pair.accessToken
        refreshToken = pair.refreshToken
        apply(user: Self.user(fromAccessToken: pair.accessToken))
    }

    private func persist(_ pair: TokenPair) throws {
        let data = try JSONEncoder().encode(pair)
        try KeychainStore.save(token: String(decoding: data, as: UTF8.self),
                               account: account)
    }

    /// POST /api/v1/auth/token — unauthenticated bootstrap endpoint.
    private func exchange(baseURL: URL, body: [String: String]) async throws -> TokenPairResponse {
        var req = URLRequest(url: baseURL.appendingPathComponent("api/v1/auth/token"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.cachePolicy = .reloadIgnoringLocalCacheData
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, resp) = try await session.data(for: req)
        guard let http = resp as? HTTPURLResponse else { throw AuthError.noToken }
        guard (200..<300).contains(http.statusCode) else {
            throw AuthError.exchange(http.statusCode)
        }
        return try JSONDecoder().decode(TokenPairResponse.self, from: data)
    }

    func apply(user: AuthUser?) {
        self.user = user
    }

    func logout() {
        credentialEpoch += 1
        KeychainStore.delete(account: account)
        tokenBox.token = nil
        refreshToken = nil
        user = nil
    }

    // MARK: - PKCE / JWT helpers

    /// RFC 7636 §4.1 — 43–128 chars of unreserved alphabet.
    static func makeVerifier() -> String {
        let alphabet = Array("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")
        return String((0..<64).map { _ in alphabet.randomElement()! })
    }

    /// `BASE64URL-ENCODE(SHA256(verifier))`.
    static func makeChallenge(verifier: String) -> String {
        let digest = SHA256.hash(data: Data(verifier.utf8))
        return Data(digest).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    /// Decode the JWT payload's `sub`/`name`/`picture` — the token was
    /// minted for this client, so claim trust is inherent (no signature
    /// check needed client-side).
    static func user(fromAccessToken token: String) -> AuthUser? {
        let parts = token.split(separator: ".")
        guard parts.count >= 2 else { return nil }
        var b64 = String(parts[1])
            .replacingOccurrences(of: "-", with: "+")
            .replacingOccurrences(of: "_", with: "/")
        while b64.count % 4 != 0 { b64 += "=" }
        guard let data = Data(base64Encoded: b64),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let email = obj["sub"] as? String, !email.isEmpty
        else { return nil }
        return AuthUser(email: email,
                          name: (obj["name"] as? String) ?? email,
                          picture: obj["picture"] as? String)
    }

    enum AuthError: LocalizedError {
        case cancelled, noToken, stateMismatch, exchange(Int)
        var errorDescription: String? {
            switch self {
            case .cancelled: return "ログインがキャンセルされました"
            case .noToken: return "認証トークンを取得できませんでした"
            case .stateMismatch: return "認証状態が一致しません (state)"
            case .exchange(let s): return "トークン交換に失敗しました (HTTP \(s))"
            }
        }
    }
}
