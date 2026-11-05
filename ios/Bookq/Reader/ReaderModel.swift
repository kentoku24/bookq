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

/// Tap-zone verdict — `zoneActionAt` (app/static/app.js).
/// Edge 30% zones turn pages in the direction `tapForwardLeft` selects
/// (web default: left = forward), the center band toggles chrome.
enum ZoneAction: Equatable {
    case turn(Int)      // +1 forward, -1 back
    case toggleChrome
}

func zoneAction(frac: Double, tapForwardLeft: Bool) -> ZoneAction {
    if frac < 0.30 { return .turn(tapForwardLeft ? 1 : -1) }
    if frac > 0.70 { return .turn(tapForwardLeft ? -1 : 1) }
    return .toggleChrome
}

/// One in-flight page turn for the custom ZStack pager. `progress` 0→1
/// drives both layers: forward slides `from` right off-screen revealing
/// `to` beneath; backward slides `to` in from the right edge covering
/// `from`. `interactive` marks finger-driven turns — the release decides
/// commit vs cancel; programmatic turns always run to completion.
struct PageTurn {
    var from: Int
    var to: Int
    var forward: Bool
    var progress: CGFloat
    var interactive: Bool
}

/// Pure drag/commit math for the pager — unit-tested, no UIKit deps.
enum PageTurnMath {
    /// Horizontal travel (pt) before a drag locks its direction.
    static let decideDistance: CGFloat = 12
    /// Damping applied to drags that point past the first/last spread.
    static let edgeRubber: CGFloat = 0.35
    /// Commit when the predicted end (flick) covers this fraction of the
    /// viewport width, or the actual travel covers commitDistanceFrac.
    static let commitPredictedFrac: CGFloat = 0.5
    static let commitDistanceFrac: CGFloat = 0.33
    /// Soft resistance past a full-width travel while dragging.
    static let overshootDamping: CGFloat = 0.3

    /// Direction verdict: nil until the drag is clearly horizontal
    /// (|dx| > decideDistance and |dx| > |dy|). +1 = forward (drag right),
    /// -1 = backward (drag left) — the manga/RTL convention shared with
    /// the web touch handler (`dragPx > 0 → next`).
    static func dragDirection(dx: CGFloat, dy: CGFloat) -> Int? {
        let ax = abs(dx), ay = abs(dy)
        guard ax > decideDistance, ax > ay else { return nil }
        return dx > 0 ? 1 : -1
    }

    /// Live drag → turn progress. Linear to one viewport width, then
    /// damped so an over-drag keeps moving but slowly.
    static func dragProgress(dx: CGFloat, viewWidth: CGFloat) -> CGFloat {
        dragProgressSigned(dx: abs(dx), viewWidth: viewWidth)
    }

    /// Signed variant for the locked-direction update path: a negative
    /// `dx` (finger reversed past the start) decays progress toward 0
    /// instead of re-growing it via abs().
    static func dragProgressSigned(dx: CGFloat, viewWidth: CGFloat) -> CGFloat {
        guard viewWidth > 0 else { return 0 }
        let p = dx / viewWidth
        if p <= 0 { return 0 }
        return p <= 1 ? p : 1 + (p - 1) * overshootDamping
    }

    /// Release decision in the locked direction's coordinate frame:
    /// `translation`/`predicted` are already sign-normalized (positive =
    /// toward the turn target). Commits on a flick (predicted end ≥ half
    /// the viewport) or a deliberate drag (≥ ~one third).
    static func shouldCommit(translation: CGFloat, predicted: CGFloat,
                             viewWidth: CGFloat) -> Bool {
        guard viewWidth > 0 else { return false }
        return predicted >= viewWidth * commitPredictedFrac
            || translation >= viewWidth * commitDistanceFrac
    }
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
            spreads: [], idxToSpread: [],
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
        for g in groups {
            let pages = g.filter { !images[$0].isSpacer }
            if pages.isEmpty { continue }
            let s = spreads.count
            for p in pages { i2s[p] = s }
            spreads.append(pages)
        }
        var w = self
        w.spreads = spreads
        w.idxToSpread = i2s
        return w
    }

    /// New work after a mode/offset change (runtime toggle — no re-pad).
    func withMode(_ mode: ReaderPageMode, offset: Int) -> ReaderWork {
        var w = self
        w.pageMode = mode
        return w.rebuildSpreads(offset: offset)
    }

    /// Page-mode rule shared by the device reader and the external-display
    /// projection: landscape viewport + spread enabled → .spread.
    static func pageMode(for viewport: CGSize, landscapeSpread: Bool) -> ReaderPageMode {
        (viewport.width >= viewport.height && landscapeSpread) ? .spread : .single
    }

    /// `spreadMetrics` equivalent — fit a spread into the viewport: pages
    /// share one height, each width = ar*h (+gap). Shared by ReaderView's
    /// spreadCell and ExternalReaderView so both lay out identically.
    static func spreadFit(ars: [CGFloat], gap: CGFloat, in size: CGSize) -> CGSize {
        let arSum = max(ars.reduce(0, +), 0.001)
        var h = size.height
        var w = arSum * h + gap * CGFloat(ars.count - 1)
        if w > size.width {
            h = (size.width - gap * CGFloat(ars.count - 1)) / arSum
            w = size.width
        }
        return CGSize(width: w, height: h)
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
