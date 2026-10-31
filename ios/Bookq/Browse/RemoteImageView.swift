import SwiftUI
import UIKit

/// In-memory image cache + deduplicated fetcher. `AsyncImage` cannot send
/// Authorization headers, so all remote art goes through this loader.
@MainActor
final class RemoteImageLoader: ObservableObject {
    static let shared = RemoteImageLoader()

    private let cache = NSCache<NSString, UIImage>()
    private var inFlight: [URL: Task<UIImage?, Never>] = [:]
    var api: BookqAPI?
    /// SW cache-first equivalent: resolve a remote URL to a downloaded
    /// offline file before hitting the network.
    var offlineResolver: ((URL) -> URL?)?
    /// Network fetch seam — tests inject a stub to hold a fetch in-flight.
    var fetcher: ((URL) async throws -> Data)?
    /// Bumped by clear() — an in-flight fetch that outlives a server /
    /// account switch must not write the old scope's image back into the
    /// cache.
    private var cacheEpoch = 0

    init() {
        cache.countLimit = 400
        cache.totalCostLimit = 128 * 1024 * 1024
    }

    func image(for url: URL) -> UIImage? {
        cache.object(forKey: url.absoluteString as NSString)
    }

    func load(_ url: URL) async -> UIImage? {
        if let hit = image(for: url) { return hit }
        if let existing = inFlight[url] { return await existing.value }
        let epoch = cacheEpoch
        let task = Task<UIImage?, Never> { [api, offlineResolver, fetcher] in
            if let local = offlineResolver?(url),
               let img = UIImage(contentsOfFile: local.path) {
                guard self.cacheEpoch == epoch else { return nil }
                let cost = Int(img.size.width * img.size.height * 4)
                self.cache.setObject(img, forKey: url.absoluteString as NSString, cost: cost)
                return img
            }
            do {
                let data: Data
                if let fetcher {
                    data = try await fetcher(url)
                } else {
                    guard let api else { return nil }
                    data = try await api.imageData(url: url)
                }
                guard let img = UIImage(data: data, scale: 1) else { return nil }
                guard self.cacheEpoch == epoch else { return nil }
                let cost = Int(img.size.width * img.size.height * 4)
                self.cache.setObject(img, forKey: url.absoluteString as NSString, cost: cost)
                return img
            } catch {
                return nil
            }
        }
        inFlight[url] = task
        let result = await task.value
        inFlight[url] = nil
        return result
    }

    func clear() {
        cacheEpoch += 1
        cache.removeAllObjects()
        inFlight.values.forEach { $0.cancel() }
        inFlight.removeAll()
    }
}

/// AsyncImage equivalent that attaches the bearer token.
struct RemoteImageView: View {
    let url: URL?
    var contentMode: ContentMode = .fill
    var onImageLoaded: ((UIImage) -> Void)? = nil
    @State private var image: UIImage?
    @State private var failed = false

    var body: some View {
        Group {
            if let image {
                Image(uiImage: image)
                    .resizable()
                    .aspectRatio(contentMode: contentMode)
            } else {
                placeholder
            }
        }
        .task(id: url) { await load() }
    }

    private var placeholder: some View {
        Rectangle()
            .fill(Color(white: 0.16))
            .overlay {
                Image(systemName: failed ? "exclamationmark.triangle" : "book.closed")
                    .foregroundStyle(.secondary)
            }
    }

    private func load() async {
        // No cover URL = absent thumb (web shows the neutral placeholder);
        // ⚠️ is reserved for a fetch that actually failed.
        guard let url else { return }
        image = await RemoteImageLoader.shared.load(url)
        failed = image == nil
        if let image { onImageLoaded?(image) }
    }
}
