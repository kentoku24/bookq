import SwiftUI

/// One browse level: grid/list/card of folder tiles + "read self" tiles for
/// mixed children. Mirrors `render.js` tile ordering (folders, then images).
struct FolderLevelView: View {
    let path: String
    @ObservedObject var library: LibraryStore
    var onOpen: (String) -> Void
    /// Root level passes true — the global metadata search field lives in
    /// LibraryView (`.searchable` applied there); deeper levels keep their
    /// own current-level filter field.
    var searchExternal: Bool = false

    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var progress: ReadingProgressStore
    @EnvironmentObject private var offline: OfflineStore
    @EnvironmentObject private var visibility: VisibilityStore
    @Environment(\.horizontalSizeClass) private var hSize
    @State private var query = ""
    @State private var saveError: String?

    var body: some View {
        if searchExternal {
            content(library: library)
        } else {
            content(library: library)
                .searchable(text: $query, prompt: "この階層を検索")
        }
    }

    @ViewBuilder
    private func content(library: LibraryStore) -> some View {
        let folders = BrowseModel.filter(library.levelFolders(path: path), query: query)
        let selfTiles = query.isEmpty ? library.selfTiles(path: path) : []

        // `blockedGridHtml` — never paint an unfiltered list while the
        // visibility judgment is unknown (loading/error without snapshot).
        Group {
            switch visibility.blockedState {
            case .loading:
                VStack { Spacer(); ProgressView("読込中…"); Spacer() }
            case .error:
                VStack(spacing: 10) {
                    Spacer()
                    Text("取得失敗").foregroundStyle(.red).font(.caption)
                    Button("再試行") { visibility.retry() }
                    Spacer()
                }
            case .none:
                grid(folders: folders, selfTiles: selfTiles)
                    .overlay(alignment: .bottom) { statusRow }
            }
        }
        .overlay(alignment: .bottomLeading) {
            // #offline-status — persistent connectivity pill on every
            // browse level including loading/error (web sidebar-header
            // parity — it never leaves the header).
            OnlineStatusBadge()
                .padding(.leading, 12).padding(.bottom, 14)
        }
    }

    /// `visibility-status` row — showAll mode's save/readonly/error line.
    @ViewBuilder
    private var statusRow: some View {
        if let t = visibility.statusText {
            HStack(spacing: 8) {
                Text(t).font(.caption)
                if visibility.status == .error {
                    Button("再試行") { visibility.retry() }
                        .font(.caption)
                }
            }
            .padding(.horizontal, 12).padding(.vertical, 6)
            .background(.ultraThinMaterial, in: Capsule())
            .padding(.bottom, 14)
        } else if let saveError {
            Text(saveError).font(.caption)
                .padding(.horizontal, 12).padding(.vertical, 6)
                .background(.ultraThinMaterial, in: Capsule())
                .padding(.bottom, 14)
                .onAppear {
                    Task {
                        try? await Task.sleep(nanoseconds: 2_000_000_000)
                        self.saveError = nil
                    }
                }
        }
    }

    @ViewBuilder
    private func grid(folders: [(path: String, node: TreeNode)],
                      selfTiles: [(path: String, node: TreeNode)]) -> some View {
        // Row-chunked manual grid — same workaround as SearchResultsView:
        // LazyVGrid children under `.searchable` receive shifting proposals
        // and keep stale/unequal frames, so taps land on neighboring cells.
        // Explicit cell widths keep every frame deterministic.
        let items = folders.map { BrowseTile.folder(path: $0.path, node: $0.node) }
            + selfTiles.map { BrowseTile.selfTile(path: $0.path, node: $0.node) }
        GeometryReader { geo in
            ScrollView {
                // #folder-count equivalent
                Text("\(items.count) 件")
                    .font(.caption2).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .trailing)
                    .padding(.horizontal, 12).padding(.top, 4)
                // -16 = the LazyVStack's horizontal .padding(8) on both sides.
                let cols = BrowseModel.gridColumns(
                    isTileLayout: model.config.layoutMode == .tile,
                    minimum: hSize == .regular ? 150 : 110,
                    availableWidth: geo.size.width - 16)
                let w = (geo.size.width - 16 - 8 * CGFloat(cols - 1)) / CGFloat(cols)
                LazyVStack(spacing: 8) {
                    ForEach(Array(stride(from: 0, to: items.count, by: cols)),
                            id: \.self) { start in
                        HStack(alignment: .top, spacing: 8) {
                            ForEach(items[start..<min(start + cols, items.count)]) { entry in
                                tileCell(entry)
                                    .frame(width: w, alignment: .top)
                            }
                            ForEach(0..<max(0, start + cols - items.count),
                                    id: \.self) { _ in
                                Color.clear.frame(width: w, height: 0)
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                .scrollTargetLayout()
                // Recreate on membership/order changes — the same guard
                // the LazyVGrid version carried.
                .id(items.map(\.id))
                .padding(8)
            }
        }
    }

    @ViewBuilder
    private func tileCell(_ entry: BrowseTile) -> some View {
        switch entry {
        case .folder(let path, let node):
            folderCell(path: path, node: node)
        case .selfTile(let path, let node):
            selfCell(path: path, node: node)
        }
    }

    private func folderCell(path: String, node: TreeNode) -> some View {
        let entry = (path: path, node: node)
        let leaves = library.leafPaths(under: entry.path)
        let pres = visibility.presentation(
            entry.path, editable: visibility.isTopLevel(entry.path))
        let rowMode = model.config.layoutMode != .tile
        return ZStack(alignment: .topTrailing) {
            // Row layouts are too short to stack the eye below
            // the download button — park it to the button's left.
            ZStack(alignment: rowMode ? .topTrailing : .bottomTrailing) {
                Button { onFolderTap(entry) } label: {
                    FolderTileView(
                        path: entry.path,
                        node: entry.node,
                        layout: model.config.layoutMode,
                        progress: progress.progress(for: entry.path, leaves: leaves),
                        leaves: leaves,
                        meta: entry.node.metadata,
                        vis: pres
                    )
                }
                .buttonStyle(.plain)
                if pres?.toggle == true {
                    VisibilityEyeButton(
                        path: entry.path,
                        hidden: pres?.hidden == true,
                        onError: { saveError = $0 })
                        .padding(EdgeInsets(top: 5, leading: 5,
                                            bottom: 5,
                                            trailing: rowMode ? 44 : 5))
                }
            }
            if !leaves.isEmpty {
                OfflineButton(path: entry.path, leaves: leaves, store: offline)
                    .padding(5)
            }
        }
        .id(entry.path)
    }

    private func selfCell(path: String, node: TreeNode) -> some View {
        let entry = (path: path, node: node)
        return ZStack(alignment: .topTrailing) {
            // Self tiles always open the reader at their own path (web
            // `data-read-self` → loadImages) — never drillsDown, or a
            // mixed folder would drill instead of reading its own images.
            Button { onOpen(entry.path) } label: {
                SelfTileView(node: entry.node, path: entry.path,
                             layout: model.config.layoutMode)
            }
            .buttonStyle(.plain)
            // the self-tile is itself the download target
            OfflineButton(path: entry.path, leaves: [entry.path],
                          store: offline)
                .padding(5)
        }
        .id("self-\(entry.path)")
    }

    /// `guardPath` gate — pending judgments queue behind a refresh rather
    /// than opening a path whose visibility is unknown; positive-hidden
    /// paths never open in normal mode.
    private func onFolderTap(_ entry: (path: String, node: TreeNode)) {
        switch visibility.guardPath(entry.path) {
        case .pending:
            Task {
                await visibility.refresh()
                guard visibility.guardPath(entry.path) != .hidden else { return }
                open(entry)
            }
        case .hidden:
            return
        case .allow:
            open(entry)
        }
    }

    private func open(_ entry: (path: String, node: TreeNode)) {
        if BrowseModel.drillsDown(entry.node) {
            model.pushLevel(entry.path)
        } else {
            onOpen(entry.path)
        }
    }
}

/// Grid cell identity — a mixed folder shows BOTH a folder tile and a
/// "read self" tile at the same path, so the two kinds are namespaced.
private enum BrowseTile: Identifiable {
    case folder(path: String, node: TreeNode)
    case selfTile(path: String, node: TreeNode)

    var id: String {
        switch self {
        case .folder(let p, _): return "folder|\(p)"
        case .selfTile(let p, _): return "self|\(p)"
        }
    }
}

/// Shared eye toggle (browse + search results) — visibility.js
/// `.visibility-toggle`: self match unhides by the stored key.
struct VisibilityEyeButton: View {
    let path: String
    let hidden: Bool
    var onError: (String) -> Void = { _ in }
    @EnvironmentObject private var visibility: VisibilityStore

    var body: some View {
        Button {
            Task {
                do {
                    let m = visibility.hiddenMatch(path)
                    let target = (m?.kind == .self_) ? m!.key : path
                    try await visibility.setHidden(target, hidden: !hidden)
                } catch {
                    onError("保存失敗")
                }
            }
        } label: {
            Image(systemName: hidden ? "eye.slash.fill" : "eye.fill")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(.white)
                .frame(width: 30, height: 30)
                .background(.black.opacity(0.55), in: Circle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(hidden ? "再表示する" : "非表示にする")
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
