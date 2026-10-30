import SwiftUI

/// One browse level: grid/list/card of folder tiles + "read self" tiles for
/// mixed children. Mirrors `render.js` tile ordering (folders, then images).
struct FolderLevelView: View {
    let path: String
    @ObservedObject var library: LibraryStore
    var onOpen: (String) -> Void

    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var progress: ReadingProgressStore
    @Environment(\.horizontalSizeClass) private var hSize
    @State private var query = ""

    var body: some View {
        content(library: library)
            .searchable(text: $query, prompt: "この階層を検索")
    }

    @ViewBuilder
    private func content(library: LibraryStore) -> some View {
        let folders = BrowseModel.filter(library.levelFolders(path: path), query: query)
        let selfTiles = query.isEmpty ? library.selfTiles(path: path) : []

        ScrollView {
            // #folder-count equivalent
            Text("\(folders.count + selfTiles.count) 件")
                .font(.caption2).foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .trailing)
                .padding(.horizontal, 12).padding(.top, 4)
            LazyVGrid(columns: columns, spacing: 8, pinnedViews: []) {
                ForEach(folders, id: \.path) { entry in
                    Button { onFolderTap(entry) } label: {
                        FolderTileView(
                            path: entry.path,
                            node: entry.node,
                            layout: model.config.layoutMode,
                            progress: progress.progress(
                                for: entry.path,
                                leaves: library.leafPaths(under: entry.path))
                        )
                    }
                    .buttonStyle(.plain)
                    .id(entry.path)
                }
                ForEach(selfTiles, id: \.path) { entry in
                    Button { onOpen(entry.path) } label: {
                        SelfTileView(node: entry.node, path: entry.path,
                                     layout: model.config.layoutMode)
                    }
                    .buttonStyle(.plain)
                    .id("self-\(entry.path)")
                }
            }
            .scrollTargetLayout()
            .padding(8)
        }
    }

    private var columns: [GridItem] {
        switch model.config.layoutMode {
        case .tile:
            // adaptive grid: ~3 columns iPhone, more on iPad
            let min: CGFloat = hSize == .regular ? 150 : 110
            return [GridItem(.adaptive(minimum: min), spacing: 8)]
        case .list, .card:
            return [GridItem(.flexible(), spacing: 8)]
        }
    }

    private func onFolderTap(_ entry: (path: String, node: TreeNode)) {
        if BrowseModel.drillsDown(entry.node) {
            model.pushLevel(entry.path)
        } else {
            onOpen(entry.path)
        }
    }
}

/// "このフォルダを読む" tile for mixed folders / root loose images.
struct SelfTileView: View {
    let node: TreeNode
    let path: String
    var layout: AppConfig.LayoutMode = .tile
    @EnvironmentObject private var model: AppModel

    var body: some View {
        if layout == .tile {
            VStack(alignment: .leading, spacing: 4) {
                cover
                Text("このフォルダを読む")
                    .font(.caption).lineLimit(2)
            }
        } else {
            HStack(spacing: 10) {
                cover.frame(width: (layout == .card ? 72 : 44) * 2 / 3,
                            height: layout == .card ? 72 : 44)
                Text("このフォルダを読む").font(.subheadline)
                Spacer()
                Image(systemName: "book").foregroundStyle(.tertiary)
            }
            .contentShape(Rectangle())
        }
    }

    private var cover: some View {
        RemoteImageView(url: coverURL, contentMode: .fill)
            .frame(maxWidth: .infinity)
            .aspectRatio(layout == .tile ? 2.0 / 3.0 : 1, contentMode: .fill)
            .clipped()
            .clipShape(RoundedRectangle(cornerRadius: 6))
            .overlay(alignment: .topTrailing) {
                Image(systemName: "book.fill")
                    .font(.caption2)
                    .padding(4)
                    .background(.black.opacity(0.5), in: Circle())
                    .padding(4)
            }
    }

    private var coverURL: URL? {
        guard let api = model.api,
              let thumb = node.thumb, !thumb.isEmpty else { return nil }
        return api.thumbURL(folder: path, name: thumb, w: 400)
    }
}
