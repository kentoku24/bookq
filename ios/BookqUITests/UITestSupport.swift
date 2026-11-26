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
    /// environment — ios-ui-test.sh injects BOOKQ_UI_TEST_SERVER_URL into
    /// the xctestrun so the app always matches the URL it health-gated.
    /// The default is IPv4 loopback, not localhost: on hosts where
    /// localhost resolves to ::1 a foreign service can hold :8080.
    static let serverURL: String = {
        let env = ProcessInfo.processInfo.environment["BOOKQ_UI_TEST_SERVER_URL"]?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return (env?.isEmpty == false) ? env! : "http://127.0.0.1:8080"
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
            // Every test method starts from clean persisted state —
            // positions/pair-sync/layout prefs would otherwise leak
            // between tests sharing one app install.
            "-uitestReset",
        ]
        app.launch()
    }
}

extension XCUIApplication {
    /// Type into a text input only after the software keyboard proves the
    /// tap actually focused it, then validate what landed. `.searchable`
    /// on iOS 27 can otherwise accept a tap visually while XCUITest sees
    /// no keyboard focus; typeText then fails before its own retry ever
    /// taps the field again (#621). Validation also turns a silently
    /// dropped character into a retry instead of a downstream 30s timeout.
    func typeTextReliably(_ text: String, into element: XCUIElement,
                          file: StaticString = #filePath, line: UInt = #line) {
        for _ in 0..<3 {
            focusTextInput(element, file: file, line: line)
            element.typeText(text)
            if waitForTextInputValue(element, text, timeout: 2) { return }
            clearTextInput(element, file: file, line: line)
        }
        attachUIState()
        XCTFail("text input never accepted '\(text)' "
                    + "(current value: '\(textInputValue(of: element))')",
                file: file, line: line)
    }

    /// Clear a text input and verify the empty state. SearchField exposes
    /// the placeholder as `value` while empty, so a bare string comparison
    /// cannot tell empty from the prompt text.
    func clearTextInput(_ element: XCUIElement,
                        file: StaticString = #filePath, line: UInt = #line) {
        if textInputValue(of: element).isEmpty { return }
        for label in ["テキストを消去", "Clear text"] {
            for button in [element.buttons[label], buttons[label]] {
                guard button.exists, button.isHittable else { continue }
                button.tap()
                if waitForTextInputValue(element, "", timeout: 1.5) { return }
            }
        }

        focusTextInput(element, file: file, line: line)
        let delete = keyboards.keys["delete"]
        let count = textInputValue(of: element).count
        if delete.exists {
            for _ in 0..<max(count + 1, 8) {
                if textInputValue(of: element).isEmpty { break }
                delete.tap()
            }
        }
        if waitForTextInputValue(element, "", timeout: 1.5) { return }
        attachUIState()
        XCTFail("text input did not clear "
                    + "(current value: '\(textInputValue(of: element))')",
                file: file, line: line)
    }

    /// Tap until the software keyboard appears. Hardware-keyboard mode is
    /// disabled by ios-ui-test.sh; without that keyboard signal, typing is
    /// not attempted because XCTest cannot direct text into the field.
    private func focusTextInput(_ element: XCUIElement,
                                timeout: TimeInterval = 8,
                                file: StaticString = #filePath,
                                line: UInt = #line) {
        let keyboard = keyboards.firstMatch
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if keyboard.exists { return }
            element.tap()
            if keyboard.waitForExistence(timeout: 1.5) { return }
        }
        attachUIState()
        XCTFail("text input never presented the software keyboard",
                file: file, line: line)
    }

    private func textInputValue(of element: XCUIElement) -> String {
        let value = element.value as? String ?? ""
        if let placeholder = element.placeholderValue, value == placeholder {
            return ""
        }
        return value
    }

    private func waitForTextInputValue(_ element: XCUIElement,
                                       _ expected: String,
                                       timeout: TimeInterval) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if textInputValue(of: element) == expected { return true }
            RunLoop.current.run(until: Date().addingTimeInterval(0.1))
        }
        return textInputValue(of: element) == expected
    }

    /// Wait for `element` to appear; on timeout attach a screenshot and
    /// the full accessibility tree to the test activity, then fail. A
    /// bare "did not appear" cannot distinguish cover-stuck / login /
    /// empty-level on a CI runner — the attachments land in the
    /// .xcresult that ios-ui-test.sh uploads, so the next flake is
    /// diagnosable instead of a dead timeout.
    func assertExists(_ element: XCUIElement, within timeout: TimeInterval,
                      _ message: @autoclosure () -> String = "",
                      file: StaticString = #filePath, line: UInt = #line) {
        if element.waitForExistence(timeout: timeout) { return }
        attachUIState()
        XCTFail(message().isEmpty
                    ? "\(element) did not appear within \(timeout)s"
                    : message(),
                file: file, line: line)
    }

    /// Same check in reverse — an element that must go away (a dismissed
    /// cover, a withdrawn control) staying visible is a different bug
    /// than the next screen being late.
    func assertGone(_ element: XCUIElement, within timeout: TimeInterval,
                    _ message: @autoclosure () -> String = "",
                    file: StaticString = #filePath, line: UInt = #line) {
        if element.waitForNonExistence(timeout: timeout) { return }
        attachUIState()
        XCTFail(message().isEmpty
                    ? "\(element) still present after \(timeout)s"
                    : message(),
                file: file, line: line)
    }

    private func attachUIState() {
        XCTContext.runActivity(named: "UI state at timeout") { activity in
            activity.add(XCTAttachment(screenshot: screenshot()))
            activity.add(XCTAttachment(string: debugDescription))
        }
    }
}
