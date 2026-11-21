import Foundation

/// One node of the `/api/v1/tree` tree. `name` may itself contain "/"
/// (single-child flattening, `app/scanner.py`) — never split on it.
///
/// Lives in BookqCore (not the app's APIModels) because the pure browse
/// and reader rules (`BrowseModel`, `ReaderWork`) and the conformance
/// vectors all decode it. Moved verbatim from `ios/Bookq/API/APIModels.swift`.
public struct TreeNode: Codable, Hashable {
    public var name: String
    public var type: String            // "folder" | "image"
    public var children: [TreeNode]? = nil
    public var thumb: String? = nil
    public var thumbs: [String]? = nil
    public var archive: Bool? = nil
    public var pdf: Bool? = nil
    public var hasMetadata: Bool? = nil
    public var metadata: ComicMetadata? = nil
    public var subtreeFacets: [String: [String]]? = nil
    // Server-resolved fields (T03, additive optional — old clients
    // ignore them; absent until the server emits them).
    public var path: String? = nil
    public var coverUrl: String? = nil
    public var coverUrls: [String]? = nil
    public var kind: String? = nil        // "work" | "shelf" | "mixed"
    public var work: String? = nil
    public var hidden: Bool? = nil

    public var isFolder: Bool { type == "folder" }
    public var isImage: Bool { type == "image" }

    public init(name: String, type: String, children: [TreeNode]? = nil,
                thumb: String? = nil, thumbs: [String]? = nil,
                archive: Bool? = nil, pdf: Bool? = nil,
                hasMetadata: Bool? = nil, metadata: ComicMetadata? = nil,
                subtreeFacets: [String: [String]]? = nil,
                path: String? = nil, coverUrl: String? = nil,
                coverUrls: [String]? = nil, kind: String? = nil,
                work: String? = nil, hidden: Bool? = nil) {
        self.name = name
        self.type = type
        self.children = children
        self.thumb = thumb
        self.thumbs = thumbs
        self.archive = archive
        self.pdf = pdf
        self.hasMetadata = hasMetadata
        self.metadata = metadata
        self.subtreeFacets = subtreeFacets
        self.path = path
        self.coverUrl = coverUrl
        self.coverUrls = coverUrls
        self.kind = kind
        self.work = work
        self.hidden = hidden
    }

    enum CodingKeys: String, CodingKey {
        case name, type, children, thumb, thumbs, archive, pdf, metadata
        case path, coverUrl, coverUrls, kind, work, hidden
        case hasMetadata = "has_metadata"
        case subtreeFacets = "subtree_facets"
    }
}

public struct ComicMetadata: Codable, Hashable {
    public var folder_path: String = ""
    public var folder_name: String = ""
    public var title_jp: String = ""
    public var title_en: String = ""
    public var artist: [String] = []
    public var tags: [String] = []
    public var source: String = ""
    public var has_metadata: Bool = false
    public var total_pages: Int = 0

    public var displayTitle: String {
        !title_jp.isEmpty ? title_jp : (!title_en.isEmpty ? title_en : folder_name)
    }

    private enum CodingKeys: String, CodingKey {
        case folder_path, folder_name, title_jp, title_en
        case artist, tags, source, has_metadata, total_pages
    }

    // Spec marks title_jp/title_en optional — tolerate their absence.
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        folder_path = try c.decode(String.self, forKey: .folder_path)
        folder_name = try c.decode(String.self, forKey: .folder_name)
        title_jp = try c.decodeIfPresent(String.self, forKey: .title_jp) ?? ""
        title_en = try c.decodeIfPresent(String.self, forKey: .title_en) ?? ""
        artist = try c.decode([String].self, forKey: .artist)
        tags = try c.decode([String].self, forKey: .tags)
        source = try c.decode(String.self, forKey: .source)
        has_metadata = try c.decode(Bool.self, forKey: .has_metadata)
        total_pages = try c.decode(Int.self, forKey: .total_pages)
    }
}
