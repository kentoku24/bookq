import SwiftUI

/// Minimal placeholder reader (skeleton phase): RTL horizontal paging over
/// the folder's direct images. Phase 3 replaces this with the full reader
/// (spread/chapter/zoom/progress-sync). It still exercises the whole
/// auth → /api/images → /view pipeline end to end.
struct ReaderView: View {
    let path: String

    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss

    @State private var pages: [String] = []
    @State private var index = 0
    @State private var error: String?
    @State private var loading = true
    @State private var showChrome = true

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            if loading {
                ProgressView().tint(.white)
            } else if let error {
                VStack(spacing: 12) {
                    Image(systemName: "exclamationmark.triangle")
                        .font(.largeTitle).foregroundStyle(.white)
                    Text(error).foregroundStyle(.white).font(.footnote)
                    Button("閉じる") { dismiss() }
                        .buttonStyle(.borderedProminent)
                }
            } else if pages.isEmpty {
                Text("画像がありません").foregroundStyle(.white)
            } else {
                pager
            }
        }
        .statusBarHidden(!showChrome)
        .overlay(alignment: .top) { if showChrome { chrome } }
        .task { await load() }
        .onDisappear { record() }
    }

    private var pager: some View {
        // RTL: paging moves right-to-left like the web reader's manga layout.
        TabView(selection: $index) {
            ForEach(Array(pages.enumerated()), id: \.offset) { idx, name in
                RemoteImageView(url: pageURL(name), contentMode: .fit)
                    .background(Color.black)
                    .tag(idx)
                    .onTapGesture { withAnimation { showChrome.toggle() } }
            }
        }
        .tabViewStyle(.page(indexDisplayMode: .never))
        .environment(\.layoutDirection, .rightToLeft)
        .onChange(of: index) { _, _ in record() }
    }

    private var chrome: some View {
        VStack {
            HStack {
                Button { dismiss() } label: {
                    Image(systemName: "xmark")
                        .padding(10)
                        .background(.black.opacity(0.5), in: Circle())
                }
                Spacer()
                Text("\(index + 1) / \(pages.count)")
                    .font(.subheadline).monospacedDigit()
                    .foregroundStyle(.white)
                    .padding(6)
                    .background(.black.opacity(0.5), in: Capsule())
                Spacer()
                Color.clear.frame(width: 44)
            }
            .padding(10)
            Spacer()
            Slider(
                value: Binding(
                    get: { Double(index) },
                    set: { index = Int($0.rounded()) }
                ),
                in: 0...Double(max(pages.count - 1, 0)),
                step: 1
            )
            .padding(.horizontal, 20)
            .padding(.bottom, 30)
            .tint(.white)
        }
        .transition(.opacity)
    }

    private func pageURL(_ name: String) -> URL? {
        model.api?.pageURL(folder: path, name: name,
                           original: model.config.originalImages)
    }

    private func load() async {
        guard let api = model.api else { error = "未接続"; loading = false; return }
        do {
            pages = try await api.images(folder: path)
            // restore saved page
            if let saved = model.progress.entries[path], saved.page < pages.count {
                index = saved.page
            }
        } catch {
            self.error = error.localizedDescription
        }
        loading = false
    }

    private func record() {
        guard !pages.isEmpty else { return }
        let title = BrowseModel.splitDisplayName(path).leaf
        model.progress.record(path: path, page: index, pages: pages.count, title: title)
    }
}
