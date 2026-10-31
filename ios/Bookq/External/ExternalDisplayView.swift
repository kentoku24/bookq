import SwiftUI
import UIKit

/// Root view of the external-display window. Non-interactive role — no
/// gestures, no chrome. Black standby while no reader is presented
/// (transient: the session only activates while a presentation exists).
struct ExternalDisplayRootView: View {
    @ObservedObject private var store = ExternalDisplayStore.shared

    /// "3840×2160@1.0  bounds 3840×2160  insets 0,0,0,0" — one glance tells
    /// whether the panel got native 4K or fell back to a scaled mode.
    private static func describe(_ screen: UIScreen) -> String {
        let m = screen.currentMode
        let mode = m.map { "\(Int($0.size.width))×\(Int($0.size.height))@\($0.pixelAspectRatio)" } ?? "?"
        let b = screen.bounds
        let i = screen.overscanCompensationInsets
        let fmt = { (v: CGFloat) in String(format: "%.0f", v) }
        return "\(mode)  bounds \(fmt(b.width))×\(fmt(b.height))  " +
               "insets \(fmt(i.top)),\(fmt(i.left)),\(fmt(i.bottom)),\(fmt(i.right))"
    }

    var body: some View {
        ZStack {
            Color.black
            if let p = store.presentation {
                ExternalReaderView(p: p)
            }
        }
        .ignoresSafeArea()
        .preferredColorScheme(.dark)
        // Diagnostics overlay (temporary, for device bring-up): shows what
        // mode/bounds the panel actually reports — a 1:1-scaled monitor
        // fed 1080p and an insetBounds shrink look identical from afar.
        .overlay(alignment: .bottomLeading) {
            if let screen = store.externalScreen {
                Text(Self.describe(screen))
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.4))
                    .padding(6)
            }
        }
    }
}

/// Fullscreen mirror of the current reader page/spread on the external
/// screen. Same layout math as the device reader's spreadCell: pages share
/// one height, width = ar*h, fit into the viewport, RTL order.
struct ExternalReaderView: View {
    let p: ExternalDisplayStore.Presentation

    var body: some View {
        GeometryReader { geo in
            let ext = externalWork(in: geo.size)
            let pages = ext.spreads[safe: ext.spreadOf(p.currentIdx)] ?? []
            spreadCell(ext: ext, pages: pages, in: geo.size)
        }
        .ignoresSafeArea()
    }

    /// Spreads re-grouped under the external viewport's orientation with the
    /// device reader's rule (landscape && landscapeSpread → spread).
    private func externalWork(in size: CGSize) -> ReaderWork {
        p.work.withMode(ReaderWork.pageMode(for: size, landscapeSpread: p.landscapeSpread),
                        offset: p.spreadOffset)
    }

    private func spreadCell(ext: ReaderWork, pages: [Int], in size: CGSize) -> some View {
        let gap: CGFloat = 5
        let ars = pages.map { p.pageAR[$0] ?? 0.7 }
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
