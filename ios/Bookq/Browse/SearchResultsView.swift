import SwiftUI

/// Global search results — `renderSearchTiles/List/Cards` equivalent.
/// Results render through the same FolderTileView cells as the browse grid
/// (cover via tree node lookup, metadata badges, progress, offline +
/// visibility buttons as sibling controls).
struct SearchResultsView: View {
    @ObservedObject var search: SearchModel
    @ObservedObject var library: LibraryStore
    var onOpen: (String) -> Void

    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var progress: ReadingProgressStore
    @EnvironmentObject private var offline: OfflineStore
    @EnvironmentObject private var visibility: VisibilityStore
    @Environment(\.horizontalSizeClass) private var hSize
    @State private var saveError: String?

    var body: some View {
        // `_visibleResults` — raw results filtered by the visibility
        // predicate on every render (late responses / mode switches stay
        // consistent).
        let visible = search.results.filter { visibility.isVisible($0.folder_path) }
        GeometryReader { geo in
            ScrollView {
                Text("\(visible.count) 件")
                    .font(.caption2).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .trailing)
                    .padding(.horizontal, 12).padding(.top, 4)
                // Manual row-chunked grid with an explicit cell width —
                // LazyVGrid/flexible HStack children under .searchable
                // receive shifting proposals and end up with stale/unequal
                // frames. GeometryReader outside the ScrollView gives a
                // bounded width, so the cell width is deterministic.
                let cols = model.config.layoutMode == .tile
                    ? (hSize == .regular ? 5 : 3) : 1
                let w = (geo.size.width - 16 - 8 * CGFloat(cols - 1))
                    / CGFloat(cols)
                LazyVStack(spacing: 8) {
                    ForEach(Array(stride(from: 0, to: visible.count, by: cols)),
                            id: \.self) { start in
                        HStack(alignment: .top, spacing: 8) {
                            ForEach(visible[start..<min(start + cols, visible.count)],
                                    id: \.folder_path) { r in
                                cell(r).frame(width: w, alignment: .top)
                            }
                            ForEach(0..<max(0, start + cols - visible.count),
                                    id: \.self) { _ in
                                Color.clear.frame(width: w, height: 0)
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                .id(visible.map(\.folder_path))
                .padding(8)
                if visible.isEmpty {
                    Text("一致する結果がありません")
                        .font(.caption).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity).padding(.top, 40)
                }
            }
        }
        .overlay(alignment: .bottomLeading) {
            OnlineStatusBadge()
                .padding(.leading, 12).padding(.bottom, 20)
        }
        .overlay(alignment: .bottom) {
            if let saveError {
                Text(saveError)
                    .font(.caption)
                    .padding(.horizontal, 14).padding(.vertical, 7)
                    .background(.ultraThinMaterial, in: Capsule())
                    .padding(.bottom, 20)
                    .onAppear {
                        Task {
                            try? await Task.sleep(nanoseconds: 2_000_000_000)
                            self.saveError = nil
                        }
                    }
            }
        }
    }

    /// One result cell — tile + sibling visibility/offline controls.
    @ViewBuilder
    private func cell(_ r: ComicMetadata) -> some View {
        let path = r.folder_path
        let node = library.node(at: path) ?? placeholder(r)
        let leaves = library.leafPaths(under: path)
        let pres = visibility.presentation(
            path, editable: visibility.isTopLevel(path))
        let rowMode = model.config.layoutMode != .tile
        ZStack(alignment: .topTrailing) {
            ZStack(alignment: rowMode ? .topTrailing : .bottomTrailing) {
                Button { open(path) } label: {
                    FolderTileView(
                        path: path, node: node,
                        layout: model.config.layoutMode,
                        progress: progress.progress(for: path, leaves: leaves),
                        leaves: leaves, meta: r, vis: pres)
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("tile:\(path)")
                if pres?.toggle == true {
                    VisibilityEyeButton(
                        path: path,
                        hidden: pres?.hidden == true,
                        onError: { saveError = $0 })
                        .padding(EdgeInsets(top: 5, leading: 5,
                                            bottom: 5,
                                            trailing: rowMode ? 44 : 5))
                }
            }
            if !leaves.isEmpty {
                OfflineButton(path: path, leaves: leaves, store: offline)
                    .padding(5)
            }
        }
    }

    private func open(_ path: String) {
        search.clear()          // web clears the input on result-tap
        onOpen(path)
    }

    /// Result folder not present in the tree (index built vs. tree drift) —
    /// render a name-only placeholder tile.
    private func placeholder(_ r: ComicMetadata) -> TreeNode {
        TreeNode(name: r.folder_name, type: "folder")
    }
}
