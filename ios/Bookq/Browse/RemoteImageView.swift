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
        // A decoded 2560px reader page costs ~150MB+ as RGBA — the old
        // 128MB cap meant full-res pages could never stay cached and
        // every revisit paid a refetch+redecode. 512MB holds a few
        // spreads; NSCache still evicts under real memory pressure.
        cache.totalCostLimit = 512 * 1024 * 1024
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

    /// Solid dark-gray stand-in rendered while `image == nil`. Keeping the
    /// body a single `Image` (only the UIImage value changes on load) avoids
    /// the if/else view-identity swap that left Buttons inside grid cells
    /// with stale hit-test/accessibility frames.
    private static let placeholderImage: UIImage = {
        let size = CGSize(width: 2, height: 3)
        return UIGraphicsImageRenderer(size: size).image { ctx in
            UIColor(white: 0.16, alpha: 1).setFill()
            ctx.fill(CGRect(origin: .zero, size: size))
        }
    }()

    var body: some View {
        // Read the cache synchronously so a warm image renders on the FIRST
        // pass — an async nil→image swap mid-layout is what left grid-cell
        // Buttons with stale hit-test frames.
        let cached = url.flatMap { RemoteImageLoader.shared.image(for: $0) }
        return Image(uiImage: image ?? cached ?? Self.placeholderImage)
            .resizable()
            .aspectRatio(contentMode: contentMode)
            .overlay {
                Image(systemName: failed ? "exclamationmark.triangle" : "book.closed")
                    .foregroundStyle(.secondary)
                    .opacity((image ?? cached) == nil ? 1 : 0)
            }
            .task(id: url) { await load() }
    }

    private func load() async {
        // No cover URL = absent thumb (web shows the neutral placeholder);
        // ⚠️ is reserved for a fetch that actually failed.
        guard let url else { return }
        // Always store into @State — the body is a single stable Image, so
        // this write can't change the view's structure (and thus can't
        // stale a Button's hit-test frame the way the old if/else swap did).
        // Keeping the value also survives a later NSCache eviction.
        image = await RemoteImageLoader.shared.load(url)
        failed = image == nil
        if let image { onImageLoaded?(image) }
    }
}
