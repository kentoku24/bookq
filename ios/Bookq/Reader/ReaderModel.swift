import Foundation

/// Pure reader model — chapter assembly + spread construction, byte-faithful
/// to `app/static/app.js` `loadImages` and `app/static/viewer/spread.js`.
/// Deterministic and unit-tested.
struct PageRef: Equatable, Hashable {
    var name: String
    var folder: String       // leaf folder path this page lives in
    var isSpacer: Bool = false

    static let spacer = PageRef(name: "__spacer__", folder: "", isSpacer: true)
}

struct ReaderChapter: Equatable {
    var name: String
    var fullPath: String
    var images: [PageRef]    // real (non-spacer) pages
}

enum ReaderPageMode: String, Equatable {
    case single, spread
}

struct ReaderWork: Equatable {
    var clickedPath: String
    var images: [PageRef]             // global, including spacers
    var chapters: [ReaderChapter]
    var boundaries: [Int]             // global index where each chapter starts
    var globalToReal: [Int]           // -1 for spacers
    var realCount: Int
    var spreads: [[Int]]              // per-spread page indices (non-spacer only)
    var idxToSpread: [Int]            // global idx → spread index (-1 spacer)
    var chapterStartSpreads: Set<Int> // spreads flagged chapterStart (gap labels)
    var pageMode: ReaderPageMode

    var isCrossChapter: Bool { chapters.count > 1 }

    /// Sibling leaf folders of `path` — `getSiblingLeafFolders` (app.js).
    /// Returns the parent node's children that are leaf folders
    /// (type==folder && hasOnlyImages), with their composed paths.
    /// `nil` when `path` is a top-level node (no parent → single chapter).
    static func siblingLeaves(of path: String, in roots: [TreeNode]) -> [(node: TreeNode, path: String)]? {
        func walk(_ nodes: [TreeNode], _ parent: String) -> [(node: TreeNode, path: String)]? {
            for n in nodes where n.isFolder {
                let full = parent.isEmpty ? n.name : parent + "/" + n.name
                for c in n.children ?? [] {
                    if full + "/" + c.name == path {
                        return (n.children ?? []).map { ($0, full + "/" + $0.name) }
                    }
                }
                if let r = walk(n.children ?? [], full) { return r }
            }
            return nil
        }
        return walk(roots, "")
    }

    /// Which chapter a global index belongs to — `getCurrentChapterInfo`.
    func chapterInfo(at globalIdx: Int) -> (chapterIdx: Int, name: String, localIdx: Int) {
        var ci = 0
        if boundaries.count > 1 {
            for i in 1..<boundaries.count where globalIdx >= boundaries[i] { ci = i }
        }
        let b = boundaries.indices.contains(ci) ? boundaries[ci] : 0
        let name = chapters.indices.contains(ci) ? chapters[ci].name : ""
        return (ci, name, globalIdx - b)
    }

    /// Display index (real pages only) — `globalToRealIdx`.
    func realIndex(at globalIdx: Int) -> Int {
        guard globalIdx >= 0 && globalIdx < globalToReal.count else { return -1 }
        return globalToReal[globalIdx]
    }

    /// Chapter-local page index clamped to that chapter's image range
    /// (`saved.page` semantics — always a real page).
    func resumeGlobalIndex(chapterIdx: Int, localIdx: Int) -> Int {
        let ci = boundaries.indices.contains(chapterIdx) ? chapterIdx : 0
        let b = boundaries[ci]
        let count = chapters[ci].images.count
        let local = localIdx >= 0 && localIdx < count ? localIdx : 0
        return b + local
    }

    /// Chapter list construction — mirrors loadImages' sibling/mixed/fallback
    /// branching. `images(folder:)` fetches /api/v1/images only for the plain
    /// fallback case (tree leaf and mixed-self use direct children).
    static func chapters(for path: String, in roots: [TreeNode]) -> (list: [ReaderChapter], cross: Bool) {
        let siblings = siblingLeaves(of: path, in: roots) ?? []
        let leaves = siblings.filter { $0.node.isFolder && BrowseModel.hasOnlyImages($0.node) }
        let selfNode = BrowseModel.node(at: path, in: roots)
        let isSelfMixed = selfNode.map { BrowseModel.isMixed($0) } ?? false
        let cross = leaves.count > 1 && !isSelfMixed

        if cross {
            var list: [ReaderChapter] = []
            for sib in leaves {
                let imgs = (sib.node.children ?? [])
                    .filter(\.isImage)
                    .map { PageRef(name: $0.name, folder: sib.path) }
                list.append(ReaderChapter(name: sib.node.name,
                                          fullPath: sib.path, images: imgs))
            }
            return (list, true)
        }

        let leafName = BrowseModel.splitDisplayName(path).leaf
        if let node = selfNode, BrowseModel.hasOnlyImages(node) {
            let imgs = (node.children ?? [])
                .filter(\.isImage)
                .map { PageRef(name: $0.name, folder: path) }
            return ([ReaderChapter(name: leafName, fullPath: path, images: imgs)], false)
        }
        if isSelfMixed, let node = selfNode {
            let imgs = (node.children ?? [])
                .filter(\.isImage)
                .map { PageRef(name: $0.name, folder: path) }
            return ([ReaderChapter(name: leafName, fullPath: path, images: imgs)], false)
        }
        // Plain fallback — caller fills images from /api/v1/images.
        return ([ReaderChapter(name: leafName, fullPath: path, images: [])], false)
    }

    /// Assemble the work: boundaries + spacer padding (cross-chapter only)
    /// + real-index maps + spreads. Spacer padding uses mode/offset AT LOAD —
    /// runtime toggles rebuild spreads only (web parity).
    static func assemble(clickedPath: String, chapters: [ReaderChapter],
                         pageMode: ReaderPageMode, spreadOffset: Int) -> ReaderWork {
        var images: [PageRef] = []
        var boundaries: [Int] = []
        if chapters.count > 1 {
            for ch in chapters {
                boundaries.append(images.count)
                images.append(contentsOf: ch.images)
            }
            // app.js: pad = (startIdx - spreadOff + 2) % 2, spacer before each
            // chapter boundary; later boundaries shift too.
            let spreadOff = pageMode == .spread ? spreadOffset : 0
            for c in 0..<chapters.count {
                let startIdx = boundaries[c]
                let pad = (startIdx - spreadOff + 2) % 2
                if pad > 0 {
                    images.insert(.spacer, at: boundaries[c])
                    for j in (c + 1)..<boundaries.count { boundaries[j] += pad }
                    boundaries[c] += pad
                }
            }
        } else {
            images = chapters.first?.images ?? []
            boundaries = [0]
        }

        var g2r = [Int](); var spacerCount = 0
        for (g, img) in images.enumerated() {
            if img.isSpacer { g2r.append(-1); spacerCount += 1 }
            else { g2r.append(g - spacerCount) }
        }

        let work = ReaderWork(
            clickedPath: clickedPath, images: images, chapters: chapters,
            boundaries: boundaries, globalToReal: g2r,
            realCount: images.count - spacerCount,
            spreads: [], idxToSpread: [], chapterStartSpreads: [],
            pageMode: pageMode)
        return work.rebuildSpreads(offset: spreadOffset)
    }

    /// `rebuildSpreads` (spread.js) — groups pages into spreads, filters
    /// spacers, marks chapter-start spreads.
    func rebuildSpreads(offset: Int) -> ReaderWork {
        var groups: [[Int]] = []
        let n = images.count
        if pageMode == .spread {
            var i = 0
            while i < offset && i < n { groups.append([i]); i += 1 }
            while i < n {
                let g = (i + 1 < n) ? [i, i + 1] : [i]
                groups.append(g)
                i += g.count
            }
        } else {
            for i in 0..<n { groups.append([i]) }
        }
        var spreads: [[Int]] = []
        var i2s = [Int](repeating: -1, count: n)
        var starts = Set<Int>()
        for g in groups {
            let pages = g.filter { !images[$0].isSpacer }
            if pages.isEmpty { continue }
            let s = spreads.count
            for p in pages { i2s[p] = s }
            // chapterStart: boundaries.indexOf(pages[0]) > 0
            if boundaries.count > 1,
               let bi = boundaries.firstIndex(of: pages[0]), bi > 0 {
                starts.insert(s)
            }
            spreads.append(pages)
        }
        var w = self
        w.spreads = spreads
        w.idxToSpread = i2s
        w.chapterStartSpreads = starts
        return w
    }

    /// New work after a mode/offset change (runtime toggle — no re-pad).
    func withMode(_ mode: ReaderPageMode, offset: Int) -> ReaderWork {
        var w = self
        w.pageMode = mode
        return w.rebuildSpreads(offset: offset)
    }

    /// `spreadOf` — spread containing global index.
    func spreadOf(_ idx: Int) -> Int {
        guard idx >= 0 && idx < idxToSpread.count else { return 0 }
        return max(0, idxToSpread[idx])
    }

    /// Next/prev spread navigation — `goNext`/`goPrev`.
    func nextSpreadFirstPage(after globalIdx: Int) -> Int? {
        let s = spreadOf(globalIdx)
        return s + 1 < spreads.count ? spreads[s + 1][0] : nil
    }

    func prevSpreadFirstPage(before globalIdx: Int) -> Int? {
        let s = spreadOf(globalIdx)
        return s > 0 ? spreads[s - 1][0] : nil
    }
}
