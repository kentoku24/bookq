import XCTest
@testable import Bookq

/// JS encodeURIComponent parity — the same cases the web client produces.
final class ImageURLTests: XCTestCase {
    func testSegmentBasic() {
        XCTAssertEqual(ImageURL.encodeSegment("abc"), "abc")
        XCTAssertEqual(ImageURL.encodeSegment("a b"), "a%20b")
        XCTAssertEqual(ImageURL.encodeSegment("a+b"), "a%2Bb")
        XCTAssertEqual(ImageURL.encodeSegment("a/b"), "a%2Fb")
        XCTAssertEqual(ImageURL.encodeSegment("a#b"), "a%23b")
        XCTAssertEqual(ImageURL.encodeSegment("a?b"), "a%3Fb")
        XCTAssertEqual(ImageURL.encodeSegment("a%b"), "a%25b")
    }

    func testUnreservedPunctuationPassThrough() {
        XCTAssertEqual(ImageURL.encodeSegment("-_.!~*'()"), "-_.!~*'()")
    }

    func testJapanese() {
        XCTAssertEqual(ImageURL.encodeSegment("第1話"), "%E7%AC%AC1%E8%A9%B1")
    }

    func testPathKeepsSeparators() {
        XCTAssertEqual(ImageURL.encodePath("a b/c"), "a%20b/c")
        XCTAssertEqual(ImageURL.encodePath("作品名/第1巻"),
                       "%E4%BD%9C%E5%93%81%E5%90%8D/%E7%AC%AC1%E5%B7%BB")
        // flattened names ("parent/child") become two segments — same as JS
        XCTAssertEqual(ImageURL.encodePath("親/子"), "%E8%A6%AA/%E5%AD%90")
        // empty segments preserved (leading/trailing slash)
        XCTAssertEqual(ImageURL.encodePath("/a/"), "/a/")
        XCTAssertEqual(ImageURL.encodePath(""), "")
    }
}
