import XCTest
@testable import Bookq

/// AppConfig URL policy — review fix: bearer tokens ride on every API
/// call, so plaintext http is only acceptable for loopback dev servers.
@MainActor
final class AppConfigTests: XCTestCase {
    private let key = "serverURL"

    override func setUp() async throws {
        UserDefaults.standard.removeObject(forKey: key)
    }

    override func tearDown() async throws {
        UserDefaults.standard.removeObject(forKey: key)
    }

    private func config(_ s: String) -> AppConfig {
        let c = AppConfig()
        c.serverURLString = s
        return c
    }

    func testHTTPSAccepted() {
        XCTAssertEqual(config("https://bookq.tail-x.ts.net").baseURL?.scheme,
                       "https")
        XCTAssertEqual(config("bookq.tail-x.ts.net").baseURL?.scheme,
                       "https")   // scheme-less defaults to https
    }

    /// Remote http is rejected — the token would travel in cleartext.
    func testRemoteHTTPRejected() {
        XCTAssertNil(config("http://bookq.tail-x.ts.net").baseURL)
        XCTAssertNil(config("http://192.168.1.10:8080").baseURL)
        XCTAssertNil(config("http://10.0.0.5").baseURL)
    }

    /// Loopback stays allowed for local dev servers.
    func testLoopbackHTTPAllowed() {
        XCTAssertEqual(config("http://localhost:8099").baseURL?.host,
                       "localhost")
        XCTAssertEqual(config("http://127.0.0.1:8080").baseURL?.host,
                       "127.0.0.1")
        XCTAssertEqual(config("http://[::1]:8080").baseURL?.host, "::1")
    }

    func testGarbageRejected() {
        XCTAssertNil(config("").baseURL)
        XCTAssertNil(config("ftp://x").baseURL)
    }
}
