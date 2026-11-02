import XCTest
@testable import Bookq

/// PairJoinCode.extract — the scanned-QR payload parser. The leader's QR
/// carries `<origin>/#/join/<code>` (pairUI.js / QRImage parity); bare
/// 6-digit codes are accepted for QR generators that skip the URL.
final class PairJoinCodeTests: XCTestCase {

    func testWebJoinLink() {
        XCTAssertEqual(
            PairJoinCode.extract("https://bookq.example.ts.net/#/join/123456"),
            "123456")
    }

    func testJoinPathWithoutHash() {
        XCTAssertEqual(
            PairJoinCode.extract("https://host/join/654321"), "654321")
    }

    func testBareCode() {
        XCTAssertEqual(PairJoinCode.extract("123456"), "123456")
    }

    func testRejectsNonPairPayloads() {
        XCTAssertNil(PairJoinCode.extract("https://host/"))
        XCTAssertNil(PairJoinCode.extract("https://host/#/browse/"))
        XCTAssertNil(PairJoinCode.extract("hello world"))
        XCTAssertNil(PairJoinCode.extract("12345"))
        XCTAssertNil(PairJoinCode.extract("1234567"))
        XCTAssertNil(PairJoinCode.extract(""))
        XCTAssertNil(PairJoinCode.extract("/join/12345"))
        XCTAssertNil(PairJoinCode.extract("/join/abcdef"))
        // 7-digit suffix after /join/ must not yield a truncated code.
        XCTAssertNil(PairJoinCode.extract("https://host/#/join/1234567"))
    }

    // MARK: - sameServer

    private func url(_ s: String) -> URL? { URL(string: s) }

    func testSameServerMatchesHostAndPort() {
        let mine = url("https://bookq.example.ts.net")
        XCTAssertTrue(PairJoinCode.sameServer(
            payload: "https://bookq.example.ts.net/#/join/123456",
            baseURL: mine))
    }

    func testSameServerRejectsDifferentHost() {
        XCTAssertFalse(PairJoinCode.sameServer(
            payload: "https://other.example.ts.net/#/join/123456",
            baseURL: url("https://bookq.example.ts.net")))
    }

    func testSameServerRejectsDifferentPort() {
        XCTAssertFalse(PairJoinCode.sameServer(
            payload: "http://localhost:9999/#/join/123456",
            baseURL: url("http://localhost:8080")))
        XCTAssertTrue(PairJoinCode.sameServer(
            payload: "http://localhost:8080/#/join/123456",
            baseURL: url("http://localhost:8080")))
    }

    func testSameServerDefaultPortNormalized() {
        // :443 explicit vs bare https host — same origin.
        XCTAssertTrue(PairJoinCode.sameServer(
            payload: "https://bookq.example.ts.net:443/#/join/123456",
            baseURL: url("https://bookq.example.ts.net")))
        XCTAssertFalse(PairJoinCode.sameServer(
            payload: "https://bookq.example.ts.net:8443/#/join/123456",
            baseURL: url("https://bookq.example.ts.net")))
    }

    func testSameServerBareCodeAndMissingBase() {
        XCTAssertTrue(PairJoinCode.sameServer(
            payload: "123456", baseURL: url("https://bookq.example.ts.net")))
        XCTAssertTrue(PairJoinCode.sameServer(
            payload: "https://other.example/#/join/123456", baseURL: nil))
    }
}
