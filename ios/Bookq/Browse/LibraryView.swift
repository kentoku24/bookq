import SwiftUI

/// Root browse screen: NavigationStack of FolderLevelViews + reader modal.
struct LibraryView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var offline: OfflineStore
    @EnvironmentObject private var search: SearchModel
    @EnvironmentObject private var visibility: VisibilityStore
    @EnvironmentObject private var pair: PairStore
    @State private var showSettings = false
    @State private var showFacets = false
    @State private var showPair = false
    /// Epoch the user manually dismissed — the paired reader auto-opens on
    /// each new work commit but never yanks back mid-browse within one.
    @State private var pairSuppressedEpoch: Int?
    /// Rightward travel of the nav-bar "bookq" title while the showAll drag
    /// is armed (web `_bindEntryGesture` translateX parity).
    @State private var bookqDragX: CGFloat = 0
    /// Transient bottom pill — the showAll toggle feedback toast.
    @State private var visNotice: String?
    /// Content width for the drag's right-edge (90%) activation threshold.
    @State private var swipeWidth: CGFloat = 0

    var body: some View {
        NavigationStack(path: $model.navPath) {
            levelStack
                .navigationTitle("bookq")
                .navigationBarTitleDisplayMode(.inline)
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
        // Paired reader: auto-presents whenever the shared session enters
        // 'reading' (each workEpoch once — manual dismissal is remembered).
        .fullScreenCover(isPresented: pairReaderBinding) {
            PairedReaderView()
        }
        .sheet(isPresented: $showSettings) {
            SettingsView()
        }
        .sheet(isPresented: $showFacets) {
            FacetSheet(search: search)
        }
        .sheet(isPresented: $showPair) {
            PairSheet()
        }
        .searchable(text: $search.query, prompt: "作品を検索")
        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: {
            swipeWidth = $0
        }
        .onChange(of: visNotice) { _, v in
            guard v != nil else { return }
            Task {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                visNotice = nil
            }
        }
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
            // same way the web keeps browsing off its cached /api/v1/tree.
            if let err = model.library?.error,
               model.library?.tree.isEmpty ?? true {
                errorOverlay(err)
            } else if model.library?.loading == true,
                      model.library?.tree.isEmpty ?? true {
                ProgressView("読み込み中…")
            }
        }
        // DL完了/失敗 toast + showAll トグル toast (web #toast equivalent)
        .overlay(alignment: .bottom) {
            if let toast = offline.toast ?? visNotice {
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
            pairBanner
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
        // "bookq" title in the buttons' row — the hidden showAll entry
        // gesture lives here (web `#browse-root-icon` parity): drag it to
        // the right edge (≥90% width) and release to toggle 全表示モード.
        ToolbarItem(placement: .principal) {
            Text("bookq")
                .font(.headline)
                .onGeometryChange(for: CGFloat.self) {
                    $0.frame(in: .global).minX
                } action: {
                    bookqMinX = $0
                }
                .offset(x: max(0, bookqDragX))
                .contentShape(Rectangle())
                .gesture(bookqSwipe)
        }
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
        ToolbarItem(placement: .automatic) {
            Button { showPair = true } label: {
                Image(systemName: pair.bound
                      ? "person.2.fill" : "person.2")
            }
            .accessibilityLabel("ペアリング")
        }
    }

    /// Global x of the title's left edge — the drag's `location` is local
    /// to the text, so edge detection needs this offset.
    @State private var bookqMinX: CGFloat = 0

    /// showAll ギミックのドラッグ — web `_bindEntryGesture` parity:
    /// rightward-only arm (dx>12 かつ dx>1.2|dy|; 縦スクロールや左フリック
    /// は渡す)、アーム中はタイトルが指に追従、右端(幅90%)での解放で発火。
    private var bookqSwipe: some Gesture {
        DragGesture(minimumDistance: 8)
            .onChanged { v in
                let dx = v.translation.width, dy = v.translation.height
                if bookqDragX == 0,
                   !(dx > 12 && dx > abs(dy) * 1.2) { return }
                bookqDragX = max(0, dx)
            }
            .onEnded { v in
                defer { bookqDragX = 0 }
                let globalX = bookqMinX + v.location.x
                guard swipeWidth > 0,
                      globalX >= swipeWidth * 0.9 else { return }
                visibility.showAll.toggle()
                visNotice = visibility.showAll
                    ? "全表示モード（再読み込みで解除）"
                    : "通常モードに戻しました"
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
        // Paired + leader: a work pick is a shared OPEN_WORK intent, never
        // a local solo open (pairController navHandler equivalent). The
        // follower's tap stays local — the pair survives its own browse.
        if pair.role == .leader && pair.paired {
            Task { _ = await pair.pickWork(folder: path) }
            return
        }
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

    /// Paired-reader cover binding: opens on phase→reading once per
    /// workEpoch; dismissing records the epoch so it stays browsable
    /// until the next shared work commit. `selecting` also presents —
    /// the leader's selecting overlay is where a new pick happens, and
    /// without it the re-entry banner dead-ends. `loading`/`paused` stay
    /// mounted too — they are transient phases with their own overlays;
    /// dismissing on them makes the leader's onDisappear fire a spurious
    /// SET_ACTIVITY selecting (#216).
    private var pairReaderBinding: Binding<Bool> {
        Binding(
            get: {
                PairReaderGate.presents(pair.phase) &&
                pairSuppressedEpoch != pair.lastState?.workEpoch
            },
            set: { presented in
                if !presented { pairSuppressedEpoch = pair.lastState?.workEpoch }
            })
    }

    /// "同期中" banner — re-entry into the shared reader while it is
    /// suppressed or while waiting for the leader's pick.
    @ViewBuilder
    private var pairBanner: some View {
        if pair.paired, let ph = pair.phase {
            Button {
                // -1 never equals a real workEpoch → the cover binding
                // sees a mismatch and presents the paired reader.
                pairSuppressedEpoch = -1
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "person.2.fill")
                    Text(bannerText(ph)).lineLimit(1)
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

    private func bannerText(_ ph: PairPhase) -> String {
        switch ph {
        case .reading:
            let p = pair.projection
            return p.map { "同期中: \($0.spreadIndex + 1)/\($0.spreadTotal)" } ?? "同期中"
        case .selecting:
            return pair.role == .leader ? "ペア中: 作品を選んでください" : "ペア中: リーダーが選択中"
        case .loading: return "ペア中: 読み込み中…"
        case .paused: return "ペア中: 相手が離席中"
        case .waiting: return "ペア中: 参加者待ち"
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
