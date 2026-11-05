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
    @State private var pendingIdx: Int?
    @State private var lastNavAt = Date.distantPast
    @State private var navTask: Task<Void, Never>?
    /// tapForward equivalent — session-only like the web. true = left zone
    /// turns forward (web default), false = right zone turns forward.
    @State private var tapForwardLeft = true
    /// In-flight page turn for the custom ZStack pager (replaces the old
    /// TabView strip slide). nil = settled.
    @State private var turnAnim: PageTurn?
    /// Damped offset applied to the settled spread when a drag points past
    /// the first/last spread (edge rubber-band).
    @State private var rubberX: CGFloat = 0
    /// A pinch gesture is in flight on some page — page drags suspend.
    @State private var pinchActive = false
    /// Monotonic token so a superseded turn animation can't clear its
    /// successor's `turnAnim` from a stale completion.
    @State private var turnSeq = 0

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

    /// Custom pager — asymmetric page-stack turn (replaces the old TabView
    /// strip slide): forward slides the current spread right off-screen
    /// revealing the next spread static beneath; backward slides the
    /// previous spread in from the right edge covering the current one.
    /// `turnAnim` is the only transition state; `currentIdx` already points
    /// at the destination once a turn commits (web parity: counter/slider
    /// move at turn start, the strip catches up visually).
    private func pager(work: ReaderWork, in size: CGSize) -> some View {
        let settled = work.spreadOf(currentIdx)
        return ZStack {
            ForEach(pagerSpreadIndices(work: work, settled: settled), id: \.self) { s in
                spreadCell(work: work, spread: s, pages: work.spreads[s], in: size)
                    .opacity(opacityFor(s, settled: settled))
                    .offset(x: offsetFor(s, settled: settled, size: size))
                    .zIndex(zIndexFor(s, settled: settled))
                    .allowsHitTesting(turnAnim == nil && s == settled)
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

    /// Spread indices the pager renders. Idle keeps the settled spread plus
    /// its two neighbours alive at opacity 0 (image/AR warm-up, matching the
    /// old TabView neighbourhood); a turn keeps only `from`/`to`.
    private func pagerSpreadIndices(work: ReaderWork, settled: Int) -> [Int] {
        var set = Set<Int>([settled])
        if let t = turnAnim {
            set.insert(t.from); set.insert(t.to)
        } else {
            if settled > 0 { set.insert(settled - 1) }
            if settled + 1 < work.spreads.count { set.insert(settled + 1) }
        }
        return set.filter { work.spreads.indices.contains($0) }.sorted()
    }

    /// Only layers participating in the current view are opaque — hidden
    /// neighbours must never peek out from behind a narrower settled spread.
    private func opacityFor(_ s: Int, settled: Int) -> Double {
        if let t = turnAnim { return (s == t.from || s == t.to) ? 1 : 0 }
        return s == settled ? 1 : 0
    }

    /// Visual offset. Forward: `from` travels 0→+width (next beneath is
    /// static). Backward: `to` travels +width→0 on top (current beneath is
    /// static). Idle: `rubberX` edge feedback on the settled spread.
    private func offsetFor(_ s: Int, settled: Int, size: CGSize) -> CGFloat {
        if let t = turnAnim {
            if t.forward { return s == t.from ? t.progress * size.width : 0 }
            return s == t.to ? (1 - t.progress) * size.width : 0
        }
        return s == settled ? rubberX : 0
    }

    /// Forward puts the outgoing spread on top; backward puts the incoming
    /// spread on top — the asymmetry the turn animation is built on.
    private func zIndexFor(_ s: Int, settled: Int) -> Double {
        if let t = turnAnim {
            return (t.forward ? s == t.from : s == t.to) ? 1 : 0
        }
        return s == settled ? 1 : 0
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

    private func snapTo(_ idx: Int, in work: ReaderWork, animated: Bool = true) {
        pendingIdx = nil
        guard idx >= 0 && idx < work.images.count else { return }
        let target = work.spreadOf(idx)
        let from = work.spreadOf(currentIdx)
        if animated && target != from && work.spreads.indices.contains(target) {
            beginTurn(from: from, to: target, landingIdx: idx, in: work)
        } else {
            settleTurnInstantly()
            applyLanding(idx, in: work)
        }
    }

    /// Side effects of arriving at `idx` — the old snapTo body minus the
    /// animation concern. Runs at turn START for animated turns too, so the
    /// counter/slider/external display move with the gesture (web parity).
    private func applyLanding(_ idx: Int, in work: ReaderWork) {
        currentIdx = idx
        scheduleSave()
        prefetch(around: idx, in: work)
        publishExternal()
    }

    /// Start a programmatic (non-interactive) turn: `currentIdx` lands on
    /// `landingIdx` immediately, `turnAnim` plays the slide, and the layer
    /// swap completes when progress reaches 1.
    private func beginTurn(from: Int, to: Int, landingIdx: Int, in work: ReaderWork) {
        settleTurnInstantly()
        turnAnim = PageTurn(from: from, to: to, forward: to > from,
                            progress: 0, interactive: false)
        applyLanding(landingIdx, in: work)
        animateTurnProgress(to: 1)
    }

    /// Drop any in-flight turn without animating — the view settles on
    /// `currentIdx`'s spread, which is already the committed destination
    /// for programmatic turns (or the drag origin for interactive ones).
    private func settleTurnInstantly() {
        turnAnim = nil
        rubberX = 0
    }

    /// Animate `turnAnim.progress` to `target` (1 = commit, 0 = cancel)
    /// and clear the turn when it finishes. `seq` guards the completion
    /// against a newer turn having replaced this one mid-flight.
    private func animateTurnProgress(to target: CGFloat) {
        guard turnAnim != nil else { return }
        turnSeq += 1
        let seq = turnSeq
        withAnimation(
            .timingCurve(0.25, 0.1, 0.25, 1.0, duration: 0.3),
            completionCriteria: .logicallyComplete
        ) {
            turnAnim?.progress = target
        } completion: {
            if turnSeq == seq { turnAnim = nil }
        }
    }

    // MARK: - Page-turn drag

    /// Live drag → turn/rubber state. Direction locks on first decided
    /// move; a programmatic turn in flight is settled first so a fresh
    /// swipe always owns the screen.
    private func pagerDragChanged(_ v: DragGesture.Value,
                                  work: ReaderWork, size: CGSize) {
        guard !zoomed, !pinchActive else { return }
        if let t = turnAnim, !t.interactive { settleTurnInstantly() }
        let dx = v.translation.width
        let dy = v.translation.height
        // A locked interactive turn owns the gesture until release —
        // progress follows the finger in the turn's own direction frame,
        // so reversing the drag decays it toward 0 (never re-decides).
        if var t = turnAnim, t.interactive {
            t.progress = PageTurnMath.dragProgressSigned(
                dx: t.forward ? dx : -dx, viewWidth: size.width)
            turnAnim = t
            return
        }
        guard let dir = PageTurnMath.dragDirection(dx: dx, dy: dy) else { return }
        let from = work.spreadOf(currentIdx)
        let to = from + dir
        guard to >= 0 && to < work.spreads.count else {
            // Edge rubber-band: damped follow, never commits.
            turnAnim = nil
            rubberX = dx * PageTurnMath.edgeRubber
            return
        }
        rubberX = 0
        turnAnim = PageTurn(
            from: from, to: to, forward: dir > 0,
            progress: PageTurnMath.dragProgress(dx: dir > 0 ? dx : -dx,
                                                viewWidth: size.width),
            interactive: true)
    }

    /// Release: commit when the flick/distance thresholds say so, else
    /// spring the turn back. Only interactive (drag-created) turns decide
    /// here — programmatic ones are already animating to completion.
    private func pagerDragEnded(_ v: DragGesture.Value,
                                work: ReaderWork, size: CGSize) {
        guard let t = turnAnim, t.interactive else {
            if abs(rubberX) > 0.01 {
                withAnimation(.spring(response: 0.3, dampingFraction: 0.8)) {
                    rubberX = 0
                }
            }
            return
        }
        // Sign-normalize into the locked direction's frame. A pinch that
        // started mid-drag poisons the gesture — never commit it.
        let sgn: CGFloat = t.forward ? 1 : -1
        let commit = !pinchActive && PageTurnMath.shouldCommit(
            translation: v.translation.width * sgn,
            predicted: v.predictedEndTranslation.width * sgn,
            viewWidth: size.width)
        if commit, let landing = work.spreads[safe: t.to]?.first {
            turnAnim?.interactive = false
            applyLanding(landing, in: work)
            animateTurnProgress(to: 1)
        } else {
            animateTurnProgress(to: 0)
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
        // Spreads regrouped — any in-flight turn references stale indices.
        // Web parity: orientation/mode changes snap without animation.
        settleTurnInstantly()
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
