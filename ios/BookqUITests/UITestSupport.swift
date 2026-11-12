import XCTest

/// Shared launch configuration for the Bookq UI tests.
///
/// The app under test is pointed at a TEST_MODE bookq server through the
/// `-serverURL` / `-testToken` launch arguments `AppModel` reads
/// (`injectedServerURL` / `injectedTestToken`, DEBUG-only). TEST_MODE
/// servers accept any Bearer token, but the access JWT's `sub` claim is
/// decoded client-side to build `AuthUser` — so the token only needs a
/// decodable payload, never a valid signature.
enum UITestSupport {
    /// TEST_MODE server base URL. Overridable through the test runner's
    /// environment (e.g. an Xcode scheme environment variable); CI uses
    /// the localhost default.
    static let serverURL: String = {
        let env = ProcessInfo.processInfo.environment["BOOKQ_UI_TEST_SERVER_URL"]?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return (env?.isEmpty == false) ? env! : "http://localhost:8080"
    }()

    /// Unsigned JWT-shaped token (`header.payload.sig`): the payload
    /// carries the `sub`/`name` claims `AuthManager.user(fromAccessToken:)`
    /// decodes. Signature is never checked client- or server-side here.
    static let testToken: String = {
        func b64url(_ s: String) -> String {
            Data(s.utf8).base64EncodedString()
                .replacingOccurrences(of: "+", with: "-")
                .replacingOccurrences(of: "/", with: "_")
                .replacingOccurrences(of: "=", with: "")
        }
        let header = b64url(#"{"alg":"none","typ":"JWT"}"#)
        let payload = b64url(
            #"{"sub":"ui-test@bookq.local","name":"UI Test","exp":4102444800}"#)
        return "\(header).\(payload).sig"
    }()

    /// Launch the app pre-configured for the TEST_MODE fixture server.
    static func launch(_ app: XCUIApplication) {
        app.launchArguments = [
            "-serverURL", serverURL,
            "-testToken", testToken,
        ]
        app.launch()
    }
}
