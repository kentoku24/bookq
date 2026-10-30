import SwiftUI

/// Root browse screen: NavigationStack of FolderLevelViews + reader modal.
struct LibraryView: View {
    @EnvironmentObject private var model: AppModel
    @State private var showSettings = false

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
        .task {
            await model.library?.load()
            model.library?.startPolling()
        }
        .overlay {
            if let err = model.library?.error {
                errorOverlay(err)
            } else if model.library?.loading == true,
                      model.library?.tree.isEmpty ?? true {
                ProgressView("読み込み中…")
            }
        }
    }

    private var levelStack: some View {
        VStack(spacing: 0) {
            resumePill
            if let library = model.library {
                FolderLevelView(path: "", library: library, onOpen: openWork)
            }
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
                if !(model.library?.hiddenRoots.isEmpty ?? true) {
                    Toggle("隠しルートを表示",
                           isOn: Binding(
                            get: { model.library?.showAll ?? false },
                            set: { model.library?.showAll = $0 }))
                }
                Divider()
                Button { Task { await model.library?.refresh() } } label: {
                    Label("再読み込み", systemImage: "arrow.clockwise")
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

    private var layoutBinding: Binding<AppConfig.LayoutMode> {
        Binding(
            get: { model.config.layoutMode },
            set: { model.config.layoutMode = $0 })
    }

    private func title(for path: String) -> String {
        BrowseModel.splitDisplayName(path).leaf
    }

    private func openWork(_ path: String) {
        model.openReader = BrowseRoute.reader(path)
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
