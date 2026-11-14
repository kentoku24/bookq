import Foundation

/// HTTP client for the bookq API. Attaches `Authorization: Bearer` when a
/// token is present and handles the `/api/v1/tree` 202 rebuild handshake.
/// Connectivity verdict hook (docs/offline-spec.md §5 iOS): `send`
/// classifies every request outcome and reports it here. 2xx → success;
/// unreachable URLErrors and 502/503/504 → fail; anything else (4xx,
/// 500, decode errors, cancellations) is neutral — the server answered.
struct ConnectivityReporter: Sendable {
    var success: @Sendable () -> Void
    var unreachable: @Sendable () -> Void
}

final class BookqAPI: Sendable {
    let baseURL: URL
    private let session: URLSession
    private let tokenProvider: @Sendable () -> String?
    /// 401 recovery: refresh the access token and return the new Bearer.
    /// Nil return = refresh failed → the 401 propagates (→ re-login).
    private let tokenRefresher: (@Sendable () async -> String?)?
    /// Verdict sink — nil in tests/harnesses that don't own a state machine.
    private let reporter: ConnectivityReporter?

    init(baseURL: URL, session: URLSession = .shared,
         tokenProvider: @escaping @Sendable () -> String?,
         tokenRefresher: (@Sendable () async -> String?)? = nil,
         reporter: ConnectivityReporter? = nil) {
        self.baseURL = baseURL
        self.session = session
        self.tokenProvider = tokenProvider
        self.tokenRefresher = tokenRefresher
        self.reporter = reporter
    }

    /// `ws(s)://<host>/ws/reader-sync` — the dual-reader sync endpoint.
    /// Bearer auth rides the upgrade request (server accepts it on the WS
    /// handshake exactly like HTTP routes).
    var readerSyncURL: URL? {
        guard var comps = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)
        else { return nil }
        switch comps.scheme {
        case "https": comps.scheme = "wss"
        case "http": comps.scheme = "ws"
        default: return nil
        }
        comps.path = "/ws/reader-sync"
        comps.query = nil
        return comps.url
    }

    // MARK: - Requests

    /// Query-value charset: RFC 3986 unreserved only. `URLQueryItem` leaves
    /// "+" literal and form-decoders (Starlette parse_qsl) read it as a
    /// space — folder paths containing "+" must arrive as %2B (web parity:
    /// encodeURIComponent). `percentEncodedQuery` takes the string
    /// verbatim, so encode once here; assigning `query` would re-encode %.
    private static let queryValueAllowed: CharacterSet = {
        var s = CharacterSet.alphanumerics
        s.insert(charactersIn: "-._~")
        return s
    }()

    /// Serialize query items with strict percent-encoding.
    private func applyQuery(_ comps: inout URLComponents, _ items: [URLQueryItem]) {
        guard !items.isEmpty else { return }
        comps.percentEncodedQuery = items.map { item in
            let name = item.name.addingPercentEncoding(
                withAllowedCharacters: Self.queryValueAllowed) ?? item.name
            guard let value = item.value else { return name }
            let v = value.addingPercentEncoding(
                withAllowedCharacters: Self.queryValueAllowed) ?? value
            return "\(name)=\(v)"
        }.joined(separator: "&")
    }

    private func makeRequest(path: String,
                             query: [URLQueryItem] = [],
                             method: String = "GET",
                             headers: [String: String] = [:]) -> URLRequest {
        var comps = URLComponents(
            url: baseURL.appendingPathComponent(path),
            resolvingAgainstBaseURL: false)!
        applyQuery(&comps, query)
        var req = URLRequest(url: comps.url!)
        req.httpMethod = method
        // Bearer-authed JSON must never replay from URLCache — RFC 7231
        // heuristically caches error responses, and a stale 404 caught
        // during a server restart poisons later boots until it expires.
        req.cachePolicy = .reloadIgnoringLocalCacheData
        if let token = tokenProvider() {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        for (k, v) in headers { req.setValue(v, forHTTPHeaderField: k) }
        return req
    }

    private func send(_ req: URLRequest) async throws -> (Data, HTTPURLResponse) {
        var (data, resp) = try await sendOnce(req)
        guard var http = resp as? HTTPURLResponse else {
            throw APIError.protocolError
        }
        // RFC 6750: an expired access token answers 401 — refresh once and
        // replay the request with the rotated Bearer (refresh rotation is
        // serialized inside AuthManager, so parallel 401s coalesce).
        if http.statusCode == 401, let tokenRefresher,
           let fresh = await tokenRefresher() {
            var retry = req
            retry.setValue("Bearer \(fresh)", forHTTPHeaderField: "Authorization")
            (data, resp) = try await sendOnce(retry)
            guard let h2 = resp as? HTTPURLResponse else {
                throw APIError.protocolError
            }
            http = h2
        }
        return (data, http)
    }

    /// One network leg + spec §5 verdict reporting. The classification is
    /// per *outcome*: an unreachable URLError or a proxy-band 5xx means
    /// the server did not answer; any other status is a real response
    /// (neutral), and 2xx proves reachability.
    private func sendOnce(_ req: URLRequest) async throws -> (Data, URLResponse) {
        do {
            let (data, resp) = try await session.data(for: req)
            if let http = resp as? HTTPURLResponse {
                if (200..<300).contains(http.statusCode) {
                    reporter?.success()
                } else if [502, 503, 504].contains(http.statusCode) {
                    reporter?.unreachable()
                }
            }
            return (data, resp)
        } catch let e as URLError {
            if Self.isUnreachable(e) { reporter?.unreachable() }
            throw e
        }
    }

    /// spec §5 iOS: the no-response error set (parity with web TypeError /
    /// SW `Response.error()`). `.cancelled` is intentionally absent —
    /// aborts are caller-driven, never connectivity evidence.
    static func isUnreachable(_ e: URLError) -> Bool {
        switch e.code {
        case .notConnectedToInternet, .timedOut, .cannotConnectToHost,
             .networkConnectionLost, .cannotFindHost, .dnsLookupFailed:
            return true
        default:
            return false
        }
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

    /// GET /api/v1/health — unauthenticated liveness.
    /// `onlineCheck` adds web parity markers (`?online_check=1` +
    /// `X-Online-Check: 1`): the web Service Worker (sw.js) detects
    /// them and bypasses its NetworkFirst cache so the probe really
    /// hits the network — native has no SW but keeps the wire shape.
    func health(onlineCheck: Bool = false) async throws {
        var query: [URLQueryItem] = []
        var headers: [String: String] = [:]
        if onlineCheck {
            query.append(URLQueryItem(name: "online_check", value: "1"))
            headers["X-Online-Check"] = "1"
        }
        let (_, resp) = try await send(makeRequest(
            path: "api/v1/health", query: query, headers: headers))
        // web parity: fetch `r.ok` is the whole 2xx band.
        guard (200..<300).contains(resp.statusCode) else {
            throw APIError.status(resp.statusCode)
        }
    }

    /// GET /api/v1/version — also the boot auth probe: 401 = login needed.
    func version() async throws -> VersionResponse {
        let (data, resp) = try await send(makeRequest(path: "api/v1/version"))
        return try decode(VersionResponse.self, data, resp)
    }

    /// GET /api/v1/tree. Retries through 202 rebuild responses using
    /// `Retry-After`. All 200 bodies are the {kind,nodes,generation}
    /// envelope — a `since` mismatch re-sends kind="tree" with the new
    /// generation, so callers always get a generation without parsing ETag.
    func tree(since generation: Int? = nil,
              etag: String? = nil) async throws -> TreeResult {
        var attempt = 0
        while true {
            var query: [URLQueryItem] = []
            if let generation {
                query.append(URLQueryItem(name: "since", value: String(generation)))
            }
            var headers: [String: String] = [:]
            if let etag { headers["If-None-Match"] = etag }
            let (data, resp) = try await send(makeRequest(
                path: "api/v1/tree", query: query, headers: headers))
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
                do {
                    let env = try JSONDecoder().decode(TreeResponse.self, from: data)
                    let newETag = resp.value(forHTTPHeaderField: "ETag")
                    return .nodes(kind: env.kind, nodes: env.nodes,
                                  generation: env.generation, etag: newETag)
                } catch {
                    throw APIError.decode(error)
                }
            default:
                throw APIError.status(resp.statusCode)
            }
        }
    }

    /// GET /api/v1/images?folder= — direct image entries of a leaf (files,
    /// archive entries or PDF pages). `folder` is a composed path.
    func images(folder: String) async throws -> [String] {
        let q = [URLQueryItem(name: "folder", value: folder)]
        let (data, resp) = try await send(makeRequest(path: "api/v1/images", query: q))
        return try decode(ImagesResponse.self, data, resp).images
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
        applyQuery(&comps, query)
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
            url: baseURL.appendingPathComponent("api/v1/thumb-sprite"),
            resolvingAgainstBaseURL: false)!
        let encodedNames = names.map(ImageURL.encodeSegment).joined(separator: ",")
        applyQuery(&comps, [
            URLQueryItem(name: "folder", value: folder),
            URLQueryItem(name: "w", value: String(w)),
            URLQueryItem(name: "v", value: "2"),
            URLQueryItem(name: "rows", value: rows.map(String.init).joined(separator: ",")),
        ])
        // names must be percent-encoded *inside* the query manually because
        // URLQueryItem would encode "," too.
        var s = comps.url!.absoluteString
        s += "&names=\(encodedNames)"
        return URL(string: s)!
    }

    /// Reader page URL — `/view/` (normalized) or `/image/` (original).
    /// `width` requests the `/view/?w=` fast-tier rendition (ignored for
    /// `original`, which has no server-side resize).
    func pageURL(folder: String, name: String, original: Bool,
                 width: Int? = nil) -> URL {
        let rel = folder.isEmpty ? name : folder + "/" + name
        var query: [URLQueryItem] = []
        if !original, let width {
            query = [URLQueryItem(name: "w", value: String(width))]
        }
        return encodedURL("\(original ? "image" : "view")/" + ImageURL.encodePath(rel),
                          query: query)
    }

    /// Absolute URL for an already-encoded API path + optional query.
    func absolute(_ encodedPath: String, query: [URLQueryItem] = []) -> URL {
        encodedURL(encodedPath, query: query)
    }

    /// Authenticated image fetch used by `RemoteImageView` and the
    /// offline downloader. Reader pages and archive entries can be tens of
    /// MB, and solid-RAR page extraction decompresses every preceding entry
    /// server-side — far beyond the 60s default request timeout.
    func imageData(url: URL) async throws -> Data {
        var req = URLRequest(url: url)
        req.cachePolicy = .reloadIgnoringLocalCacheData
        req.timeoutInterval = 300
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

    /// GET /api/v1/reading-positions — `{positions[]}` newest-first;
    /// `folder` scopes to one work.
    func readingPositions(folder: String? = nil) async throws -> [ReadingPosition] {
        var q: [URLQueryItem] = []
        if let folder { q.append(URLQueryItem(name: "folder", value: folder)) }
        let (data, resp) = try await send(
            makeRequest(path: "api/v1/reading-positions", query: q))
        return try decode(PositionsResponse.self, data, resp).positions
    }

    /// PUT /api/v1/reading-positions?folder=… — `folder` rides the query,
    /// body is exactly {page,total,ts}. 2xx = accepted ("ok"/"stale" is
    /// informational — web treats any 2xx as sent); decode best-effort.
    func putReadingPosition(_ pos: ReadingPosition) async throws -> PositionStatus? {
        var req = makeRequest(path: "api/v1/reading-positions",
                              query: [URLQueryItem(name: "folder", value: pos.folder)],
                              method: "PUT")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONEncoder().encode(
            PositionBody(page: pos.page, total: pos.total, ts: pos.ts))
        let (data, resp) = try await send(req)
        if resp.statusCode == 401 || resp.statusCode == 403 {
            throw APIError.unauthorized(status: resp.statusCode)
        }
        guard (200..<300).contains(resp.statusCode) else {
            throw APIError.status(resp.statusCode)
        }
        return try? JSONDecoder().decode(PositionStatus.self, from: data)
    }

    /// DELETE /api/v1/reading-positions — `folder` nil wipes all records.
    func deleteReadingPosition(folder: String? = nil) async throws {
        var q: [URLQueryItem] = []
        if let folder { q.append(URLQueryItem(name: "folder", value: folder)) }
        let (_, resp) = try await send(
            makeRequest(path: "api/v1/reading-positions", query: q, method: "DELETE"))
        guard (200..<300).contains(resp.statusCode) else {
            throw APIError.status(resp.statusCode)
        }
    }

    func folderVisibility() async throws -> VisibilitySnapshot {
        let (data, resp) = try await send(makeRequest(path: "api/v1/folder-visibility"))
        return try decode(VisibilitySnapshot.self, data, resp)
    }

    /// PUT /api/v1/folder-visibility — body exactly {root,hidden}, owner proof
    /// via `X-Bookq-Visibility-Owner`. 409 = owner mismatch (stale account),
    /// 404 = root not a current top-level folder (hide only). The response
    /// is the authoritative post-write snapshot.
    func setFolderVisibility(root: String, hidden: Bool, owner: String) async throws -> VisibilitySnapshot {
        var req = makeRequest(path: "api/v1/folder-visibility", method: "PUT")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(owner, forHTTPHeaderField: "X-Bookq-Visibility-Owner")
        struct Body: Encodable { var root: String; var hidden: Bool }
        req.httpBody = try JSONEncoder().encode(Body(root: root, hidden: hidden))
        let (data, resp) = try await send(req)
        return try decode(VisibilitySnapshot.self, data, resp)
    }

    // MARK: - Search / facets

    /// `_fetchBuilding` equivalent (search/facets.js): 202 index-building
    /// responses retry on `min(1500*(n+1),5000)` backoff, max 10 attempts.
    /// Staleness is the caller's generation guard — Task cancellation also
    /// aborts a retry loop mid-flight.
    private func fetchBuilding(path: String, query: [URLQueryItem])
        async throws -> (Data, HTTPURLResponse)? {
        for attempt in 0..<10 {
            try Task.checkCancellation()
            let (data, resp) = try await send(makeRequest(path: path, query: query))
            try Task.checkCancellation()
            if resp.statusCode != 202 { return (data, resp) }
            let wait = min(1.5 * Double(attempt + 1), 5.0)
            try await Task.sleep(nanoseconds: UInt64(wait * 1_000_000_000))
        }
        return nil
    }

    /// GET /api/v1/search — `doSearch` equivalent. Filters: artist/tag comma-
    /// joined (server ANDs keys, ORs within a key), sort fixed relevance.
    /// `includeHidden` opts out of the caller's hidden-root exclusion
    /// (owner-scoped server-side — never another user's set). nil = still
    /// building after 10 attempts or cancelled.
    func search(q: String, artist: [String], tag: [String],
                includeHidden: Bool) async throws -> SearchResponse? {
        var query = [URLQueryItem(name: "q", value: q)]
        if !artist.isEmpty { query.append(URLQueryItem(name: "artist", value: artist.joined(separator: ","))) }
        if !tag.isEmpty { query.append(URLQueryItem(name: "tag", value: tag.joined(separator: ","))) }
        query.append(URLQueryItem(name: "sort", value: "relevance"))
        if includeHidden { query.append(URLQueryItem(name: "include_hidden", value: "1")) }
        guard let (data, resp) = try await fetchBuilding(
            path: "api/v1/search", query: query) else { return nil }
        return try decode(SearchResponse.self, data, resp)
    }

    /// GET /api/v1/facets — `loadFacets` equivalent. `path` scopes to
    /// a subtree ("" = whole library).
    func facets(path: String = "", includeHidden: Bool) async throws -> Facets? {
        var query: [URLQueryItem] = []
        if !path.isEmpty { query.append(URLQueryItem(name: "path", value: path)) }
        if includeHidden { query.append(URLQueryItem(name: "include_hidden", value: "1")) }
        guard let (data, resp) = try await fetchBuilding(
            path: "api/v1/facets", query: query) else { return nil }
        return try decode(Facets.self, data, resp)
    }

    /// POST /api/v1/index/refresh — synchronous metadata rebuild.
    func refresh() async throws {
        let (_, resp) = try await send(makeRequest(path: "api/v1/index/refresh", method: "POST"))
        guard (200..<300).contains(resp.statusCode) else {
            throw APIError.status(resp.statusCode)
        }
    }
}

enum TreeResult {
    /// `kind` is "tree" (full) or "children" (shallow); generation always
    /// present in v1 envelopes.
    case nodes(kind: String, nodes: [TreeNode], generation: Int, etag: String?)
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
