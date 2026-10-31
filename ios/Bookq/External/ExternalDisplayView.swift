import SwiftUI
import UIKit

/// Root view of the external-display window. Non-interactive role — no
/// gestures, no chrome. Black standby while no reader is presented
/// (transient: the session only activates while a presentation exists).
struct ExternalDisplayRootView: View {
    @ObservedObject private var store = ExternalDisplayStore.shared

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            if let p = store.presentation {
                ExternalReaderView(p: p)
            }
        }
        .preferredColorScheme(.dark)
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
