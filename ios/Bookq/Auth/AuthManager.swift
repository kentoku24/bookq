import AuthenticationServices
import Combine
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

/// Holds the native bearer token and runs the ASWebAuthenticationSession
/// login against `GET /auth/login?native=1` (see `app/auth.py`).
@MainActor
final class AuthManager: ObservableObject {
    static let callbackScheme = "bookq"
    private let account = "native-token"

    /// Nonisolated token storage for the API layer (see TokenBox).
    nonisolated let tokenBox = TokenBox()
    @Published private(set) var user: MeResponse?

    var token: String? { tokenBox.token }

    /// Restore the persisted token at launch.
    func restore() {
        tokenBox.token = KeychainStore.load(account: account)
    }

    /// System-browser OAuth → `bookq://auth/callback?token=...` → Keychain.
    func login(baseURL: URL) async throws {
        var comps = URLComponents(url: baseURL.appendingPathComponent("auth/login"),
                                  resolvingAgainstBaseURL: false)!
        comps.queryItems = [URLQueryItem(name: "native", value: "1")]
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
              let token = parts.queryItems?.first(where: { $0.name == "token" })?.value,
              !token.isEmpty
        else { throw AuthError.noToken }

        try KeychainStore.save(token: token, account: account)
        self.tokenBox.token = token
    }

    func apply(user: MeResponse) {
        self.user = user
    }

    func logout() {
        KeychainStore.delete(account: account)
        tokenBox.token = nil
        user = nil
    }

    enum AuthError: LocalizedError {
        case cancelled, noToken
        var errorDescription: String? {
            switch self {
            case .cancelled: return "ログインがキャンセルされました"
            case .noToken: return "認証トークンを取得できませんでした"
            }
        }
    }
}
