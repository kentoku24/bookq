import SwiftUI

/// Paired-reader surface — the dual-reader equivalent of ReaderView.
/// Each device renders ONLY its own side of the committed spread
/// (pairedView.apply): page turns are wire intents, not local moves.
struct PairedReaderView: View {
    @EnvironmentObject private var pair: PairStore
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase

    @State private var showChrome = true
    @State private var zoomed = false

    var body: some View {
        GeometryReader { geo in
            ZStack {
                Color.black.ignoresSafeArea()
                content(in: geo.size)
            }
            .statusBarHidden(!showChrome)
            .overlay(alignment: .top) { if showChrome { chrome } }
        }
        .onChange(of: scenePhase) { _, ph in pair.scenePhase(ph) }
        .onDisappear {
            // Leader closing the paired view = browsing the work list —
            // SET_ACTIVITY selecting (web sidebar-open equivalent). The
            // follower's close is a local look only; the pair stays.
            // Only a close while `reading` is user intent — phase-driven
            // dismissals (transient loading on SET_OFFSET/OPEN_WORK, paused,
            // ended) must not flip the session to selecting (#216).
            if pair.role == .leader && pair.bound &&
                PairReaderGate.closeSignalsSelecting(pair.phase) {
                pair.setActivity("selecting")
            }
        }
    }

    @ViewBuilder
    private func content(in size: CGSize) -> some View {
        switch pair.phase {
        case .reading, .paused, .loading:
            if let p = pair.projection {
                page(p, in: size)
            } else {
                ProgressView("同期中…").tint(.white)
            }
            if pair.phase == .paused {
                pauseOverlay
            }
        case .selecting:
            selectingOverlay
        case .waiting:
            ProgressView("相手の参加を待っています…").tint(.white)
        default:
            ProgressView("同期中…").tint(.white)
        }
    }

    // MARK: - Page

    /// One side of the committed spread. `ref == nil` renders the blank
    /// partner side — web shows an empty half, never back-fills.
    private func page(_ p: PairProjection, in size: CGSize) -> some View {
        ZStack {
            if let ref = p.ref {
                let url = model.api?.pageURL(folder: ref.folder, name: ref.name,
                                             original: model.config.originalImages)
                ZoomableScrollView(onZoomChange: { z in zoomed = z }) {
                    RemoteImageView(url: url, contentMode: .fit)
                        .background(Color.black)
                }
                .id("\(ref.folder)|\(ref.name)")   // fresh zoom state per page
            } else {
                Color(white: 0.12)
                    .overlay { Text("空白").foregroundStyle(.secondary) }
            }
        }
        .gesture(SpatialTapGesture().onEnded { ev in
            let frac = ev.location.x / max(size.width, 1)
            if frac < 0.30 { turn(1) }
            else if frac > 0.70 { turn(-1) }
            else { withAnimation { showChrome.toggle() } }
        })
        // Web parity: edge taps cross chapters; keys/swipes clamp in-chapter.
        // iOS divergence (solo reader parity): keys/swipes cross chapters
        // too — every page-turn op may cross, jumps never do.
        .gesture(DragGesture(minimumDistance: 40).onEnded { ev in
            guard !zoomed else { return }
            if ev.translation.width > 0 { turn(-1) }
            else { turn(1) }
        })
        .focusable().focusEffectDisabled()
        .onKeyPress(.rightArrow) { turn(1); return .handled }
        .onKeyPress(.leftArrow) { turn(-1); return .handled }
    }

    private func turn(_ delta: Int, cross: Bool = true) {
        pair.turn(delta: delta, crossChapter: cross,
                  fromChapter: pair.projection?.chapterFolder)
    }

    // MARK: - Overlays

    private var pauseOverlay: some View {
        VStack {
            Spacer()
            Text("相手が離席中・再接続待ち")
                .font(.subheadline)
                .padding(.horizontal, 14).padding(.vertical, 8)
                .background(.ultraThinMaterial, in: Capsule())
                .padding(.bottom, 80)
        }
    }

    private var selectingOverlay: some View {
        VStack(spacing: 14) {
            Image(systemName: "books.vertical")
                .font(.largeTitle).foregroundStyle(.white.opacity(0.7))
            Text(pair.role == .leader
                 ? "ブラウズから共有する作品を選んでください"
                 : "リーダーが作品を選ぶのを待っています")
                .foregroundStyle(.white).font(.subheadline)
            Button("閉じる") { dismiss() }
                .buttonStyle(.borderedProminent)
        }
        .padding()
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
                if let p = pair.projection {
                    VStack(spacing: 2) {
                        Text("\(p.spreadIndex + 1) / \(p.spreadTotal)")
                            .font(.subheadline).monospacedDigit()
                        Text(pair.role == .leader ? "リーダー" : "フォロワー")
                            .font(.caption2).foregroundStyle(.white.opacity(0.8))
                    }
                    .foregroundStyle(.white)
                    .padding(6)
                    .background(.black.opacity(0.5), in: Capsule())
                }
                Spacer()
                modeButtons
            }
            .padding(10)
            Spacer()
            if let m = pair.manifest, let p = pair.projection {
                sliderRow(m, p)
            }
        }
        .transition(.opacity)
    }

    private var modeButtons: some View {
        HStack(spacing: 8) {
            Button { pair.swapSides() } label: {
                Image(systemName: "arrow.left.arrow.right")
                    .padding(10)
                    .background(.black.opacity(0.5), in: Circle())
            }
            .accessibilityLabel("左右入れ替え")
            Button {
                pair.setOffset((pair.lastState?.offset ?? 0) == 0 ? 1 : 0)
            } label: {
                Image(systemName: (pair.lastState?.offset ?? 0) == 1 ? "book" : "book.pages")
                    .padding(10)
                    .background((pair.lastState?.offset ?? 0) == 1
                                ? Color.accentColor : .black.opacity(0.5), in: Circle())
            }
            .accessibilityLabel("spread offset")
        }
        .foregroundStyle(.white)
        .disabled(pair.phase != .reading)
    }

    /// Chapter-local slider → GOTO the target spread's anchor (right)
    /// PageRef. The range spans only the CURRENT chapter's contiguous
    /// `chapterFolder` run (web `syncChapterSlider` parity) — episode
    /// changes come from page turns at the edges, never the slider.
    private func sliderRow(_ m: PairManifest, _ p: PairProjection) -> some View {
        let r = PairModel.chapterRange(in: m.spreadTable,
                                       index: p.spreadIndex)
        let lo = r?.lo ?? 0
        let hi = r?.hi ?? max(m.spreadTable.count - 1, 0)
        let span = max(hi - lo, 0)
        return Slider(
            value: Binding(
                get: {
                    Double(min(max(p.spreadIndex - lo, 0), span))
                },
                set: { v in
                    let s = min(max(lo + Int(v.rounded()), lo), hi)
                    guard m.spreadTable.indices.contains(s) else { return }
                    pair.goto(page: m.spreadTable[s].right)
                }),
            in: 0...Double(max(span, 1)),
            step: 1
        )
        .disabled(span <= 0)
        .environment(\.layoutDirection, .rightToLeft)
        .padding(.horizontal, 20)
        .padding(.bottom, 30)
        .tint(.white)
    }
}
