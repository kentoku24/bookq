import SwiftUI

/// Root browse screen: NavigationStack of FolderLevelViews + reader modal.
struct LibraryView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var offline: OfflineStore
    @EnvironmentObject private var search: SearchModel
    @EnvironmentObject private var visibility: VisibilityStore
    @State private var showSettings = false
    @State private var showFacets = false

    var body: some View {
        NavigationStack(path: $model.navPath) {
            levelStack
                .navigationTitle("bookq")
                .toolbar { toolbar }
                .navigationDestination(for: BrowseRoute.self) { route in
                    switch route {
                    case .level(let path):
                        if let library = model.library {
                            FolderLevelView(path: path, library: library, onOpen: openWork)
                                .navigationTitle(title(for: path))
                                .navigationBarTitleDisplayMode(.inline)
                        }
                    case .reader:
                        EmptyView() // readers open as a fullScreenCover
                    }
                }
        }
        .fullScreenCover(item: $model.openReader) { route in
            ReaderView(path: route.path)
        }
        .sheet(isPresented: $showSettings) {
            SettingsView()
        }
        .sheet(isPresented: $showFacets) {
            FacetSheet(search: search)
        }
        .searchable(text: $search.query, prompt: "作品を検索")
        .onChange(of: visibility.showAll) { _, _ in
            // include_hidden affects both search and facet sets server-side
            Task {
                await search.loadFacets()
                if search.active { await search.search() }
            }
        }
        .task {
            await model.library?.load()
            model.library?.startPolling()
        }
        .overlay {
            // Error overlay only when there is nothing to show — a failed
            // background poll must not blank usable (stale) tree data, the
            // same way the web keeps browsing off its cached /api/folders.
            if let err = model.library?.error,
               model.library?.tree.isEmpty ?? true {
                errorOverlay(err)
            } else if model.library?.loading == true,
                      model.library?.tree.isEmpty ?? true {
                ProgressView("読み込み中…")
            }
        }
        // DL完了/失敗 toast (web #toast equivalent)
        .overlay(alignment: .bottom) {
            if let toast = offline.toast {
                Text(toast)
                    .font(.subheadline)
                    .padding(.horizontal, 16).padding(.vertical, 9)
                    .background(.ultraThinMaterial, in: Capsule())
                    .padding(.bottom, 30)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
            }
        }
        .animation(.easeInOut, value: offline.toast)
    }

    private var levelStack: some View {
        VStack(spacing: 0) {
            resumePill
            filterChipsRow
            if let library = model.library {
                if search.active {
                    SearchResultsView(search: search, library: library,
                                      onOpen: openWork)
                } else {
                    FolderLevelView(path: "", library: library,
                                    onOpen: openWork, searchExternal: true)
                }
            }
        }
    }

    /// `#filter-chips` + funnel — active facet chips (✕ re-searches) and the
    /// sheet button with an active count badge.
    @ViewBuilder
    private var filterChipsRow: some View {
        let chips = search.artists.sorted().map { ("🎨", $0) }
            + search.tags.sorted().map { ("🏷", $0) }
        if !chips.isEmpty || search.active {
            HStack(spacing: 6) {
                Button { showFacets = true } label: {
                    Image(systemName: "line.3.horizontal.decrease.circle")
                    if !chips.isEmpty {
                        Text("\(chips.count)").font(.caption2)
                    }
                }
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 5) {
                        ForEach(Array(chips.enumerated()), id: \.offset) { _, c in
                            HStack(spacing: 3) {
                                Text(c.0 + c.1).font(.caption2).lineLimit(1)
                                Button {
                                    if c.0 == "🎨" { search.artists.remove(c.1) }
                                    else { search.tags.remove(c.1) }
                                    search.filtersChanged()
                                } label: {
                                    Image(systemName: "xmark")
                                        .font(.system(size: 8, weight: .bold))
                                }
                            }
                            .padding(.horizontal, 8).padding(.vertical, 4)
                            .background(.secondary.opacity(0.15), in: Capsule())
                        }
                    }
                    .padding(.horizontal, 4)
                }
            }
            .padding(.horizontal, 12).padding(.vertical, 4)
        }
    }

    /// 続きから pill — most recent local position.
    @ViewBuilder
    private var resumePill: some View {
        if let last = model.progress.lastRead {
            Button { openWork(last.path) } label: {
                HStack(spacing: 6) {
                    Image(systemName: "book.fill")
                    Text("続きから: \(last.entry.title ?? last.path) p.\(last.entry.page + 1)")
                        .lineLimit(1)
                    Spacer()
                    Image(systemName: "chevron.right")
                }
                .font(.subheadline)
                .padding(.horizontal, 14).padding(.vertical, 8)
                .background(.ultraThinMaterial, in: Capsule())
                .padding(.horizontal, 12)
                .padding(.vertical, 6)
            }
            .buttonStyle(.plain)
        }
    }

    @ToolbarContentBuilder
    private var toolbar: some ToolbarContent {
        ToolbarItem(placement: .primaryAction) {
            Menu {
                Picker("表示", selection: layoutBinding) {
                    Label("タイル", systemImage: "square.grid.2x2").tag(AppConfig.LayoutMode.tile)
                    Label("リスト", systemImage: "list.bullet").tag(AppConfig.LayoutMode.list)
                    Label("カード", systemImage: "rectangle.stack").tag(AppConfig.LayoutMode.card)
                }
                // 全表示 entry — always reachable (the web's equivalent is a
                // hidden swipe on the root icon, always armed too). Eye
                // toggles and badges only appear while this mode is on.
                Toggle("隠しルートを表示",
                       isOn: Binding(
                        get: { visibility.showAll },
                        set: { visibility.showAll = $0 }))
                Divider()
                Button { Task { await model.library?.refresh() } } label: {
                    Label("再読み込み", systemImage: "arrow.clockwise")
                }
                if offline.workCount > 0 {
                    Divider()
                    // #storage-text equivalent (static menu row)
                    Text(offlineLabel)
                }
            } label: {
                Image(systemName: "ellipsis.circle")
            }
        }
        ToolbarItem(placement: .automatic) {
            Button { showSettings = true } label: {
                Image(systemName: "gearshape")
            }
        }
    }

    private var offlineLabel: String {
        let mb = Double(offline.totalBytes) / 1_000_000
        let size = mb >= 1000
            ? String(format: "%.1f GB", mb / 1000)
            : String(format: "%.0f MB", mb)
        return "オフライン: \(offline.workCount) 作品・\(size)"
    }

    private var layoutBinding: Binding<AppConfig.LayoutMode> {
        Binding(
            get: { model.config.layoutMode },
            set: { model.config.layoutMode = $0 })
    }

    private func title(for path: String) -> String {
        BrowseModel.splitDisplayName(path).leaf
    }

    /// `guardPath` — pending judgments queue behind a refresh; positive-
    /// hidden paths never open the reader in normal mode.
    private func openWork(_ path: String) {
        switch visibility.guardPath(path) {
        case .pending:
            Task {
                await visibility.refresh()
                guard visibility.guardPath(path) != .hidden else { return }
                model.openReader = BrowseRoute.reader(path)
            }
        case .hidden:
            return
        case .allow:
            model.openReader = BrowseRoute.reader(path)
        }
    }

    private func errorOverlay(_ err: String) -> some View {
        VStack(spacing: 12) {
            Image(systemName: "exclamationmark.triangle").font(.largeTitle)
            Text(err).font(.footnote).multilineTextAlignment(.center)
            Button("再試行") { Task { await model.library?.load(force: true) } }
        }
        .padding()
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(.background)
    }
}

/// Navigation routes inside the library stack.
enum BrowseRoute: Hashable {
    case level(String)
    case reader(String)

    var path: String {
        switch self {
        case .level(let p), .reader(let p): return p
        }
    }
}

extension BrowseRoute: Identifiable {
    var id: String { path }
}
