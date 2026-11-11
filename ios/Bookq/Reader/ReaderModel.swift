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

/// Pure drag/commit math for the pager — unit-tested, no UIKit deps.
///
/// The pager is driven by a continuous spread position `pos` (CGFloat):
/// integers are settled spreads, fractions are mid-turn. `floor(pos)` is
/// always the top layer sliding right; `ceil(pos)` sits static beneath —
/// forward (pos rising) slides the current spread off right revealing the
/// next, backward (pos falling) slides the previous in from the right on
/// top. One formula yields both directions, so finger reversals mid-drag
/// need no direction lock: pos just follows the finger back through the
/// origin.
enum PageTurnMath {
    /// Horizontal travel (pt) before a drag locks its direction.
    static let decideDistance: CGFloat = 12
    /// Damping applied to drags that point past the first/last spread.
    static let edgeRubber: CGFloat = 0.35
    /// Commit when the predicted end (flick) covers this fraction of the
    /// viewport width, or deliberate travel covers the web touchend
    /// threshold `min(10% width, 70pt)` (app.js — points ≈ CSS px).
    static let commitPredictedFrac: CGFloat = 0.5
    static let commitDistanceCap: CGFloat = 70
    /// Soft resistance past a full-width travel while dragging.
    static let overshootDamping: CGFloat = 0.3
    /// Base flip-through rate (spreads/sec) when `pos` chases
    /// `posTarget` — must exceed a ~10 taps/s input burst or `pos`
    /// falls behind permanently; the per-target ease (`dist/0.12`)
    /// still lands single turns gently. Long jumps scale up ×(dist/3),
    /// capped ×10.
    static let chaseSpreadsPerSecond: CGFloat = 12
    /// `/view/?w=` width for in-flight pages — ~1.3× an iPhone's 3×
    /// viewport, so fast-tier bitmaps decode ~4× cheaper than the
    /// 2560px default while flip-through stays sharp enough to read
    /// motion. Settled spreads upgrade to the full rendition.
    static let fastPageWidth = 1600
    /// Peak black overlay on the covered (static) spread mid-turn —
    /// the depth scrim that sells the page-lift metaphor (iBooks /
    /// Android `STACK_SCRIM` parity). Fully covered → this opacity,
    /// fully revealed → 0.
    static let stackScrimMax: CGFloat = 0.35

    /// Strip pitch for a single-mode pair interval — `(viewWidth +
    /// meanPageWidth) / 2 - 1pt`, where `pageWidth` is the pair's mean
    /// displayed page width (`spreadFit` width: `min(viewportW,
    /// ar * viewportH)`). The mate's cell is full-viewport wide with the
    /// page centred inside, so this pitch puts the mate's inner image
    /// edge exactly 1pt inside the screen edge — a true 1pt peek of page
    /// content at any aspect ratio. Full-width pages (portrait) give
    /// `viewWidth - 1`; letterboxed pages (landscape single) pitch by
    /// less than the viewport so the sliver still lands on the screen
    /// edge instead of the page edge (where it would show the whole
    /// mate page, not a peek). Averaging the pair's two widths keeps
    /// peek → strip → peek seamless when the mates' aspects differ.
    static func stripPitch(viewWidth: CGFloat, pageWidth: CGFloat) -> CGFloat {
        max((viewWidth + pageWidth) / 2 - 1, 0)
    }

    /// Whether `pos` sits strictly inside an intra-spread interval —
    /// `[floor(pos), floor(pos)+1]` is a pair adjacency, so the two
    /// pages of one spread pair slide past each other (strip layout)
    /// instead of the stack reveal. Integer `pos` is a rest state and
    /// always false here; the rest peek is handled separately.
    static func inStripInterval(_ pos: CGFloat,
                                intraIntervals: Set<Int>) -> Bool {
        let j = Int(floor(pos))
        return pos != CGFloat(j) && intraIntervals.contains(j)
    }

    /// Direction verdict: nil until the drag is clearly horizontal
    /// (|dx| > decideDistance and |dx| > |dy|). +1 = forward (drag right),
    /// -1 = backward (drag left) — the manga/RTL convention shared with
    /// the web touch handler (`dragPx > 0 → next`).
    static func dragDirection(dx: CGFloat, dy: CGFloat) -> Int? {
        let ax = abs(dx), ay = abs(dy)
        guard ax > decideDistance, ax > ay else { return nil }
        return dx > 0 ? 1 : -1
    }

    /// Live drag → signed spread delta for `pos`. Odd-symmetric: positive
    /// dx advances pos (forward), negative retreats it (backward); travel
    /// past one viewport keeps moving at `overshootDamping` rate.
    static func dragDelta(dx: CGFloat, viewWidth: CGFloat) -> CGFloat {
        guard viewWidth > 0 else { return 0 }
        let p = dx / viewWidth
        let a = abs(p)
        let mag = a <= 1 ? a : 1 + (a - 1) * overshootDamping
        return p < 0 ? -mag : mag
    }

    /// Rubber-band for `pos` against [lo, hi] spread bounds — inside
    /// passthrough, overshoot damped at edgeRubber. Callers pass the
    /// current chapter's spread range (`chapterSpreadRange`), which for
    /// a single-chapter work IS the book edge [0, count-1] — web damps
    /// `dragPx *= 0.35` past the chapter edge the same way.
    static func rangeClamp(_ p: CGFloat, lo: Int, hi: Int) -> CGFloat {
        let l = CGFloat(lo), h = CGFloat(max(hi, lo))
        if p < l { return l + (p - l) * edgeRubber }
        if p > h { return h + (p - h) * edgeRubber }
        return p
    }

    /// Release decision for a drag in continuous-pos space. Returns the
    /// commit direction (+1 = forward/right, -1 = backward/left) or nil
    /// to spring back. A flick (predicted end ≥ half the viewport) wins
    /// over deliberate travel (web touchend `min(10% width, 70px)`);
    /// direction comes from the firing signal so a reversed flick
    /// commits the way it points.
    static func commitDirection(translation: CGFloat, predicted: CGFloat,
                                viewWidth: CGFloat) -> Int? {
        guard viewWidth > 0 else { return nil }
        if abs(predicted) >= viewWidth * commitPredictedFrac {
            return predicted > 0 ? 1 : -1
        }
        if abs(translation) >= min(viewWidth * 0.10, commitDistanceCap) {
            return translation > 0 ? 1 : -1
        }
        return nil
    }

    /// Covered fraction of the static (`ceil`) spread during a turn —
    /// `ceil(pos) - pos` inside the book. Forward (`pos = i+f`) dims the
    /// revealed next spread by 1-f so it brightens as the mover slides
    /// away; backward (`pos = i-f`) dims the current spread by f as the
    /// previous one covers it. Integer `pos` yields 0 for free, and
    /// edge overshoot is excluded so rubber-banding never dims.
    static func coveredFraction(_ pos: CGFloat, count: Int) -> CGFloat {
        guard pos > 0, pos < CGFloat(count - 1) else { return 0 }
        return min(max(ceil(pos) - pos, 0), 1)
    }
}

/// A page's position inside its spread pair — single-page mode only.
/// `mate` is the mate page's *display* spread index (single mode has
/// one page per spread, so it names the adjacent pager layer).
enum SpreadPairRole: Equatable {
    case solo
    case first(mate: Int)     // 1ページ目 — right page, read first
    case second(mate: Int)    // 2ページ目 — left/facing page

    /// The mate's display-spread index, nil for unpaired pages.
    var mateSpread: Int? {
        switch self {
        case .solo: return nil
        case .first(let m), .second(let m): return m
        }
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
    /// Single-mode spread pairing: the offset-aware spread grouping
    /// applied to one-page spreads — each display spread's role inside
    /// its pair. `.spread` mode leaves every role `.solo` (the pair is
    /// already side-by-side there).
    var pairRoles: [SpreadPairRole] = []
    /// Display-spread j where [j, j+1] are the two pages of one pair —
    /// the pager's "intra-spread" turn intervals (strip layout).
    var intraIntervals: Set<Int> = []

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

    /// Spread-mode grouping shared by `.spread` display spreads and the
    /// single-mode pair map: `offset` solo pages then consecutive pairs
    /// over global indices (spacers included — filtered per use).
    static func spreadGroups(n: Int, offset: Int) -> [[Int]] {
        var groups: [[Int]] = []
        var i = 0
        while i < offset && i < n { groups.append([i]); i += 1 }
        while i < n {
            let g = (i + 1 < n) ? [i, i + 1] : [i]
            groups.append(g)
            i += g.count
        }
        return groups
    }

    /// `rebuildSpreads` (spread.js) — groups pages into spreads, filters
    /// spacers, marks chapter-start spreads.
    func rebuildSpreads(offset: Int) -> ReaderWork {
        let n = images.count
        let groups = pageMode == .spread
            ? Self.spreadGroups(n: n, offset: offset)
            : (0..<n).map { [$0] }
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
        w.rebuildPairLayout(offset: offset)
        return w
    }

    /// Single-mode pair layout: which display spread is the first
    /// (1ページ目) or second (2ページ目) page of its spread pair, and
    /// the mate's display spread. Pair membership comes from the
    /// spread-mode grouping (offset-aware) applied to global indices;
    /// pairs that would straddle a chapter boundary are dissolved —
    /// spreads never mix chapters, so a chapter edge stays a stack
    /// turn even when single-mode offset pairing would pair across it
    /// (single-mode spacer padding is computed for offset 0).
    private mutating func rebuildPairLayout(offset: Int) {
        pairRoles = [SpreadPairRole](repeating: .solo, count: spreads.count)
        intraIntervals = []
        guard pageMode == .single else { return }
        let boundarySet = Set(boundaries.dropFirst())
        // global page idx → (isFirst, mate global idx); only real pairs.
        var pairPos: [Int: (first: Bool, mate: Int)] = [:]
        for g in Self.spreadGroups(n: images.count, offset: offset) {
            let pages = g.filter { !images[$0].isSpacer }
            guard pages.count == 2, !boundarySet.contains(pages[1]) else { continue }
            pairPos[pages[0]] = (true, pages[1])
            pairPos[pages[1]] = (false, pages[0])
        }
        for (s, spread) in spreads.enumerated() {
            guard let g = spread.first, let p = pairPos[g] else { continue }
            let mateSpread = idxToSpread[p.mate]
            pairRoles[s] = p.first ? .first(mate: mateSpread) : .second(mate: mateSpread)
            // Mates are adjacent real pages, hence adjacent display
            // spreads — [s, s+1] is the pair's intra-spread interval.
            if p.first && mateSpread == s + 1 { intraIntervals.insert(s) }
        }
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

    /// `getChapterSpreadRange` (app.js) — spread index range [lo, hi]
    /// covering chapter `ci`'s real pages. Spreads never mix chapters
    /// (spacer padding aligns boundaries), so the range is contiguous.
    /// Swipes and keys clamp turns to it; taps and overview jumps cross.
    func chapterSpreadRange(_ ci: Int) -> (lo: Int, hi: Int) {
        guard !spreads.isEmpty else { return (0, 0) }
        if boundaries.count <= 1 { return (0, spreads.count - 1) }
        let c = min(max(ci, 0), boundaries.count - 1)
        let start = boundaries[c]
        let end = c + 1 < boundaries.count ? boundaries[c + 1] : images.count
        var firstReal = -1, lastReal = -1
        if start < images.count {
            for i in start..<min(end, images.count) where !images[i].isSpacer {
                if firstReal < 0 { firstReal = i }
                lastReal = i
            }
        }
        if firstReal < 0 {
            let s = spreadOf(min(start, max(images.count - 1, 0)))
            return (s, s)
        }
        return (spreadOf(firstReal), spreadOf(lastReal))
    }

    /// `spreadOf` — spread containing global index.
    func spreadOf(_ idx: Int) -> Int {
        guard idx >= 0 && idx < idxToSpread.count else { return 0 }
        return max(0, idxToSpread[idx])
    }

    // MARK: - chapter-local slider (web syncChapterSlider family)

    /// Spread range [lo, hi] of the chapter `globalIdx` belongs to —
    /// `chapterSpreadRange` resolved through `chapterInfo`.
    func chapterSliderRange(at globalIdx: Int) -> (lo: Int, hi: Int) {
        chapterSpreadRange(chapterInfo(at: globalIdx).chapterIdx)
    }

    /// `chapterSliderMax` — the slider's span: hi - lo spreads inside the
    /// current chapter only. Cross-chapter moves never come from the
    /// slider; they happen via page turns at the chapter edges.
    func chapterSliderMax(at globalIdx: Int) -> Int {
        let r = chapterSliderRange(at: globalIdx)
        return max(r.hi - r.lo, 0)
    }

    /// `idxToChapterSlider` — global index → slider value: the spread's
    /// offset inside its chapter, clamped to 0...span.
    func idxToChapterSlider(_ globalIdx: Int) -> Int {
        let r = chapterSliderRange(at: globalIdx)
        return min(max(spreadOf(globalIdx) - r.lo, 0), max(r.hi - r.lo, 0))
    }

    /// `chapterSliderToIdx` — slider value → first global page of the
    /// clamped chapter-local spread (`spreads[...].pages[0]` parity).
    /// nil when the resolved spread is out of table.
    func chapterSliderToIdx(_ val: Int, at globalIdx: Int) -> Int? {
        let r = chapterSliderRange(at: globalIdx)
        let s = min(max(r.lo + val, r.lo), r.hi)
        guard spreads.indices.contains(s) else { return nil }
        return spreads[s].first
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
