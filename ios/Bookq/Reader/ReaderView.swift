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
    /// Pages currently zoomed, keyed by global page index. Tracked per
    /// page — not a single flag — so a zoomed neighbour cell that stays
    /// mounted doesn't poison the drag guard after a turn, while turning
    /// back to it still correctly refuses page drags.
    @State private var zoomedPages: Set<Int> = []
    @State private var pageAR: [Int: CGFloat] = [:]  // measured AR per page
    /// Web `lastAR` parity — aspect ratio assumed for pages not yet
    /// measured, so an unmeasured spread doesn't fall back to a fixed 0.7.
    @State private var lastAR: CGFloat = 0.7
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
    /// keys, slider, overview) just move this target — `pos` chases it
    /// at a steady rate, so rapid taps each advance one spread instead
    /// of queueing.
    @State private var posTarget = 0
    /// Finger-driven drag: `pos` captured when the gesture locked its
    /// direction (nil = not dragging). Cancel returns `pos` to `posTarget`.
    @State private var dragBase: CGFloat?
    /// A pinch gesture is in flight on some page — page drags suspend.
    @State private var pinchActive = false
    /// True while `pos` is chasing `posTarget` — hit-testing/drag-lock
    /// wait because the on-screen slide is still in flight.
    @State private var posAnimating = false
    /// Drives `pos` toward `posTarget` at a steady flip rate (see
    /// `animatePos`). `pos` stays the honest visual position — written
    /// per tick — so the pager simply renders whatever `pos` shows.
    @State private var chaseTask: Task<Void, Never>?
    /// Finger translation at the moment a drag locked its direction —
    /// deltas are measured from here so a late lock (post-animation)
    /// doesn't jump by the pre-lock travel.
    @State private var dragTransBase: CGFloat = 0

    private var pageMode: ReaderPageMode {
        ReaderWork.pageMode(for: viewportSize, landscapeSpread: landscapeSpread)
    }

    var body: some View {
        GeometryReader { geo in
            ZStack {
                Color.black
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
        // Full-screen geometry: geo.size spans the unsafe regions too, so
        // spreadFit sizes pages against the whole display like the web
        // viewer's window.innerWidth/innerHeight. The chrome overlay keeps
        // its own safe-area insets — overlay content is inset by the
        // system even though the geometry underneath ignores them.
        .ignoresSafeArea()
        .statusBarHidden(!showChrome)
        .overlay(alignment: .top) { if showChrome { chrome } }
        .onDisappear {
            chaseTask?.cancel()
            saveCurrentPosition()
            progress.sync?.flush()          // pagehide → flush pending PUT
            ext.endPresentation()           // external display → back to mirroring
        }
        .modifier(ExternalAccessoryModifier(ext: ext))
        .onChange(of: pageAR) { _, _ in publishExternal() }
        // lastAR can move without pageAR (re-measure of an already-recorded
        // value) — the external display's unmeasured-page fit would stay
        // stale until the next turn otherwise.
        .onChange(of: lastAR) { _, _ in publishExternal() }
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
                PagerLayer(pos: pos, index: s, width: size.width,
                           count: work.spreads.count,
                           fullRes: pagerSettled) { tier in
                    spreadCell(work: work, spread: s, pages: work.spreads[s],
                               tier: tier, in: size)
                }
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
        dragBase == nil && !posAnimating && pos == CGFloat(posTarget)
    }

    /// Spread indices the pager renders: a window around `pos`. At rest
    /// it collapses to `pos`±1 (the old TabView neighbourhood); while a
    /// chase is in flight it widens to the live-cell radius so spreads
    /// the flip will pass through already have their fast-tier fetch in
    /// flight — without mounting a dead placeholder for every corridor
    /// spread on long jumps.
    private func pagerSpreadIndices(work: ReaderWork) -> [Int] {
        let n = work.spreads.count
        let pad = posAnimating ? Int(pagerLiveDistance) + 1 : 1
        let lo = Int(floor(pos)) - pad
        let hi = Int(ceil(pos)) + pad
        var set = Set<Int>()
        for s in lo...hi where s >= 0 && s < n { set.insert(s) }
        return set.sorted()
    }

    /// True when a page of the spread under `pos` is zoomed — the
    /// ScrollView's pan/zoom owns the gesture then, so page drags must
    /// stay out. `Int(pos)` truncates toward the visible top layer
    /// (`floor` for non-negative pos, 0 under start-edge rubber-band).
    private func spreadZoomed(in work: ReaderWork) -> Bool {
        let n = work.spreads.count
        guard n > 0 else { return false }
        let s = min(max(Int(pos), 0), n - 1)
        return work.spreads[s].contains { zoomedPages.contains($0) }
    }

    /// Current page's chapter spread range — web
    /// `getChapterSpreadRange(getCurrentChapterInfo(currentIdx))`.
    /// Swipes and arrow keys clamp turns to it; taps cross chapters.
    private func chapterSpreadBounds(in work: ReaderWork) -> (lo: Int, hi: Int) {
        let ci = work.chapterInfo(at: currentIdx).chapterIdx
        return work.chapterSpreadRange(ci)
    }

    /// Global index of the current chapter's first (0) or last (-1)
    /// navigation target — web Home/End: `spreads[r.lo/hi].pages[0]`,
    /// i.e. the first page of the edge spread, not the chapter's last
    /// page itself.
    private func chapterEdge(_ which: Int, in work: ReaderWork) -> Int {
        let info = work.chapterInfo(at: currentIdx)
        if which == 0 {
            return work.resumeGlobalIndex(chapterIdx: info.chapterIdx, localIdx: 0)
        }
        let b = chapterSpreadBounds(in: work)
        if let first = work.spreads[safe: b.hi]?.first { return first }
        return work.resumeGlobalIndex(
            chapterIdx: info.chapterIdx,
            localIdx: work.chapters[info.chapterIdx].images.count - 1)
    }

    /// One spread cell: 1–2 pages fit into the viewport using measured ARs
    /// (`spreadMetrics` equivalent — pages share one height, width = ar*h).
    /// RTL lives here (not on the pager ZStack) so manga page order stays
    /// right-to-left without mirroring the pager's `.offset` turns.
    @ViewBuilder
    private func spreadCell(work: ReaderWork, spread s: Int,
                            pages: [Int], tier: PagerCellTier,
                            in size: CGSize) -> some View {
        if tier == .placeholder {
            // Far window spreads are always opacity 0 — a plain colour
            // keeps the slide math without mounting image views.
            Color.black
        } else {
            let fast = tier == .fast
            let ars = pages.map { pageAR[$0] ?? lastAR }
            let gap: CGFloat = 5
            let fit = ReaderWork.spreadFit(ars: ars, gap: gap, in: size)
            HStack(spacing: gap) {
                ForEach(Array(pages.enumerated()), id: \.offset) { pos, idx in
                    pageView(work: work, idx: idx, fast: fast)
                        .frame(width: ars[pos] * fit.height, height: fit.height)
                }
            }
            .environment(\.layoutDirection, .rightToLeft)
            .frame(width: fit.width, height: fit.height)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            // Keeps the cell hittable while its ZoomableScrollView is
            // suspended mid-turn — the pager's tap/drag gestures live on
            // the parent ZStack and need a hittable descendant.
            .contentShape(Rectangle())
        }
    }

    private func pageView(work: ReaderWork, idx: Int, fast: Bool) -> some View {
        let page = work.images[idx]
        // Fast tier always uses the normalized /view rendition at reduced
        // width — even under originalImages, since it is only the
        // flip-through preview; the settled spread loads full quality.
        let url = model.api?.pageURL(
            folder: page.folder, name: page.name,
            original: fast ? false : model.config.originalImages,
            width: fast ? PageTurnMath.fastPageWidth : nil)
        return ZoomableScrollView(
            onZoomChange: { z in
                if z { zoomedPages.insert(idx) } else { zoomedPages.remove(idx) }
            },
            onPinchOut: { showOverview = true },
            onPinchState: { active in pinchActive = active }
        ) {
            RemoteImageView(url: url, contentMode: .fit) { img in
                let ar = img.size.width / max(img.size.height, 1)
                if lastAR != ar { lastAR = ar }
                if pageAR[idx] != ar { pageAR[idx] = ar }
            }
            .background(Color.black)
        }
        // Scroll/zoom suspend only while the pager is mid-turn — taps
        // and the page-turn drag must stay live so rapid input keeps
        // counting (gating the whole PagerLayer killed the parent
        // gestures too, making turns ignore taps until settle).
        .allowsHitTesting(pagerSettled)
        // A destroyed cell (placeholder swap / window slide) must not
        // leave a stale zoom entry — its next mount starts at 1x.
        .onDisappear { zoomedPages.remove(idx) }
    }

    // MARK: - Chrome

    /// Wide viewport → landscape chrome (right-edge vertical rail, per
    /// the iPhone Duo HIG "vertical controls" pattern: side placement
    /// preserves the full-height spread's vertical space). Portrait keeps
    /// the classic top bar + bottom slider. Same predicate as pageMode's
    /// orientation term and the landscape-spread toggle's visibility.
    private var isLandscape: Bool {
        viewportSize.width >= viewportSize.height
    }

    private var chrome: some View {
        Group {
            if isLandscape { landscapeChrome } else { portraitChrome }
        }
        .transition(.opacity)
    }

    /// Portrait chrome — top bar (close / counter / mode buttons) plus
    /// the bottom chapter slider.
    private var portraitChrome: some View {
        VStack {
            HStack {
                Button { dismiss() } label: {
                    Image(systemName: "xmark")
                        .padding(10)
                        .background(.black.opacity(0.5), in: Circle())
                }
                .accessibilityIdentifier("readerClose")
                Spacer()
                VStack(spacing: 2) {
                    Text(counterText).font(.subheadline).monospacedDigit()
                        .accessibilityIdentifier("readerCounter")
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

    // MARK: - Landscape rail chrome

    /// Split side rails (iPhone Duo HIG "vertical controls"): mode
    /// controls the user reaches for while reading sit on the LEFT
    /// rail (close / spread on-off / spread offset, top→bottom), while
    /// status + navigation stay on the RIGHT rail (counter, overview,
    /// overflow menu, chapter slider filling the remaining height).
    /// Tap-direction picks mirror the web `#tapmode-controls`: two
    /// corner buttons at the bottom-left / bottom-right instead of a
    /// grouped pair. Pages keep their max-size centered fit — on wide
    /// spreads a rail may overlap the page edge, so each rail has a
    /// hit-test transparent scrim keeping its controls legible.
    private var landscapeChrome: some View {
        HStack(spacing: 0) {
            leftRailColumn
            Spacer(minLength: 0)
            railColumn
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .overlay(alignment: .bottom) { tapDirectionCorners }
    }

    /// Bottom-corner tap-direction pair — web `#tapmode-controls`
    /// parity: ◀ bottom-left = left tap advances, ▶ bottom-right =
    /// right tap advances; the active direction is highlighted.
    private var tapDirectionCorners: some View {
        HStack {
            tapDirectionButton(systemName: "arrowtriangle.left.fill",
                               label: "左タップで次へ",
                               active: tapForwardLeft) {
                tapForwardLeft = true
            }
            Spacer()
            tapDirectionButton(systemName: "arrowtriangle.right.fill",
                               label: "右タップで次へ",
                               active: !tapForwardLeft) {
                tapForwardLeft = false
            }
        }
        .padding(.horizontal, 12)
        .padding(.bottom, 12)
    }

    private func tapDirectionButton(systemName: String, label: String,
                                    active: Bool,
                                    action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: systemName)
                .padding(10)
                .background(active ? Color.accentColor : .black.opacity(0.5),
                            in: Circle())
        }
        .accessibilityLabel(label)
    }

    /// Left-edge rail — top→bottom: close, spread on/off, spread
    /// offset (spread mode only). Items hug the top; the column's
    /// empty height stays hit-test transparent so edge taps still
    /// turn pages.
    private var leftRailColumn: some View {
        VStack(spacing: 12) {
            Button { dismiss() } label: {
                Image(systemName: "xmark")
                    .padding(10)
                    .background(.black.opacity(0.5), in: Circle())
            }
            .accessibilityIdentifier("readerClose")
            Button {
                landscapeSpread.toggle()
                rebuild()
            } label: {
                Image(systemName: pageMode == .spread
                      ? "rectangle.split.2x1" : "rectangle.portrait")
                    .padding(10)
                    .background(.black.opacity(0.5), in: Circle())
            }
            .accessibilityLabel("spread toggle")
            if pageMode == .spread {
                Button {
                    spreadOffset = spreadOffset == 0 ? 1 : 0
                    rebuild()
                } label: {
                    Image(systemName: spreadOffset == 1 ? "book" : "book.pages")
                        .padding(10)
                        .background(spreadOffset == 1 ? Color.accentColor
                                    : .black.opacity(0.5), in: Circle())
                }
                .accessibilityLabel("spread offset")
            }
        }
        .foregroundStyle(.white)
        .padding(.top, 12)
        .padding(.leading, 6)
        // Same width as the right rail — symmetric rails keep the
        // spread's side margins visually balanced.
        .frame(width: 76)
        .frame(maxHeight: .infinity, alignment: .top)
        .background(alignment: .leading) { leadingRailScrim }
    }

    private var railColumn: some View {
        VStack(spacing: 12) {
            counterRailBadge
            Button { showOverview = true } label: {
                Image(systemName: "rectangle.grid.2x2")
                    .padding(10)
                    .background(.black.opacity(0.5), in: Circle())
            }
            .accessibilityLabel("複数ページ")
            railMenu
            if let work {
                verticalSliderRow(work)
            }
        }
        .foregroundStyle(.white)
        .padding(.top, 12)
        // Extra bottom room keeps the slider clear of the bottom-right
        // tap-direction button (`tapDirectionCorners` overlays this
        // same corner); without it the thumb's chapter-end position
        // sits under the button.
        .padding(.bottom, 56)
        .padding(.trailing, 6)
        // Fixed width is load-bearing: the slider's GeometryReader is
        // infinitely flexible, so without it the column expands to the
        // full proposal and VStack centers every item mid-screen.
        .frame(width: 76)
        .frame(maxHeight: .infinity)
        .background(alignment: .trailing) { railScrim }
    }

    @ViewBuilder
    private var counterRailBadge: some View {
        if !counterText.isEmpty {
            Text(counterText)
                .font(.caption2).monospacedDigit()
                .lineLimit(1)
                .accessibilityIdentifier("readerCounter")
                .padding(.horizontal, 8).padding(.vertical, 4)
                .background(.black.opacity(0.5), in: Capsule())
        }
    }

    /// Soft fade behind the rail so controls stay legible where a wide
    /// spread reaches under them. Hit-test transparent — taps in the
    /// margin still turn pages.
    private var railScrim: some View {
        LinearGradient(colors: [.clear, .black.opacity(0.4)],
                       startPoint: .leading, endPoint: .trailing)
            .frame(width: 120)
            .allowsHitTesting(false)
    }

    /// Mirror of `railScrim` for the left rail — dark at the screen's
    /// leading edge fading toward the page.
    private var leadingRailScrim: some View {
        LinearGradient(colors: [.black.opacity(0.4), .clear],
                       startPoint: .leading, endPoint: .trailing)
            .frame(width: 120)
            .allowsHitTesting(false)
    }

    /// Landscape overflow menu — what's left once the read-mode
    /// controls moved to the left rail and tap direction to the bottom
    /// corners: external-display toggle + bring-up diagnostic. Hidden
    /// entirely when there's nothing to show.
    @ViewBuilder
    private var railMenu: some View {
        if ext.accessoryAvailable || ext.connected || !ext.userEnabled
            || ext.externalScreen != nil {
            Menu {
                // External display toggle: shown while a display is
                // attached or projecting, and stays while disabled so
                // it can be re-enabled (same rule as `modeButtons`).
                if ext.accessoryAvailable || ext.connected || !ext.userEnabled {
                    Button { ext.userEnabled.toggle() } label: {
                        Label("外部ディスプレイ",
                              systemImage: ext.connected && ext.userEnabled
                                ? "tv.fill" : "tv")
                    }
                }
                // External-display bring-up diagnostic (temporary).
                if let s = ext.externalScreen {
                    Text(ExternalDisplayStore.describe(s))
                } else if ext.accessoryAvailable {
                    Text("ext: display available")
                }
            } label: {
                Image(systemName: "ellipsis")
                    .padding(10)
                    .background(.black.opacity(0.5), in: Circle())
            }
            .accessibilityLabel("その他")
        }
    }

    /// Landscape chapter slider — same chapter-scoped value math as
    /// `sliderRow`, rendered vertically in the rail (top = chapter
    /// start, bottom = chapter end).
    private func verticalSliderRow(_ work: ReaderWork) -> some View {
        let span = work.chapterSliderMax(at: currentIdx)
        return VerticalSlider(
            value: Binding(
                get: { Double(work.idxToChapterSlider(currentIdx)) },
                set: { v in
                    if let idx = work.chapterSliderToIdx(
                        Int(v.rounded()), at: currentIdx) {
                        snapTo(idx, in: work)
                    }
                }),
            range: 0...Double(max(span, 1)),
            enabled: span > 0
        )
        .accessibilityIdentifier("readerSlider")
    }

    /// Chapter-local slider (web `syncChapterSlider` family): the range
    /// spans only the CURRENT chapter's spreads — RTL puts the chapter's
    /// first page at the right end and its last page at the left. When
    /// `currentIdx` crosses into another chapter the helpers re-derive
    /// the range, so the slider remaps automatically. Episode changes
    /// come only from page turns at the edges, never the slider.
    private func sliderRow(_ work: ReaderWork) -> some View {
        let span = work.chapterSliderMax(at: currentIdx)
        return Slider(
            value: Binding(
                get: { Double(work.idxToChapterSlider(currentIdx)) },
                set: { v in
                    if let idx = work.chapterSliderToIdx(
                        Int(v.rounded()), at: currentIdx) {
                        snapTo(idx, in: work)
                    }
                }),
            in: 0...Double(max(span, 1)),
            step: 1
        )
        .disabled(span <= 0)
        .environment(\.layoutDirection, .rightToLeft)
        .padding(.horizontal, 20)
        .padding(.bottom, 30)
        .tint(.white)
        .accessibilityIdentifier("readerSlider")
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

    /// Spread-granularity turn — web navTurn/goNext/goPrev. Every turn
    /// path crosses chapters at the edges (taps, arrow keys; swipes
    /// cross via the book-wide drag clamp) — episode changes only ever
    /// come from page turns, never jumps. `crossChapter` stays for
    /// goNext/goPrev signature parity; all callers use the default.
    /// Every accepted turn lands immediately: `posTarget` steps one
    /// spread and the `pos` chase follows it, so rapid taps each count
    /// as a turn (the pager fast-forwards instead of queueing).
    private func turn(_ dir: Int, in work: ReaderWork, crossChapter: Bool = true) {
        let s = work.spreadOf(currentIdx)
        var t = dir > 0 ? s + 1 : s - 1
        if !crossChapter {
            let b = chapterSpreadBounds(in: work)
            t = min(max(t, b.lo), b.hi)
            if t == s { return }
        }
        guard work.spreads.indices.contains(t),
              let first = work.spreads[t].first else { return }
        snapTo(first, in: work)
    }

    /// Navigate to `idx` (any page — the spread it belongs to is the turn
    /// unit). `currentIdx` lands immediately; `pos` animates to the target
    /// spread unless `animated` is false (Home key, mode rebuilds).
    private func snapTo(_ idx: Int, in work: ReaderWork, animated: Bool = true) {
        guard idx >= 0 && idx < work.images.count else { return }
        // Programmatic navigation owns `pos` now — drop any in-flight
        // drag so chase ticks and finger deltas can't fight over it.
        dragBase = nil
        let target = work.spreadOf(idx)
        guard work.spreads.indices.contains(target) else {
            applyLanding(idx, in: work)
            return
        }
        if target != posTarget {
            if animated {
                animatePos(to: target)
            } else {
                chaseTask?.cancel()
                posAnimating = false
                posTarget = target
                pos = CGFloat(target)
            }
        }
        applyLanding(idx, in: work)
    }

    /// Advance `pos` toward spread `target`. A spring cannot keep up
    /// with rapid taps — its travel time grows with distance, so a
    /// `posTarget` running ahead caps the flip rate near ~2 spreads/s.
    /// Instead `pos` chases `posTarget` at a steady rate (scaled up for
    /// long jumps), easing out near the target; every tap still counts
    /// as its own turn because `posTarget` steps independently.
    private func animatePos(to target: Int) {
        posTarget = target
        posAnimating = true
        chaseTask?.cancel()
        chaseTask = Task { @MainActor in
            var last = Date()
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 8_000_000)
                let now = Date()
                // Cap dt so a hitch doesn't teleport `pos` — but the cap
                // must stay well above worst-case tick jitter. At 0.05 a
                // saturated main thread (ticks ~200ms apart) advanced pos
                // by only 0.05s×speed per tick ≈ 2 spreads/s — the exact
                // "2 pages per second" ceiling seen on device. 0.2s keeps
                // the chase honest to real elapsed time under jitter.
                stepPos(dt: min(now.timeIntervalSince(last), 0.2))
                last = now
                if pos == CGFloat(posTarget) {
                    posAnimating = false
                    return
                }
            }
        }
    }

    /// One chase tick. Speed scales with remaining distance (long
    /// overview/slider jumps blur through quickly) and eases off inside
    /// the last ~0.12 spread so single turns keep a soft landing.
    private func stepPos(dt: TimeInterval) {
        let diff = CGFloat(posTarget) - pos
        let dist = abs(diff)
        if dist < 0.02 {
            pos = CGFloat(posTarget)
            return
        }
        let scaled = PageTurnMath.chaseSpreadsPerSecond
            * min(max(dist / 3, 1), 10)
        let speed = min(scaled, dist / 0.12)
        // Clamp the step to the remaining distance: a stalled main thread
        // (dt up to 0.2s) would otherwise overshoot posTarget and bounce
        // around it for several ticks before the dist<0.02 snap.
        pos += (diff > 0 ? 1 : -1) * min(speed * CGFloat(dt), dist)
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
        guard !pinchActive, !spreadZoomed(in: work) else { return }
        let dx = v.translation.width
        let dy = v.translation.height
        if dragBase == nil {
            // Only take over once the gesture is decided-horizontal; a
            // mid-animation `pos` is a valid base (cancel returns to
            // `posTarget`, the interrupted destination). New drags can't
            // lock while a programmatic slide is still on screen — the
            // snapped `pos` would jump away from the interpolated visual.
            guard !posAnimating,
                  PageTurnMath.dragDirection(dx: dx, dy: dy) != nil else { return }
            dragBase = pos
            dragTransBase = v.translation.width
        }
        guard let base = dragBase else { return }
        // The drag range is the whole work — swipes cross chapters like
        // taps do (iOS divergence from web/Android, which clamp swipes
        // to the chapter). Only the book's own edges rubber-band.
        let raw = base + PageTurnMath.dragDelta(
            dx: dx - dragTransBase, viewWidth: size.width)
        pos = PageTurnMath.rangeClamp(raw, lo: 0,
                                      hi: max(work.spreads.count - 1, 0))
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
        // settle `pos` on it before deciding commit vs cancel. Both are
        // measured from `dragTransBase` (the translation at lock time).
        // The commit range is the whole work — swipes cross chapters
        // (iOS divergence from web commitSwipeTurn's chapter clamp).
        let relDx = v.translation.width - dragTransBase
        pos = PageTurnMath.rangeClamp(
            base + PageTurnMath.dragDelta(dx: relDx,
                                          viewWidth: size.width),
            lo: 0, hi: n - 1)
        let commit = !pinchActive ? PageTurnMath.commitDirection(
            translation: relDx,
            predicted: v.predictedEndTranslation.width - dragTransBase,
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
            applyLanding(landing, in: work)
        }
        animatePos(to: target)
    }

    /// Publish the current snapshot to the external-display projection.
    /// The store activates the session only while a screen is attached.
    private func publishExternal() {
        guard let work else { return }
        ext.updatePresentation(.init(
            work: work, currentIdx: currentIdx, pageAR: pageAR, lastAR: lastAR,
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
        posAnimating = false
        chaseTask?.cancel()
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
            posAnimating = false
            chaseTask?.cancel()
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

/// How far (in spreads) a mounted pager cell stays "live" — closer cells
/// render real pages, farther ones a placeholder. `pagerSpreadIndices`
/// uses it to size the mounted window while a chase is in flight.
private let pagerLiveDistance: CGFloat = 3

/// What a pager spread renders: nothing (far placeholder), the
/// reduced-width fast rendition (in-flight turns), or full quality
/// (settled reading, incl. zoom).
private enum PagerCellTier: Equatable {
    case placeholder, fast, full
}

/// One pager layer — an `Animatable` view (not a modifier) so `body`
/// re-evaluates every interpolated frame and can pick live vs. cheap
/// content by distance to `pos`.
///
/// `pos` is the animatable data: SwiftUI interpolates it per frame and
/// re-runs `body`, which is what produces the slide (a plain
/// `.offset(x: f(pos))` cannot animate — the departing spread's offset
/// is 0 at both integer endpoints, leaving nothing to interpolate).
///
/// `floor(pos)` is the top layer: pos rising slides it right off-screen
/// (forward — next spread revealed beneath), pos falling slides it in
/// from the right over the current spread (backward). `ceil(pos)` waits
/// static beneath at offset 0 in both cases.
private struct PagerLayer<Content: View>: View {
    var pos: CGFloat
    var index: Int
    var width: CGFloat
    var count: Int
    /// |index - pos| beyond this renders the placeholder. The visible
    /// pair (floor/ceil) is always < 1 away, so ~3 spreads of lead keep
    /// image warm-up without mounting real cells for a whole burst
    /// corridor (which can span a dozen spreads on rapid taps). At the
    /// 12 spreads/s base chase rate that's ~250-375ms of fetch head
    /// start — enough for a server's first ?w= rendition to generate
    /// before the spread slides into view.
    var liveDistance: CGFloat = pagerLiveDistance
    /// Settled pager → the visible neighbourhood upgrades to full-res
    /// images; while a turn is in flight everything stays fast-tier so
    /// decode/render cost never scales with flip speed.
    var fullRes: Bool = false
    var content: (PagerCellTier) -> Content

    var animatableData: CGFloat {
        get { pos }
        set { pos = newValue }
    }

    var body: some View {
        content(tier)
            .overlay { Color.black.opacity(Double(scrim)) }
            .opacity(opacity)
            .offset(x: offset)
            .zIndex(z)
    }

    /// Depth scrim on the static (`ceil`) spread — the covered page
    /// dims by `coveredFraction` and brightens as the mover uncovers
    /// it (iBooks / Android `stackTurnScrim` parity). Only the ceil
    /// layer is ever the covered one; the floor mover stays lit, and
    /// at rest or edge overshoot the fraction is 0 so nothing dims.
    private var scrim: CGFloat {
        guard index == Int(ceil(pos)) else { return 0 }
        return PageTurnMath.coveredFraction(pos, count: count)
            * PageTurnMath.stackScrimMax
    }

    private var tier: PagerCellTier {
        let dist = abs(pos - CGFloat(index))
        guard dist <= liveDistance else { return .placeholder }
        // At rest pos is integral, so dist <= 1 covers the visible pair
        // plus both warm-up neighbours — all upgrade together on settle.
        return fullRes && dist <= 1 ? .full : .fast
    }

    /// Only the two spreads adjacent to `pos` are opaque — hidden
    /// neighbours must never peek out from behind a narrower spread.
    /// Edge overshoot (pos < 0 or > count-1) shows only the edge spread.
    private var opacity: Double {
        if pos < 0 { return index == 0 ? 1 : 0 }
        if pos > CGFloat(count - 1) { return index == count - 1 ? 1 : 0 }
        return index == Int(floor(pos)) || index == Int(ceil(pos)) ? 1 : 0
    }

    /// Spreads at or behind `floor(pos)` keep sliding right by
    /// `(pos - index) * width` — spreads ahead of it (the revealed
    /// `ceil` layer) stay at 0. Departed spreads trail off to +width and
    /// farther, off-screen, which is what gives `.offset` real endpoint
    /// deltas for the animation to interpolate.
    private var offset: CGFloat {
        if pos < 0 { return index == 0 ? pos * width : 0 }
        if pos > CGFloat(count - 1) {
            return index == count - 1 ? (pos - CGFloat(count - 1)) * width : 0
        }
        return index <= Int(floor(pos)) ? (pos - CGFloat(index)) * width : 0
    }

    private var z: Double {
        if pos < 0 { return index == 0 ? 1 : 0 }
        if pos > CGFloat(count - 1) { return index == count - 1 ? 1 : 0 }
        return index == Int(floor(pos)) ? 1 : 0
    }
}

extension PagerLayer: @preconcurrency Animatable {}

extension Array {
    subscript(safe index: Int) -> Element? {
        indices.contains(index) ? self[index] : nil
    }
}
