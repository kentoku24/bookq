import SwiftUI

/// Multi-page overview grid — `viewer/overview.js` equivalent.
/// All real pages as 3:4 thumb cells with chapter-local numbers, chapter
/// sections + headings, current cell highlighted, tap → jump to that page.
struct OverviewView: View {
    let work: ReaderWork
    let api: BookqAPI
    @Binding var currentIdx: Int
    var onDismiss: () -> Void

    @Environment(\.dismiss) private var dismiss

    /// Non-spacer global indices, in order (realList()).
    private var reals: [Int] {
        work.images.indices.filter { !work.images[$0].isSpacer }
    }

    struct OvSection { var title: String?; var pages: [Int] }

    /// Section model — headings only in multi-chapter works, like build().
    private var sections: [OvSection] {
        guard work.isCrossChapter else { return [OvSection(title: nil, pages: reals)] }
        var out: [OvSection] = []
        var lastCh = -1
        for g in reals {
            let info = work.chapterInfo(at: g)
            if info.chapterIdx != lastCh {
                out.append(OvSection(title: info.name, pages: []))
                lastCh = info.chapterIdx
            }
            out[out.count - 1].pages.append(g)
        }
        return out
    }

    /// Anchor on nearest real page when currentIdx sits on a spacer.
    private var anchorIdx: Int {
        guard work.images.indices.contains(currentIdx),
              !work.images[currentIdx].isSpacer else {
            return reals.first { $0 > currentIdx } ?? reals.last ?? currentIdx
        }
        return currentIdx
    }

    var body: some View {
        NavigationStack {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 18) {
                        ForEach(Array(sections.enumerated()), id: \.offset) { _, sec in
                            if let title = sec.title {
                                Text(title)
                                    .font(.subheadline).foregroundStyle(.secondary)
                                    .padding(.horizontal, 12)
                                    .padding(.top, 6)
                            }
                            LazyVGrid(
                                columns: [GridItem(.adaptive(minimum: 110), spacing: 8)],
                                spacing: 8
                            ) {
                                ForEach(sec.pages, id: \.self) { g in
                                    cell(g)
                                        .id(g)
                                }
                            }
                            .padding(.horizontal, 12)
                        }
                    }
                    .padding(.vertical, 10)
                }
                .onAppear {
                    // scrollCurToThird — anchor current cell ~1/3 from top
                    proxy.scrollTo(anchorIdx, anchor: .init(x: 0.5, y: 0.33))
                }
            }
            .navigationTitle(work.isCrossChapter
                             ? BrowseModel.splitDisplayName(work.clickedPath).leaf
                             : work.clickedPath.split(separator: "/").last.map(String.init) ?? "")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("閉じる") { onDismiss() }
                }
            }
        }
        .preferredColorScheme(.dark)
    }

    private func cell(_ g: Int) -> some View {
        let page = work.images[g]
        let info = work.chapterInfo(at: g)
        let isCur = g == anchorIdx
        return Button {
            currentIdx = g
            onDismiss()
        } label: {
            ZStack(alignment: .bottomTrailing) {
                RemoteImageView(
                    url: api.thumbURL(folder: page.folder, name: page.name, w: 400),
                    contentMode: .fit
                )
                .aspectRatio(3.0 / 4.0, contentMode: .fit)
                .frame(maxWidth: .infinity)
                .background(Color(white: 0.14))
                .clipShape(RoundedRectangle(cornerRadius: 4))
                Text("\(info.localIdx + 1)")
                    .font(.caption2).monospacedDigit()
                    .foregroundStyle(.white)
                    .padding(.horizontal, 5).padding(.vertical, 2)
                    .background(.black.opacity(0.6), in: Capsule())
                    .padding(4)
            }
            .overlay(
                RoundedRectangle(cornerRadius: 4)
                    .stroke(isCur ? Color.accentColor : .clear, lineWidth: 3)
            )
        }
        .buttonStyle(.plain)
    }
}
