import SwiftUI
import UIKit

/// iOS 27+ external-display plumbing. Applied to ReaderView: declares an
/// `ExternalNonInteractiveAccessory` the system presents on an attached
/// display (kicking out of mirroring); `isEnabled` mirrors `userEnabled`,
/// so toggling it returns the display to mirroring. `onAvailabilityChange`
/// reports a display attach — the only reliable attach signal (UIScreen
/// notifications are deprecated and only cover scene-backed screens).
struct ExternalAccessoryModifier: ViewModifier {
    @ObservedObject var ext: ExternalDisplayStore

    @ViewBuilder
    func body(content: Content) -> some View {
        if #available(iOS 27.0, *) {
            content.sceneAccessory {
                ExternalNonInteractiveAccessory(
                    isEnabled: Binding(
                        get: { ext.userEnabled },
                        set: { ext.userEnabled = $0 })
                ) {
                    ExternalDisplayRootView()
                }
                .onAvailabilityChange { available in
                    Task { @MainActor in ext.accessoryAvailabilityChanged(available) }
                }
            }
        } else {
            content
        }
    }
}

/// Root view of the accessory content on the external screen. Renders the
/// published reader snapshot fullscreen; black standby while nothing is
/// presented (transient — the system mirrors when disabled/detached).
struct ExternalDisplayRootView: View {
    @ObservedObject private var store = ExternalDisplayStore.shared

    var body: some View {
        ZStack {
            Color.black
            if let p = store.presentation {
                ExternalReaderView(p: p)
            }
            // Zero-size probe: the accessory scene has no delegate, so the
            // screen object is obtained through the view hierarchy itself —
            // it is also what lets us force the native-4K mode.
            ScreenProbe(store: store).frame(width: 0, height: 0)
        }
        .ignoresSafeArea()
        .preferredColorScheme(.dark)
        .onDisappear { store.screenLost() }
        // Diagnostics overlay (temporary, for device bring-up): shows what
        // mode/bounds the panel actually reports. Inset enough to survive
        // panel-level overscan clipping.
        .overlay(alignment: .bottomLeading) {
            if let screen = store.externalScreen {
                Text(ExternalDisplayStore.describe(screen))
                    .font(.system(size: 18, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.6))
                    .padding(40)
            }
        }
    }
}

/// Grabs the accessory scene's UIScreen from the window hierarchy and hands
/// it to the store (mode + overscan are applied there). Zero-size, invisible.
private struct ScreenProbe: UIViewRepresentable {
    let store: ExternalDisplayStore

    func makeUIView(context: Context) -> ProbeView {
        let v = ProbeView()
        v.onScreen = { [store] screen in store.screenFound(screen) }
        return v
    }

    func updateUIView(_ uiView: ProbeView, context: Context) {}

    final class ProbeView: UIView {
        var onScreen: ((UIScreen) -> Void)?
        override func didMoveToWindow() {
            super.didMoveToWindow()
            guard let screen = window?.windowScene?.screen else { return }
            onScreen?(screen)
        }
    }
}

/// Fullscreen mirror of the current reader page/spread on the external
/// screen. Same layout math as the device reader's spreadCell: pages share
/// one height, width = ar*h, fit into the viewport, RTL order. Landscape
/// single-page mode also mirrors the device's side previews (#496) —
/// settled neighbours only, since the projection has no pager position.
struct ExternalReaderView: View {
    let p: ExternalDisplayStore.Presentation

    var body: some View {
        GeometryReader { geo in
            let ext = externalWork(in: geo.size)
            let s = ext.spreadOf(p.currentIdx)
            ZStack {
                if ext.pageMode == .single, geo.size.width >= geo.size.height {
                    neighborCells(ext: ext, around: s, in: geo.size)
                }
                spreadCell(ext: ext, pages: ext.spreads[safe: s] ?? [],
                           in: geo.size)
            }
        }
        .ignoresSafeArea()
    }

    /// Prev/next page previews in the side margins — same
    /// `ReaderWork.neighborFrames` math as the device reader, at the
    /// settled spread's immediate neighbours (no mid-turn states on a
    /// non-interactive display).
    private func neighborCells(ext: ReaderWork, around s: Int,
                               in size: CGSize) -> some View {
        let n = ext.spreads.count
        guard n > 0 else { return AnyView(EmptyView()) }
        let curAR = ext.spreads[safe: s]?.first
            .map { p.pageAR[$0] ?? p.lastAR } ?? p.lastAR
        func ar(_ spread: Int?) -> CGFloat? {
            guard let spread, let g = ext.spreads[safe: spread]?.first
            else { return nil }
            return p.pageAR[g] ?? p.lastAR
        }
        let frames = ReaderWork.neighborFrames(
            curAR: curAR,
            prevAR: s > 0 ? ar(s - 1) : nil,
            nextAR: s + 1 < n ? ar(s + 1) : nil,
            in: size)
        @ViewBuilder
        func cell(_ spread: Int, _ frame: CGRect?) -> some View {
            if spread >= 0 && spread < n, let frame,
               let g = ext.spreads[safe: spread]?.first {
                RemoteImageView(url: p.urlFor(ext.images[g]),
                                contentMode: .fit)
                    .frame(width: frame.width, height: frame.height)
                    // Before .position — after it the background would
                    // fill the whole proposed (viewport-sized) area.
                    .background(Color.black)
                    .position(x: frame.midX, y: frame.midY)
            }
        }
        return AnyView(ZStack {
            cell(s - 1, frames.prev)
            cell(s + 1, frames.next)
        }
        // Fill the viewport so `.position` coordinates (viewport space)
        // line up — same fix as the device reader's neighborLayer.
        .frame(width: size.width, height: size.height)
        .allowsHitTesting(false))
    }

    /// Spreads re-grouped under the external viewport's orientation with the
    /// device reader's rule (landscape && landscapeSpread → spread).
    private func externalWork(in size: CGSize) -> ReaderWork {
        p.work.withMode(ReaderWork.pageMode(for: size, landscapeSpread: p.landscapeSpread),
                        offset: p.spreadOffset)
    }

    private func spreadCell(ext: ReaderWork, pages: [Int], in size: CGSize) -> some View {
        let gap: CGFloat = 5
        let ars = pages.map { p.pageAR[$0] ?? p.lastAR }
        let fit = ReaderWork.spreadFit(ars: ars, gap: gap, in: size)
        return HStack(spacing: gap) {
            ForEach(Array(pages.enumerated()), id: \.offset) { pos, idx in
                RemoteImageView(url: p.urlFor(ext.images[idx]), contentMode: .fit)
                    .frame(width: ars[pos] * fit.height, height: fit.height)
                    .background(Color.black)
            }
        }
        .frame(width: fit.width, height: fit.height)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .environment(\.layoutDirection, .rightToLeft)
    }
}
