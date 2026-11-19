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

    /// `-uitestReset` wipe — progress (all scopes), pair-sync snapshots
    /// and layout prefs go; `serverURL` (re-injected per launch) stays.
    func testUITestResetWipesPersistedKeys() {
        let suite = "AppModelTests.uitestReset.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        defaults.set("x", forKey: "bookq_progress_v1")
        defaults.set("x", forKey: "bookq_progress_v1:host|a@b.c")
        defaults.set("x", forKey: "bookq.pairSync.v1.example.com")
        defaults.set(true, forKey: "originalImages")
        defaults.set("list", forKey: "browseLayout")
        defaults.set("http://localhost:8080", forKey: "serverURL")
        AppModel.wipeUITestState(in: defaults)
        for key in ["bookq_progress_v1", "bookq_progress_v1:host|a@b.c",
                    "bookq.pairSync.v1.example.com", "originalImages",
                    "browseLayout"] {
            XCTAssertNil(defaults.object(forKey: key), key)
        }
        XCTAssertEqual(defaults.string(forKey: "serverURL"),
                       "http://localhost:8080")
    }

    /// `-uitestReset` also clears Documents/ — offline download scopes
    /// (`works.json` + files) live there, outside UserDefaults.
    func testUITestResetWipesOfflineDownloads() throws {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("uitest-wipe-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir,
                                                withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let scope = dir.appendingPathComponent("127.0.0.1%3A8080|user")
        try FileManager.default.createDirectory(
            at: scope.appendingPathComponent("nested"),
            withIntermediateDirectories: true)
        try Data("{}".utf8).write(to: scope.appendingPathComponent("works.json"))
        try Data("img".utf8).write(
            to: scope.appendingPathComponent("nested/p00.jpg"))
        AppModel.wipeUITestOfflineDownloads(in: dir)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(
            atPath: dir.path), [])
    }
}
