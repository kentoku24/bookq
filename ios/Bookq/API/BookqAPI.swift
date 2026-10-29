import Foundation

/// HTTP client for the bookq API. Attaches `Authorization: Bearer` when a
/// token is present and handles the `/api/folders` 202 rebuild handshake.
final class BookqAPI: Sendable {
    let baseURL: URL
    private let session: URLSession
    private let tokenProvider: @Sendable () -> String?

    init(baseURL: URL, session: URLSession = .shared,
         tokenProvider: @escaping @Sendable () -> String?) {
        self.baseURL = baseURL
        self.session = session
        self.tokenProvider = tokenProvider
    }

    // MARK: - Requests

    private func makeRequest(path: String,
                             query: [URLQueryItem] = [],
                             method: String = "GET",
                             headers: [String: String] = [:]) -> URLRequest {
        var comps = URLComponents(
            url: baseURL.appendingPathComponent(path),
            resolvingAgainstBaseURL: false)!
        if !query.isEmpty { comps.queryItems = query }
        var req = URLRequest(url: comps.url!)
        req.httpMethod = method
        if let token = tokenProvider() {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        for (k, v) in headers { req.setValue(v, forHTTPHeaderField: k) }
        return req
    }

    private func send(_ req: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let (data, resp) = try await session.data(for: req)
        guard let http = resp as? HTTPURLResponse else {
            throw APIError.protocolError
        }
        return (data, http)
    }

    /// Decode JSON or map a non-2xx status to APIError.
    private func decode<T: Decodable>(_ type: T.Type,
                                      _ data: Data,
                                      _ resp: HTTPURLResponse) throws -> T {
        if resp.statusCode == 401 || resp.statusCode == 403 {
            throw APIError.unauthorized(status: resp.statusCode)
        }
        guard (200..<300).contains(resp.statusCode) else {
            throw APIError.status(resp.statusCode)
        }
        do {
            return try JSONDecoder().decode(type, from: data)
        } catch {
            throw APIError.decode(error)
        }
    }

    // MARK: - Endpoints

    /// GET /api/me — 401 means "login required" (or TEST_MODE off-server).
    func me() async throws -> MeResponse {
        let (data, resp) = try await send(makeRequest(path: "api/me"))
        return try decode(MeResponse.self, data, resp)
    }

    /// GET /api/health — unauthenticated liveness.
    func health() async throws {
        let (_, resp) = try await send(makeRequest(path: "api/health"))
        guard resp.statusCode == 200 else { throw APIError.status(resp.statusCode) }
    }

    func version() async throws -> VersionResponse {
        let (data, resp) = try await send(makeRequest(path: "api/version"))
        return try decode(VersionResponse.self, data, resp)
    }

    /// GET /api/folders. Retries through 202 rebuild responses using
    /// `Retry-After`. Without `since` returns the bare tree array; with
    /// `since` returns 304 or `{tree,generation}`.
    func folders(since generation: Int? = nil,
                 etag: String? = nil) async throws -> FoldersResult {
        var attempt = 0
        while true {
            var query: [URLQueryItem] = []
            if let generation {
                query.append(URLQueryItem(name: "since", value: String(generation)))
            }
            var headers: [String: String] = [:]
            if let etag { headers["If-None-Match"] = etag }
            let (data, resp) = try await send(makeRequest(
                path: "api/folders", query: query, headers: headers))
            switch resp.statusCode {
            case 202:
                attempt += 1
                guard attempt < 20 else { throw APIError.status(202) }
                let wait = Double(resp.value(forHTTPHeaderField: "Retry-After") ?? "2") ?? 2
                try await Task.sleep(nanoseconds: UInt64(wait * 1_000_000_000))
                continue
            case 304:
                return .notModified
            case 401, 403:
                throw APIError.unauthorized(status: resp.statusCode)
            case 200..<300:
                let payload = try JSONDecoder().decode(FoldersPayload.self, from: data)
                let newETag = resp.value(forHTTPHeaderField: "ETag")
                switch payload {
                case .full(let tree, _):
                    return .full(tree: tree, etag: newETag)
                case .delta(let tree, let gen):
                    return .delta(tree: tree, generation: gen, etag: newETag)
                }
            default:
                throw APIError.status(resp.statusCode)
            }
        }
    }

    /// GET /api/images?folder= — direct image entries of a leaf (files,
    /// archive entries or PDF pages). `folder` is a composed path.
    func images(folder: String) async throws -> [String] {
        let q = [URLQueryItem(name: "folder", value: folder)]
        let (data, resp) = try await send(makeRequest(path: "api/images", query: q))
        struct Resp: Decodable { var images: [String] }
        return try decode(Resp.self, data, resp).images
    }

    // MARK: - Image URLs

    /// Build a URL from an *already-encoded* path (via ImageURL.encodePath).
    /// `appendingPathComponent` would re-encode "%" → "%25", so use
    /// percentEncodedPath directly.
    private func encodedURL(_ encodedPath: String, query: [URLQueryItem] = []) -> URL {
        var comps = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
        var base = comps.percentEncodedPath
        if base == "/" { base = "" }
        comps.percentEncodedPath = base + "/" + encodedPath
        if !query.isEmpty { comps.queryItems = query }
        return comps.url!
    }

    func thumbURL(folder: String, name: String, w: Int = 400) -> URL {
        let rel = folder.isEmpty ? name : folder + "/" + name
        return encodedURL("thumb/" + ImageURL.encodePath(rel),
                          query: [URLQueryItem(name: "w", value: String(w))])
    }

    /// Multi-thumb composite sprite — one request for the whole tile grid.
    /// `names` are `thumbs[]` entries (each may contain "/"), `rows` comes
    /// from `BrowseModel.multiThumbSpec`.
    func thumbSpriteURL(folder: String, names: [String], rows: [Int], w: Int = 400) -> URL {
        var comps = URLComponents(
            url: baseURL.appendingPathComponent("api/thumb-sprite"),
            resolvingAgainstBaseURL: false)!
        let encodedNames = names.map(ImageURL.encodeSegment).joined(separator: ",")
        comps.queryItems = [
            URLQueryItem(name: "folder", value: folder),
            URLQueryItem(name: "w", value: String(w)),
            URLQueryItem(name: "v", value: "2"),
            URLQueryItem(name: "rows", value: rows.map(String.init).joined(separator: ",")),
        ]
        // names must be percent-encoded *inside* the query manually because
        // URLQueryItem would encode "," too.
        var s = comps.url!.absoluteString
        s += "&names=\(encodedNames)"
        return URL(string: s)!
    }

    /// Reader page URL — `/view/` (normalized) or `/image/` (original).
    func pageURL(folder: String, name: String, original: Bool) -> URL {
        let rel = folder.isEmpty ? name : folder + "/" + name
        return encodedURL("\(original ? "image" : "view")/" + ImageURL.encodePath(rel))
    }

    /// Absolute URL for an already-encoded API path + optional query.
    func absolute(_ encodedPath: String, query: [URLQueryItem] = []) -> URL {
        encodedURL(encodedPath, query: query)
    }

    /// Authenticated image fetch used by `RemoteImageView`.
    func imageData(url: URL) async throws -> Data {
        var req = URLRequest(url: url)
        if let token = tokenProvider() {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        let (data, resp) = try await send(req)
        guard resp.statusCode == 200 else {
            throw APIError.status(resp.statusCode)
        }
        return data
    }

    // MARK: - Reading position / visibility

    func readingPosition() async throws -> ReadingPosition? {
        let (data, resp) = try await send(makeRequest(path: "api/reading-position"))
        return try decode(PositionEnvelope.self, data, resp).position
    }

    func putReadingPosition(_ pos: ReadingPosition) async throws {
        var req = makeRequest(path: "api/reading-position", method: "PUT")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONEncoder().encode(pos)
        let (_, resp) = try await send(req)
        guard (200..<300).contains(resp.statusCode) else {
            throw APIError.status(resp.statusCode)
        }
    }

    func folderVisibility() async throws -> VisibilitySnapshot {
        let (data, resp) = try await send(makeRequest(path: "api/folder-visibility"))
        return try decode(VisibilitySnapshot.self, data, resp)
    }

    /// POST /api/refresh — full metadata rebuild.
    func refresh() async throws {
        let (_, resp) = try await send(makeRequest(path: "api/refresh", method: "POST"))
        guard (200..<300).contains(resp.statusCode) else {
            throw APIError.status(resp.statusCode)
        }
    }
}

enum FoldersResult {
    case full(tree: [TreeNode], etag: String?)
    case delta(tree: [TreeNode], generation: Int, etag: String?)
    case notModified
}

enum APIError: LocalizedError {
    case unauthorized(status: Int)
    case status(Int)
    case decode(Error)
    case protocolError

    var errorDescription: String? {
        switch self {
        case .unauthorized(let s): return "認証が必要です (HTTP \(s))"
        case .status(let s): return "サーバーエラー (HTTP \(s))"
        case .decode(let e): return "レスポンス解析エラー: \(e.localizedDescription)"
        case .protocolError: return "不正なレスポンス"
        }
    }
}
