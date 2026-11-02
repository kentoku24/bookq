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
        ScrollView {
            // #folder-count equivalent
            Text("\(folders.count + selfTiles.count) 件")
                .font(.caption2).foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .trailing)
                .padding(.horizontal, 12).padding(.top, 4)
            LazyVGrid(columns: columns, spacing: 8, pinnedViews: []) {
                ForEach(folders, id: \.path) { entry in
                    let leaves = library.leafPaths(under: entry.path)
                    let pres = visibility.presentation(
                        entry.path, editable: visibility.isTopLevel(entry.path))
                    let rowMode = model.config.layoutMode != .tile
                    ZStack(alignment: .topTrailing) {
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
                ForEach(selfTiles, id: \.path) { entry in
                    ZStack(alignment: .topTrailing) {
                        Button { onFolderTap(entry) } label: {
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
            }
            .scrollTargetLayout()
            // Recreate on membership/order changes — LazyVGrid can leave
            // stale per-item frames when an existing id shifts cells.
            .id(folders.map(\.path) + selfTiles.map(\.path))
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
