import SwiftUI
import UIKit

/// Full reader: single-page portrait / two-page spread landscape, chapter
/// concatenation, zoom, progress sync. Mirrors app.js loadImages +
/// spread.js semantics.
struct ReaderView: View {
    let path: String

    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var progress: ReadingProgressStore
    @EnvironmentObject private var offline: OfflineStore
    @EnvironmentObject private var ext: ExternalDisplayStore
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase

    @State private var work: ReaderWork?
    @State private var currentIdx = 0            // global image index
    @State private var error: String?
    @State private var loading = true
    @State private var showChrome = true
    @State private var spreadOffset = 0          // 0=default pairs, 1=cover solo
    @State private var landscapeSpread = true    // spread in landscape
    @State private var zoomed = false
    @State private var pageAR: [Int: CGFloat] = [:]  // measured AR per page
    @State private var viewportSize: CGSize = .zero
    @State private var saveTask: Task<Void, Never>?
    @State private var showOverview = false
    /// tapForward equivalent — session-only like the web. true = left zone
    /// turns forward (web default), false = right zone turns forward.
    @State private var tapForwardLeft = true
    /// Continuous spread position — the pager's single visual truth.
    /// Integers are settled spreads; fractions are mid-turn. `floor(pos)`
    /// is the top layer sliding right, `ceil(pos)` sits static beneath, so
    /// one formula produces both turn directions (see PageTurnMath).
    @State private var pos: CGFloat = 0
    /// Spread index `pos` is animating toward. Programmatic turns (tap,
    /// keys, slider, overview) just move this target — the spring chases
    /// it, so rapid taps each advance one spread instead of queueing.
    @State private var posTarget = 0
    /// Finger-driven drag: `pos` captured when the gesture locked its
    /// direction (nil = not dragging). Cancel returns `pos` to `posTarget`.
    @State private var dragBase: CGFloat?
    /// A pinch gesture is in flight on some page — page drags suspend.
    @State private var pinchActive = false

    private var pageMode: ReaderPageMode {
        ReaderWork.pageMode(for: viewportSize, landscapeSpread: landscapeSpread)
    }

    var body: some View {
        GeometryReader { geo in
            ZStack {
                Color.black.ignoresSafeArea()
                content(in: geo.size)
            }
            .onAppear { viewportSize = geo.size }
            .onChange(of: geo.size) { _, size in
                let wasLandscape = viewportSize.width >= viewportSize.height
                let isLandscape = size.width >= size.height
                viewportSize = size
                if wasLandscape != isLandscape { onOrientationChange() }
            }
            .task { await load(viewport: geo.size) }
        }
        .statusBarHidden(!showChrome)
        .overlay(alignment: .top) { if showChrome { chrome } }
        .onDisappear {
            saveCurrentPosition()
            progress.sync?.flush()          // pagehide → flush pending PUT
            ext.endPresentation()           // external display → back to mirroring
        }
        .modifier(ExternalAccessoryModifier(ext: ext))
        .onChange(of: pageAR) { _, _ in publishExternal() }
        .onChange(of: scenePhase) { _, phase in
            if phase != .active {
                saveCurrentPosition()
                progress.sync?.flush()
            }
        }
    }

    // MARK: - Content

    @ViewBuilder
    private func content(in size: CGSize) -> some View {
        if loading {
            ProgressView().tint(.white)
        } else if let error {
            errorView(error)
        } else if let work, !work.images.isEmpty {
            pager(work: work, in: size)
        } else {
            Text("📭 このフォルダに画像はありません")
                .foregroundStyle(.white)
        }
    }

    private func errorView(_ msg: String) -> some View {
        VStack(spacing: 12) {
            Image(systemName: "exclamationmark.triangle")
                .font(.largeTitle).foregroundStyle(.white)
            Text(msg).foregroundStyle(.white).font(.footnote)
                .multilineTextAlignment(.center)
            Button("閉じる") { dismiss() }.buttonStyle(.borderedProminent)
        }
        .padding()
    }

    /// Custom pager — asymmetric page-stack turn driven by continuous
    /// `pos` (replaces the old TabView strip slide). `floor(pos)` slides
    /// right on top while `ceil(pos)` waits static beneath: pos rising =
    /// current spread exits right revealing next; pos falling = previous
    /// spread enters from the right covering current. `currentIdx` lands
    /// on the destination at turn start (web parity: counter/slider move
    /// with the gesture while `pos` catches up visually).
    private func pager(work: ReaderWork, in size: CGSize) -> some View {
        return ZStack {
            ForEach(pagerSpreadIndices(work: work), id: \.self) { s in
                spreadCell(work: work, spread: s, pages: work.spreads[s], in: size)
                    .opacity(opacityFor(s, count: work.spreads.count))
                    .offset(x: offsetFor(s, count: work.spreads.count, size: size))
                    .zIndex(zIndexFor(s, count: work.spreads.count))
                    .allowsHitTesting(pagerSettled && s == posTarget)
            }
        }
        // NOTE: no RTL environment here — `.offset(x:)` mirrors under
        // rightToLeft, which would invert the turn direction. RTL page
        // order lives inside spreadCell instead.
        // Tap zones (web zoneActionAt): edge 30% zones follow tapForward
        // (default left = forward), center toggles chrome. SpatialTap is
        // single-tap; zoom pan/pinch live inside ZoomableScrollView.
        .gesture(SpatialTapGesture().onEnded { ev in
            let frac = ev.location.x / max(size.width, 1)
            switch zoneAction(frac: frac, tapForwardLeft: tapForwardLeft) {
            case .turn(let d): turn(d, in: work)
            case .toggleChrome: withAnimation { showChrome.toggle() }
            }
        })
        // Page-turn drag: right = next (current slides right, next revealed
        // beneath), left = prev (previous slides in from the right on top).
        // simultaneousGesture so it never blocks the tap zones; zoomed or
        // pinching suspends it inside the handlers.
        .simultaneousGesture(
            DragGesture(minimumDistance: PageTurnMath.decideDistance)
                .onChanged { v in pagerDragChanged(v, work: work, size: size) }
                .onEnded { v in pagerDragEnded(v, work: work, size: size) }
        )
        // Web keyboard parity (iPad/hardware keyboard, RTL): ←=next, →=prev,
        // Home/End = chapter edges, G = overview.
        .focusable()
        .focusEffectDisabled()
        .onKeyPress(.rightArrow) { turn(-1, in: work); return .handled }
        .onKeyPress(.leftArrow) { turn(1, in: work); return .handled }
        .onKeyPress(keys: [.init("g"), .init("G")]) { _ in
            showOverview = true; return .handled
        }
        .onKeyPress(.home) {
            snapTo(chapterEdge(0, in: work), in: work, animated: false)
            return .handled
        }
        .onKeyPress(.end) { snapTo(chapterEdge(-1, in: work), in: work); return .handled }
        .sheet(isPresented: $showOverview) {
            if let api = model.api {
                OverviewView(
                    work: work, api: api,
                    currentIdx: Binding(
                        get: { currentIdx },
                        set: { snapTo($0, in: work) }),
                    onDismiss: { showOverview = false }
                )
            }
        }
    }

    // MARK: - Pager layers

    /// True when the pager is at rest on `posTarget` — hit-testing (zoom,
    /// scroll) is suspended while `pos` is mid-turn or being dragged.
    private var pagerSettled: Bool {
        dragBase == nil && pos == CGFloat(posTarget)
    }

    /// Spread indices the pager renders: the two spreads adjacent to
    /// `pos` plus one neighbour on each side kept at opacity 0 (image/AR
    /// warm-up, matching the old TabView neighbourhood).
    private func pagerSpreadIndices(work: ReaderWork) -> [Int] {
        let n = work.spreads.count
        var set = Set<Int>()
        for s in [Int(floor(pos)) - 1, Int(floor(pos)),
                  Int(ceil(pos)), Int(ceil(pos)) + 1] {
            if s >= 0 && s < n { set.insert(s) }
        }
        return set.sorted()
    }

    /// Only layers participating in the current view are opaque — hidden
    /// neighbours must never peek out from behind a narrower spread.
    /// Edge overshoot (pos < 0 or > count-1) shows only the edge spread.
    private func opacityFor(_ s: Int, count: Int) -> Double {
        if pos < 0 { return s == 0 ? 1 : 0 }
        if pos > CGFloat(count - 1) { return s == count - 1 ? 1 : 0 }
        return (s == Int(floor(pos)) || s == Int(ceil(pos))) ? 1 : 0
    }

    /// Visual offset: the floor spread slides right by the fractional
    /// part of `pos`; everything else sits at 0 beneath it. Past either
    /// work edge the boundary spread rubber-bands instead.
    private func offsetFor(_ s: Int, count: Int, size: CGSize) -> CGFloat {
        let w = size.width
        if pos < 0 { return s == 0 ? pos * w : 0 }
        if pos > CGFloat(count - 1) {
            return s == count - 1 ? (pos - CGFloat(count - 1)) * w : 0
        }
        return s == Int(floor(pos)) ? (pos - CGFloat(Int(floor(pos)))) * w : 0
    }

    /// `floor(pos)` is always the top layer while a turn is in flight —
    /// forward it exits right, backward it is the incoming spread.
    private func zIndexFor(_ s: Int, count: Int) -> Double {
        if pos < 0 { return s == 0 ? 1 : 0 }
        if pos > CGFloat(count - 1) { return s == count - 1 ? 1 : 0 }
        return s == Int(floor(pos)) ? 1 : 0
    }

    /// Global index of the current chapter's first (0) or last (-1) real page.
    private func chapterEdge(_ which: Int, in work: ReaderWork) -> Int {
        let info = work.chapterInfo(at: currentIdx)
        let local = which == 0 ? 0 : work.chapters[info.chapterIdx].images.count - 1
        return work.resumeGlobalIndex(chapterIdx: info.chapterIdx, localIdx: local)
    }

    /// One spread cell: 1–2 pages fit into the viewport using measured ARs
    /// (`spreadMetrics` equivalent — pages share one height, width = ar*h).
    /// RTL lives here (not on the pager ZStack) so manga page order stays
    /// right-to-left without mirroring the pager's `.offset` turns.
    private func spreadCell(work: ReaderWork, spread s: Int,
                            pages: [Int], in size: CGSize) -> some View {
        let ars = pages.map { pageAR[$0] ?? 0.7 }
        let gap: CGFloat = 5
        let fit = ReaderWork.spreadFit(ars: ars, gap: gap, in: size)
        return HStack(spacing: gap) {
            ForEach(Array(pages.enumerated()), id: \.offset) { pos, idx in
                pageView(work: work, idx: idx)
                    .frame(width: ars[pos] * fit.height, height: fit.height)
            }
        }
        .environment(\.layoutDirection, .rightToLeft)
        .frame(width: fit.width, height: fit.height)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func pageView(work: ReaderWork, idx: Int) -> some View {
        let page = work.images[idx]
        let url = model.api?.pageURL(folder: page.folder, name: page.name,
                                     original: model.config.originalImages)
        return ZoomableScrollView(
            onZoomChange: { z in zoomed = z },
            onPinchOut: { showOverview = true },
            onPinchState: { active in pinchActive = active }
        ) {
            RemoteImageView(url: url, contentMode: .fit) { img in
                let ar = img.size.width / max(img.size.height, 1)
                if pageAR[idx] != ar { pageAR[idx] = ar }
            }
            .background(Color.black)
        }
    }

    // MARK: - Chrome

    private var chrome: some View {
        VStack {
            HStack {
                Button { dismiss() } label: {
                    Image(systemName: "xmark")
                        .padding(10)
                        .background(.black.opacity(0.5), in: Circle())
                }
                Spacer()
                VStack(spacing: 2) {
                    Text(counterText).font(.subheadline).monospacedDigit()
                    // External-display bring-up diagnostic (temporary):
                    // mode/bounds when connected, else availability state.
                    if let s = ext.externalScreen {
                        Text(ExternalDisplayStore.describe(s))
                            .font(.system(size: 8, design: .monospaced))
                            .foregroundStyle(.white.opacity(0.7))
                            .lineLimit(1)
                    } else if ext.accessoryAvailable {
                        Text("ext: display available")
                            .font(.system(size: 8, design: .monospaced))
                            .foregroundStyle(.white.opacity(0.7))
                    }
                }
                .foregroundStyle(.white)
                .padding(6)
                .background(.black.opacity(0.5), in: Capsule())
                Spacer()
                modeButtons
            }
            .padding(10)
            Spacer()
            if let work {
                sliderRow(work)
            }
        }
        .transition(.opacity)
    }

    private var modeButtons: some View {
        HStack(spacing: 8) {
            if pageMode == .spread {
                Button {
                    spreadOffset = spreadOffset == 0 ? 1 : 0
                    rebuild()
                } label: {
                    Image(systemName: spreadOffset == 1 ? "book" : "book.pages")
                        .padding(10)
                        .background(spreadOffset == 1 ? Color.accentColor : .black.opacity(0.5),
                                    in: Circle())
                }
                .accessibilityLabel("spread offset")
            }
            // web: #spread-controls is only shown in landscape — a visible
            // but inert toggle in portrait reads as a dead button.
            if viewportSize.width >= viewportSize.height {
                Button {
                    landscapeSpread.toggle()
                    rebuild()
                } label: {
                    Image(systemName: pageMode == .spread ? "rectangle.split.2x1" : "rectangle.portrait")
                        .padding(10)
                        .background(.black.opacity(0.5), in: Circle())
                }
                .accessibilityLabel("spread toggle")
            }
            // tapmode-controls equivalent: edge-zone direction selector.
            // Both directions shown like the web's two corner buttons.
            HStack(spacing: 0) {
                Button { tapForwardLeft = true } label: {
                    Image(systemName: "arrowtriangle.left.fill")
                        .frame(width: 33, height: 36)
                        .background(tapForwardLeft ? Color.accentColor : .clear,
                                    in: Circle())
                }
                .accessibilityLabel("左タップで次へ")
                Button { tapForwardLeft = false } label: {
                    Image(systemName: "arrowtriangle.right.fill")
                        .frame(width: 33, height: 36)
                        .background(!tapForwardLeft ? Color.accentColor : .clear,
                                    in: Circle())
                }
                .accessibilityLabel("右タップで次へ")
            }
            .background(.black.opacity(0.5), in: Capsule())
            Button {
                showOverview = true
            } label: {
                Image(systemName: "rectangle.grid.2x2")
                    .padding(10)
                    .background(.black.opacity(0.5), in: Circle())
            }
            .accessibilityLabel("複数ページ")
            // External display toggle: shown while a display is attached
            // or projecting, and stays while disabled so it can be re-enabled.
            if ext.accessoryAvailable || ext.connected || !ext.userEnabled {
                Button {
                    ext.userEnabled.toggle()
                } label: {
                    Image(systemName: ext.connected && ext.userEnabled ? "tv.fill" : "tv")
                        .padding(10)
                        .background(ext.userEnabled ? Color.accentColor : .black.opacity(0.5),
                                    in: Circle())
                }
                .accessibilityLabel("外部ディスプレイ")
            }
        }
        .foregroundStyle(.white)
    }

    private func sliderRow(_ work: ReaderWork) -> some View {
        Slider(
            value: Binding(
                get: { Double(work.spreadOf(currentIdx)) },
                set: { v in
                    let s = Int(v.rounded())
                    if s < work.spreads.count, let first = work.spreads[s].first {
                        snapTo(first, in: work)
                    }
                }),
            in: 0...Double(max(work.spreads.count - 1, 1)),
            step: 1
        )
        .disabled(work.spreads.count <= 1)
        .environment(\.layoutDirection, .rightToLeft)
        .padding(.horizontal, 20)
        .padding(.bottom, 30)
        .tint(.white)
    }

    /// `idx+1 / total` — real index, or chapter-local when cross-chapter.
    private var counterText: String {
        guard let work else { return "" }
        let real = work.realIndex(at: currentIdx)
        if work.isCrossChapter {
            let info = work.chapterInfo(at: currentIdx)
            let total = work.chapters[info.chapterIdx].images.count
            return "\(info.localIdx + 1) / \(total)"
        }
        return "\(real + 1) / \(work.realCount)"
    }

    // MARK: - Navigation

    /// Tap-zone turn — web navTurn: spread granularity, cross-chapter.
    /// Every tap lands immediately: `posTarget` steps one spread and the
    /// `pos` spring chases it, so rapid taps each count as a turn (the
    /// pager fast-forwards through spreads instead of queueing animations).
    private func turn(_ dir: Int, in work: ReaderWork) {
        guard let next = dir > 0
            ? work.nextSpreadFirstPage(after: currentIdx)
            : work.prevSpreadFirstPage(before: currentIdx) else { return }
        snapTo(next, in: work)
    }

    /// Navigate to `idx` (any page — the spread it belongs to is the turn
    /// unit). `currentIdx` lands immediately; `pos` animates to the target
    /// spread unless `animated` is false (Home key, mode rebuilds).
    private func snapTo(_ idx: Int, in work: ReaderWork, animated: Bool = true) {
        guard idx >= 0 && idx < work.images.count else { return }
        let target = work.spreadOf(idx)
        guard work.spreads.indices.contains(target) else {
            applyLanding(idx, in: work)
            return
        }
        if target != posTarget {
            posTarget = target
            if animated {
                withAnimation(.spring(response: 0.28, dampingFraction: 0.9)) {
                    pos = CGFloat(target)
                }
            } else {
                pos = CGFloat(target)
            }
        }
        applyLanding(idx, in: work)
    }

    /// Side effects of arriving at `idx` — runs at turn START so the
    /// counter/slider/external display move with the gesture (web parity).
    private func applyLanding(_ idx: Int, in work: ReaderWork) {
        currentIdx = idx
        scheduleSave()
        prefetch(around: idx, in: work)
        publishExternal()
    }

    // MARK: - Page-turn drag

    /// Live drag → `pos`. Waits for a clearly-horizontal move, then the
    /// finger owns `pos` directly: right = forward (pos rises, current
    /// slides right revealing next beneath), left = backward (pos falls,
    /// previous slides in from the right on top). Reversing the finger
    /// walks `pos` back through the origin — no direction lock needed.
    private func pagerDragChanged(_ v: DragGesture.Value,
                                  work: ReaderWork, size: CGSize) {
        guard !zoomed, !pinchActive else { return }
        let dx = v.translation.width
        let dy = v.translation.height
        if dragBase == nil {
            // Only take over once the gesture is decided-horizontal; a
            // mid-animation `pos` is a valid base (cancel returns to
            // `posTarget`, the interrupted destination).
            guard PageTurnMath.dragDirection(dx: dx, dy: dy) != nil else { return }
            dragBase = pos
        }
        guard let base = dragBase else { return }
        let raw = base + PageTurnMath.dragDelta(dx: dx, viewWidth: size.width)
        pos = PageTurnMath.edgeClamp(raw, count: work.spreads.count)
    }

    /// Release: commit completes the turn in the flick/travel direction,
    /// otherwise `pos` springs back to `posTarget`. A pinch that started
    /// mid-drag poisons the gesture — never commit it.
    private func pagerDragEnded(_ v: DragGesture.Value,
                                work: ReaderWork, size: CGSize) {
        guard let base = dragBase else { return }
        dragBase = nil
        let n = work.spreads.count
        guard n > 0 else { return }
        // The release translation can be ahead of the last onChanged —
        // settle `pos` on it before deciding commit vs cancel.
        pos = PageTurnMath.edgeClamp(
            base + PageTurnMath.dragDelta(dx: v.translation.width,
                                          viewWidth: size.width),
            count: n)
        let commit = !pinchActive ? PageTurnMath.commitDirection(
            translation: v.translation.width,
            predicted: v.predictedEndTranslation.width,
            viewWidth: size.width) : nil
        var target = posTarget
        if let dir = commit {
            // Complete toward the boundary `pos` is heading for: forward
            // rounds up, backward rounds down; an exact-integer release
            // still counts as one more spread in the commit direction.
            var t = dir > 0 ? Int(pos.rounded(.up)) : Int(pos.rounded(.down))
            if CGFloat(t) == pos { t += dir }
            target = min(max(t, 0), n - 1)
        }
        if target != posTarget, let landing = work.spreads[safe: target]?.first {
            posTarget = target
            applyLanding(landing, in: work)
        }
        withAnimation(.spring(response: 0.28, dampingFraction: 0.9)) {
            pos = CGFloat(target)
        }
    }

    /// Publish the current snapshot to the external-display projection.
    /// The store activates the session only while a screen is attached.
    private func publishExternal() {
        guard let work else { return }
        ext.updatePresentation(.init(
            work: work, currentIdx: currentIdx, pageAR: pageAR,
            spreadOffset: spreadOffset, landscapeSpread: landscapeSpread,
            urlFor: { [model] page in
                model.api?.pageURL(folder: page.folder, name: page.name,
                                   original: model.config.originalImages)
            }))
    }

    private func prefetch(around idx: Int, in work: ReaderWork) {
        // spread.js: ahead=8, behind=3 (ensureWindow)
        let lo = max(0, idx - 3)
        let hi = min(work.images.count - 1, idx + 8)
        for i in lo...hi where !work.images[i].isSpacer {
            if let url = model.api?.pageURL(folder: work.images[i].folder,
                                            name: work.images[i].name,
                                            original: model.config.originalImages) {
                Task { _ = await RemoteImageLoader.shared.load(url) }
            }
        }
    }

    /// Rebuild spreads on mode/offset/orientation change (no re-pad).
    private func rebuild() {
        guard var w = work else { return }
        // web: spreadOffset resets to 0 when leaving spread mode
        if pageMode != .spread { spreadOffset = 0 }
        // web: on switch to spread, current page snaps down to pair start
        if pageMode == .spread && w.pageMode != .spread {
            currentIdx = (currentIdx / 2) * 2
        }
        w = w.withMode(pageMode, offset: spreadOffset)
        work = w
        // Spreads regrouped — `pos` indices are stale. Web parity:
        // orientation/mode changes snap without animation.
        let s = min(max(w.spreadOf(currentIdx), 0), max(w.spreads.count - 1, 0))
        pos = CGFloat(s)
        posTarget = s
        dragBase = nil
        publishExternal()
    }

    private func onOrientationChange() {
        guard work != nil else { return }
        rebuild()
    }

    // MARK: - Progress

    /// 500ms local debounce → record() → store + ProgressSync.push.
    private func scheduleSave() {
        saveTask?.cancel()
        saveTask = Task {
            try? await Task.sleep(nanoseconds: 500_000_000)
            guard !Task.isCancelled else { return }
            await MainActor.run { saveCurrentPosition() }
        }
    }

    private func saveCurrentPosition() {
        guard let work, !work.images.isEmpty else { return }
        let title = BrowseModel.splitDisplayName(path).leaf
        if work.isCrossChapter {
            let info = work.chapterInfo(at: currentIdx)
            let chapter = work.chapters[info.chapterIdx]
            progress.record(path: chapter.fullPath, page: info.localIdx,
                            total: chapter.images.count, title: chapter.name)
        } else {
            progress.record(path: path, page: currentIdx,
                            total: work.realCount, title: title)
        }
    }

    /// Fresh listing online; downloaded-subset listing when offline —
    /// the web's "cached entries serve the reader" equivalent.
    private func namesForLeaf(_ leaf: String) async throws -> [String] {
        guard let api = model.api else {
            if let names = offline.downloadedNames(leaf: leaf) { return names }
            throw APIError.protocolError
        }
        do {
            return try await api.images(folder: leaf)
        } catch {
            if let names = offline.downloadedNames(leaf: leaf) { return names }
            throw error
        }
    }

    // MARK: - Load

    /// `viewport` is the GeometryReader size at first layout — reading the
    /// @State viewportSize here would race with .onAppear.
    private func load(viewport: CGSize) async {
        guard model.api != nil, let library = model.library else {
            error = "未接続"; loading = false; return
        }
        viewportSize = viewport
        let openMode: ReaderPageMode =
            (viewport.width >= viewport.height && landscapeSpread) ? .spread : .single
        do {
            var (chapters, _) = ReaderWork.chapters(for: path, in: library.tree)
            // Plain fallback: no tree data → fetch /api/v1/images (web behavior)
            if chapters.count == 1 && chapters[0].images.isEmpty {
                let names = try await namesForLeaf(path)
                chapters[0].images = names.map { PageRef(name: $0, folder: path) }
            }
            // If cross-chapter leaves had empty children (shouldn't happen
            // for leaf folders — tree carries their image names), fetch.
            for (i, ch) in chapters.enumerated() where ch.images.isEmpty {
                let names = try await namesForLeaf(ch.fullPath)
                chapters[i].images = names.map { PageRef(name: $0, folder: ch.fullPath) }
            }
            // Leaf reads update LRU timestamps (web lastReadAt).
            for ch in chapters { offline.touchRead(leaf: ch.fullPath) }

            let w = ReaderWork.assemble(clickedPath: path, chapters: chapters,
                                      pageMode: openMode, spreadOffset: spreadOffset)
            // Resume: saved.page is chapter-local (web semantics). The saved
            // entry lives on the LEAF path (chapter or folder).
            let clickedChapterIdx = chapters.firstIndex(where: { $0.fullPath == path }) ?? 0
            let resumePath = chapters.indices.contains(clickedChapterIdx)
                ? chapters[clickedChapterIdx].fullPath : path
            let saved = progress.entries[resumePath] ?? progress.entries[path]
            let local = saved?.page ?? 0
            let start = w.resumeGlobalIndex(chapterIdx: clickedChapterIdx, localIdx: local)
            currentIdx = start
            work = w
            let s = min(max(w.spreadOf(start), 0), max(w.spreads.count - 1, 0))
            pos = CGFloat(s)
            posTarget = s
            prefetch(around: start, in: w)
            publishExternal()
        } catch APIError.status(let s) where [404, 401, 403].contains(s) {
            error = "フォルダが見つかりません (HTTP \(s))"
        } catch {
            self.error = error.localizedDescription
        }
        loading = false
    }
}

extension Array {
    subscript(safe index: Int) -> Element? {
        indices.contains(index) ? self[index] : nil
    }
}
