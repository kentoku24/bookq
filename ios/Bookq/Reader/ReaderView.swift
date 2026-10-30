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
    @State private var pendingIdx: Int?
    @State private var lastNavAt = Date.distantPast
    @State private var navTask: Task<Void, Never>?
    /// tapForward equivalent — session-only like the web. true = left zone
    /// turns forward (web default), false = right zone turns forward.
    @State private var tapForwardLeft = true

    private var pageMode: ReaderPageMode {
        let landscape = viewportSize.width >= viewportSize.height
        return (landscape && landscapeSpread) ? .spread : .single
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
        }
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

    private func pager(work: ReaderWork, in size: CGSize) -> some View {
        let spreadIdx = work.spreadOf(currentIdx)
        return TabView(selection: spreadBinding(work)) {
            ForEach(Array(work.spreads.enumerated()), id: \.offset) { s, pages in
                spreadCell(work: work, spread: s, pages: pages, in: size)
                    .tag(s)
            }
        }
        .tabViewStyle(.page(indexDisplayMode: .never))
        .environment(\.layoutDirection, .rightToLeft)
        .id(work.pageMode)                       // rebuild on mode change
        // Tap zones (web zoneActionAt): edge 30% zones follow tapForward
        // (default left = forward), center toggles chrome. SpatialTap is
        // single-tap; zoom pan/pinch live inside ZoomableScrollView.
        .gesture(SpatialTapGesture().onEnded { ev in
            let frac = ev.location.x / max(size.width, 1)
            if frac < 0.30 { turn(tapForwardLeft ? 1 : -1, in: work) }
            else if frac > 0.70 { turn(tapForwardLeft ? -1 : 1, in: work) }
            else { withAnimation { showChrome.toggle() } }
        })
        // Web keyboard parity (iPad/hardware keyboard): →=next, ←=prev,
        // Home/End = chapter edges, G = overview.
        .focusable()
        .focusEffectDisabled()
        .onKeyPress(.rightArrow) { turn(1, in: work); return .handled }
        .onKeyPress(.leftArrow) { turn(-1, in: work); return .handled }
        .onKeyPress(keys: [.init("g"), .init("G")]) { _ in
            showOverview = true; return .handled
        }
        .onKeyPress(.home) { snapTo(chapterEdge(0, in: work), in: work); return .handled }
        .onKeyPress(.end) { snapTo(chapterEdge(-1, in: work), in: work); return .handled }
        .overlay(alignment: .trailing) { chapterLabel(work, spread: spreadIdx) }
        .sheet(isPresented: $showOverview) {
            if let api = model.api {
                OverviewView(work: work, api: api, currentIdx: $currentIdx) {
                    showOverview = false
                }
            }
        }
    }

    /// Global index of the current chapter's first (0) or last (-1) real page.
    private func chapterEdge(_ which: Int, in work: ReaderWork) -> Int {
        let info = work.chapterInfo(at: currentIdx)
        let local = which == 0 ? 0 : work.chapters[info.chapterIdx].images.count - 1
        return work.resumeGlobalIndex(chapterIdx: info.chapterIdx, localIdx: local)
    }

    /// Bind TabView selection (spread index) → currentIdx (global page idx).
    private func spreadBinding(_ work: ReaderWork) -> Binding<Int> {
        Binding(
            get: { work.spreadOf(currentIdx) },
            set: { s in
                guard s < work.spreads.count, let first = work.spreads[s].first else { return }
                if first != currentIdx { snapTo(first, in: work) }
            }
        )
    }

    /// One spread cell: 1–2 pages fit into the viewport using measured ARs
    /// (`spreadMetrics` equivalent — pages share one height, width = ar*h).
    private func spreadCell(work: ReaderWork, spread s: Int,
                            pages: [Int], in size: CGSize) -> some View {
        let ars = pages.map { pageAR[$0] ?? 0.7 }
        let arSum = max(ars.reduce(0, +), 0.001)
        let gap: CGFloat = 5
        var h = size.height
        var w = arSum * h + gap * CGFloat(ars.count - 1)
        if w > size.width {
            h = (size.width - gap * CGFloat(ars.count - 1)) / arSum
            w = size.width
        }
        return HStack(spacing: gap) {
            ForEach(Array(pages.enumerated()), id: \.offset) { pos, idx in
                pageView(work: work, idx: idx)
                    .frame(width: ars[pos] * h, height: h)
            }
        }
        .frame(width: w, height: h)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func pageView(work: ReaderWork, idx: Int) -> some View {
        let page = work.images[idx]
        let url = model.api?.pageURL(folder: page.folder, name: page.name,
                                     original: model.config.originalImages)
        return ZoomableScrollView(
            onZoomChange: { z in zoomed = z },
            onPinchOut: { showOverview = true }
        ) {
            RemoteImageView(url: url, contentMode: .fit) { img in
                let ar = img.size.width / max(img.size.height, 1)
                if pageAR[idx] != ar { pageAR[idx] = ar }
            }
            .background(Color.black)
        }
    }

    /// Chapter title pill shown on chapter-start spreads (chapterLabels).
    private func chapterLabel(_ work: ReaderWork, spread s: Int) -> some View {
        Group {
            if work.isCrossChapter,
               let first = work.spreads[safe: s]?.first {
                let info = work.chapterInfo(at: first)
                Text(info.name)
                    .font(.caption).foregroundStyle(.white)
                    .padding(.horizontal, 10).padding(.vertical, 5)
                    .background(.black.opacity(0.55), in: Capsule())
                    .padding(.trailing, 12)
                    .transition(.opacity)
            }
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
                    if let work, work.isCrossChapter {
                        Text(work.chapterInfo(at: currentIdx).name)
                            .font(.caption2).foregroundStyle(.white.opacity(0.8))
                            .lineLimit(1)
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
            Button {
                landscapeSpread.toggle()
                rebuild()
            } label: {
                Image(systemName: pageMode == .spread ? "rectangle.split.2x1" : "rectangle.portrait")
                    .padding(10)
                    .background(.black.opacity(0.5), in: Circle())
            }
            .accessibilityLabel("spread toggle")
            // tapmode-controls equivalent: swaps the edge tap zones.
            Button {
                tapForwardLeft.toggle()
            } label: {
                Image(systemName: tapForwardLeft
                      ? "arrowtriangle.left.fill" : "arrowtriangle.right.fill")
                    .padding(10)
                    .background(tapForwardLeft ? .black.opacity(0.5) : Color.accentColor,
                                in: Circle())
            }
            .accessibilityLabel("タップ送り方向")
            Button {
                showOverview = true
            } label: {
                Image(systemName: "rectangle.grid.2x2")
                    .padding(10)
                    .background(.black.opacity(0.5), in: Circle())
            }
            .accessibilityLabel("複数ページ")
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
    /// Rapid taps coalesce: the first lands instantly, follow-ups merge into
    /// one pending target so the pager plays a single animation instead of
    /// queueing one per tap (web's instant-strip equivalent).
    private func turn(_ dir: Int, in work: ReaderWork) {
        let base = pendingIdx ?? currentIdx
        guard let next = dir > 0
            ? work.nextSpreadFirstPage(after: base)
            : work.prevSpreadFirstPage(before: base) else { return }
        let now = Date()
        if now.timeIntervalSince(lastNavAt) > 0.25 {
            lastNavAt = now
            snapTo(next, in: work)
        } else {
            pendingIdx = next
            navTask?.cancel()
            navTask = Task {
                try? await Task.sleep(nanoseconds: 160_000_000)
                guard !Task.isCancelled else { return }
                await MainActor.run {
                    guard let t = pendingIdx else { return }
                    pendingIdx = nil
                    lastNavAt = Date()
                    snapTo(t, in: work)
                }
            }
        }
    }

    private func snapTo(_ idx: Int, in work: ReaderWork) {
        pendingIdx = nil
        guard idx >= 0 && idx < work.images.count else { return }
        currentIdx = idx
        scheduleSave()
        prefetch(around: idx, in: work)
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
            // Plain fallback: no tree data → fetch /api/images (web behavior)
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
            prefetch(around: start, in: w)
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
