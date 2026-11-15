import CryptoKit
import ImageIO
import UIKit

/// Loads story pictures without touching the main thread: download, downsample and decode all happen on
/// background tasks. Decoded pictures live in memory (80 MB), the downloaded files on disk in Caches for 7 days.
actor ImageLoader {
    static let shared = ImageLoader()
    static let diskLifetime: TimeInterval = 7 * 86_400

    /// NSCache is thread-safe, so a card can ask for a decoded picture synchronously while it is being built.
    private final class Memory: @unchecked Sendable {
        let cache = NSCache<NSString, UIImage>()
        init() { cache.totalCostLimit = 80_000_000 }
        func object(forKey key: NSString) -> UIImage? { cache.object(forKey: key) }
        func setObject(_ image: UIImage, forKey key: NSString, cost: Int) { cache.setObject(image, forKey: key, cost: cost) }
    }
    private nonisolated let memory = Memory()
    private var inflight: [String: Task<UIImage?, Never>] = [:]
    private let session: URLSession
    private nonisolated let directory: URL

    init(directory: URL? = nil, session: URLSession? = nil) {
        let base = directory ?? FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("images", isDirectory: true)
        try? FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        self.directory = base
        if let session {
            self.session = session
        } else {
            let config = URLSessionConfiguration.default
            // The files are kept by this class, so URLCache would only store them twice.
            config.urlCache = nil
            config.timeoutIntervalForRequest = 15
            config.httpMaximumConnectionsPerHost = 6
            self.session = URLSession(configuration: config)
        }
        Task.detached(priority: .utility) { [base] in Self.prune(base) }
    }

    private nonisolated static func key(_ url: URL, _ maxPixel: Int) -> String { "\(maxPixel)|\(url.absoluteString)" }

    /// A picture that is already decoded, or nil. Safe to call from the main thread; it never reads the disk.
    nonisolated func cached(_ url: URL, maxPixel: Int) -> UIImage? {
        memory.object(forKey: Self.key(url, maxPixel) as NSString)
    }

    /// The picture at `url`, no larger than `maxPixel` on its longer side, decoded and ready to draw.
    func image(for url: URL, maxPixel: Int) async -> UIImage? {
        let key = Self.key(url, maxPixel)
        if let hit = memory.object(forKey: key as NSString) { return hit }
        if let running = inflight[key] { return await running.value }

        let file = fileURL(for: url)
        let session = session
        let task = Task.detached(priority: .userInitiated) { () -> UIImage? in
            guard let data = await Self.data(for: url, file: file, session: session) else { return nil }
            return Self.downsample(data, maxPixel: maxPixel)
        }
        inflight[key] = task
        let image = await task.value
        inflight[key] = nil
        if let image {
            memory.setObject(image, forKey: key as NSString, cost: Int(image.size.width * image.size.height * image.scale * image.scale * 4))
        }
        return image
    }

    /// Warms the cache for cards the reader is about to reach.
    nonisolated func prefetch(_ urls: [URL], maxPixel: Int) {
        for url in urls where cached(url, maxPixel: maxPixel) == nil {
            Task(priority: .utility) { _ = await self.image(for: url, maxPixel: maxPixel) }
        }
    }

    /// The downloaded file's bytes, from disk when it is there. Used for notification attachments and sharing.
    func data(for url: URL) async -> Data? {
        await Self.data(for: url, file: fileURL(for: url), session: session)
    }

    private nonisolated func fileURL(for url: URL) -> URL {
        let digest = SHA256.hash(data: Data(url.absoluteString.utf8))
        return directory.appendingPathComponent(digest.prefix(16).map { String(format: "%02x", $0) }.joined())
    }

    private static func data(for url: URL, file: URL, session: URLSession) async -> Data? {
        if let data = try? Data(contentsOf: file, options: .mappedIfSafe), !data.isEmpty { return data }
        guard url.scheme == "https" || url.scheme == "http" || url.isFileURL,
              let (data, response) = try? await session.data(from: url),
              (response as? HTTPURLResponse).map({ (200..<300).contains($0.statusCode) }) ?? true,
              !data.isEmpty else { return nil }
        try? data.write(to: file, options: .atomic)
        return data
    }

    /// Decodes at the size the screen needs, so a 1290 px picture never costs more memory than the card shows.
    /// ImageIO reads JPEG, PNG and WebP.
    static func downsample(_ data: Data, maxPixel: Int) -> UIImage? {
        let sourceOptions = [kCGImageSourceShouldCache: false] as CFDictionary
        guard let source = CGImageSourceCreateWithData(data as CFData, sourceOptions) else { return nil }
        let options = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceShouldCacheImmediately: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: max(maxPixel, 1),
        ] as CFDictionary
        guard let cgImage = CGImageSourceCreateThumbnailAtIndex(source, 0, options) else { return nil }
        return UIImage(cgImage: cgImage)
    }

    private static func prune(_ directory: URL, now: Date = Date()) {
        let files = (try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: [.contentModificationDateKey])) ?? []
        for file in files {
            let modified = (try? file.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate ?? now
            if now.timeIntervalSince(modified) > diskLifetime { try? FileManager.default.removeItem(at: file) }
        }
    }
}

extension ImageLoader {
    /// Pixel size for the card's main picture on this screen.
    @MainActor static var heroPixels: Int {
        let screen = UIScreen.main
        return Int((min(screen.bounds.width, screen.bounds.height) * screen.scale).rounded(.up))
    }

    /// Pixel size for list thumbnails.
    @MainActor static var thumbPixels: Int { Int(120 * UIScreen.main.scale) }
}
