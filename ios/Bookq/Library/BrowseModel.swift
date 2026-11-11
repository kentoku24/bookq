import Foundation

/// Pure browse/lookup rules mirroring `app/static/browse/render.js`.
/// All functions are free of UI state and unit-tested.
enum BrowseModel {

    /// Composed absolute path for a node (`parent + "/" + name`, JS `_fullPath`).
    static func fullPath(_ parent: String, _ name: String) -> String {
        parent.isEmpty ? name : parent + "/" + name
    }

    /// `folderHasOnlyImages`: children exist and every child is type "image".
    static func hasOnlyImages(_ node: TreeNode) -> Bool {
        guard let children = node.children, !children.isEmpty else { return false }
        return children.allSatisfy { $0.isImage }
    }

    /// `folderHasImages`: at least one direct image child.
    static func hasImages(_ node: TreeNode) -> Bool {
        node.children?.contains(where: { $0.isImage }) ?? false
    }

    /// Mixed folder = drills to subfolders but also has its own images
    /// (those get a "read self" tile).
    static func isMixed(_ node: TreeNode) -> Bool {
        hasImages(node) && !hasOnlyImages(node)
    }

    /// Look up a node by composed path — mirrors `_find_tree_node` semantics:
    /// walk the tree comparing composed `full + "/" + name` against target.
    /// Node names may contain "/" and are NEVER split.
    static func node(at path: String, in roots: [TreeNode]) -> TreeNode? {
        if path.isEmpty { return nil }
        for node in roots where node.name == path {
            return node
        }
        for node in roots {
            let full = node.name
            if path.hasPrefix(full + "/") {
                if let found = find(path, in: node.children ?? [], parent: full) {
                    return found
                }
            }
        }
        return nil
    }

    private static func find(_ path: String, in nodes: [TreeNode], parent: String) -> TreeNode? {
        for node in nodes {
            let full = parent + "/" + node.name
            if full == path { return node }
            if path.hasPrefix(full + "/"),
               let found = find(path, in: node.children ?? [], parent: full) {
                return found
            }
        }
        return nil
    }

    /// Folder children of a level (drill targets, in tree order).
    static func folders(at path: String, in roots: [TreeNode]) -> [(path: String, node: TreeNode)] {
        let children = path.isEmpty ? roots : (node(at: path, in: roots)?.children ?? [])
        return children.filter(\.isFolder).map { (fullPath(path, $0.name), $0) }
    }

    /// Tiles shown at a level: each mixed child gets a "read self" tile,
    /// plus at root one tile covering loose root images.
    static func selfTiles(at path: String, in roots: [TreeNode]) -> [(path: String, node: TreeNode)] {
        if path.isEmpty {
            let hasLoose = roots.contains { $0.isImage }
            return hasLoose ? [("", TreeNode(name: "", type: "folder"))] : []
        }
        return folders(at: path, in: roots).filter { isMixed($0.node) }
    }

    /// Whether tapping this folder drills down vs opens the reader.
    static func drillsDown(_ node: TreeNode) -> Bool {
        !hasOnlyImages(node)
    }

    // MARK: - Multi-thumb layout (`render.js multiThumbSpec`)

    struct ThumbSpec: Equatable {
        var rows: [Int]
        var cols: Int
    }

    /// Row layout for a multi-thumb tile — same table the server sprite uses.
    static func multiThumbSpec(_ count: Int) -> ThumbSpec? {
        let table: [Int: ThumbSpec] = [
            2: ThumbSpec(rows: [2], cols: 2),
            3: ThumbSpec(rows: [1, 2], cols: 2),
            4: ThumbSpec(rows: [2, 2], cols: 2),
            5: ThumbSpec(rows: [2, 3], cols: 6),
            6: ThumbSpec(rows: [2, 2, 2], cols: 2),
            7: ThumbSpec(rows: [2, 3, 2], cols: 6),
            8: ThumbSpec(rows: [3, 2, 3], cols: 6),
            9: ThumbSpec(rows: [3, 3, 3], cols: 3),
        ]
        return table[min(count, 9)]
    }

    /// Split "parent/leaf" display names (flattened folders show parent dimmed).
    static func splitDisplayName(_ name: String) -> (leaf: String, parent: String?) {
        guard let idx = name.lastIndex(of: "/") else { return (name, nil) }
        return (String(name[name.index(after: idx)...]),
                String(name[..<idx]))
    }

    /// Filter a level's folder entries by lowercase substring (search-in-level).
    static func filter(_ entries: [(path: String, node: TreeNode)], query: String) -> [(path: String, node: TreeNode)] {
        let q = query.lowercased()
        guard !q.isEmpty else { return entries }
        return entries.filter {
            $0.node.name.lowercased().contains(q) || $0.path.lowercased().contains(q)
        }
    }

    // MARK: - Grid columns

    /// Column count for the browse grid's row-chunked layout — mirrors
    /// LazyVGrid `.adaptive(minimum:spacing:)`: the largest n where
    /// `n*minimum + (n-1)*spacing <= availableWidth`. Row layouts
    /// (list/card) are always single-column.
    static func gridColumns(isTileLayout: Bool, minimum: CGFloat,
                            availableWidth: CGFloat,
                            spacing: CGFloat = 8) -> Int {
        guard isTileLayout, availableWidth > 0 else { return 1 }
        return max(1, Int((availableWidth + spacing) / (minimum + spacing)))
    }
}
