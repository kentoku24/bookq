import Foundation
@testable import Bookq

/// URLProtocol stub — routes every request through a test handler and
/// records (request, body) pairs for wire-level assertions. Ephemeral
/// session only; `.shared` is untouched.
final class StubURLProtocol: URLProtocol {
    struct Response {
        var status: Int
        var body: Data
        var headers: [String: String] = [:]
    }
    struct Rec {
        var request: URLRequest
        var body: Data?
    }

    nonisolated(unsafe) static var handler: ((URLRequest) -> Response)?
    nonisolated(unsafe) static var requests: [Rec] = []

    static func reset() {
        handler = nil
        requests = []
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.requests.append(Rec(request: request, body: Self.body(of: request)))
        let r = Self.handler?(request)
            ?? Response(status: 500, body: Data("no stub".utf8))
        let http = HTTPURLResponse(url: request.url!, statusCode: r.status,
                                   httpVersion: nil, headerFields: r.headers)!
        client?.urlProtocol(self, didReceive: http, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: r.body)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}

    /// URLSession hands bodies over as a stream — flatten for assertions.
    private static func body(of req: URLRequest) -> Data? {
        if let b = req.httpBody { return b }
        guard let s = req.httpBodyStream else { return nil }
        s.open()
        defer { s.close() }
        var out = Data()
        var buf = [UInt8](repeating: 0, count: 4096)
        while s.hasBytesAvailable {
            let n = s.read(&buf, maxLength: buf.count)
            if n <= 0 { break }
            out.append(contentsOf: buf[..<n])
        }
        return out
    }

    // MARK: - Conveniences

    static func session() -> URLSession {
        let c = URLSessionConfiguration.ephemeral
        c.protocolClasses = [StubURLProtocol.self]
        c.urlCache = nil
        return URLSession(configuration: c)
    }

    static func api(host: String = "unit.test") -> BookqAPI {
        BookqAPI(baseURL: URL(string: "http://\(host)")!,
                 session: session(), tokenProvider: { nil })
    }

    static func json(_ obj: Any, status: Int = 200,
                     headers: [String: String] = [:]) -> Response {
        Response(status: status,
                 body: try! JSONSerialization.data(withJSONObject: obj),
                 headers: headers)
    }

    static func last(path: String) -> Rec? {
        requests.last { $0.request.url?.path == "/\(path)" }
    }

    static func hits(path: String) -> Int {
        requests.filter { $0.request.url?.path == "/\(path)" }.count
    }

    static func decodedBody(_ rec: Rec?) -> [String: Any]? {
        guard let d = rec?.body else { return nil }
        return try? JSONSerialization.jsonObject(with: d) as? [String: Any]
    }
}
