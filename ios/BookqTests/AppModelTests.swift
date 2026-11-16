import XCTest
@testable import Bookq

/// AppModel session-death routing — `sessionDidExpire` drops only a live
/// Ready session to Login (Android `sessionExpired` parity). No network:
/// `didLogin()` reaches `.ready` through local state only.
@MainActor
final class AppModelTests: XCTestCase {

    /// No server URL → init stays `.onboarding`; session death is a no-op.
    func testSessionDidExpireIgnoredOutsideReady() {
        let model = AppModel()
        XCTAssertEqual(model.phase, .onboarding)
        model.sessionDidExpire()
        XCTAssertEqual(model.phase, .onboarding)
    }

    /// `.ready` → `.login` on definitive session death.
    func testSessionDidExpireFlipsReadyToLogin() {
        let model = AppModel()
        model.didLogin()
        XCTAssertEqual(model.phase, .ready)
        model.sessionDidExpire()
        XCTAssertEqual(model.phase, .login)
    }

    /// The wiring init installs — `auth.onSessionExpired` must actually
    /// reach `sessionDidExpire` (calling the method directly can't catch
    /// a missing/broken closure).
    func testOnSessionExpiredCallbackDropsReadyToLogin() {
        let model = AppModel()
        model.didLogin()
        XCTAssertEqual(model.phase, .ready)
        model.auth.onSessionExpired?()
        XCTAssertEqual(model.phase, .login)
    }
}
