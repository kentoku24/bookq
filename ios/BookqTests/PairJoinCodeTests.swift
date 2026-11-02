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
    }
}
