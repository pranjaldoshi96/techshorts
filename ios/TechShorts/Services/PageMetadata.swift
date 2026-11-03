import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// Reads og:image / og:description from an article page, for feeds that ship without an image or excerpt.
actor PageMetadata {
    struct Meta: Sendable {
        var image: URL?
        var description: String?
    }

    static let shared = PageMetadata()
    private var cache: [URL: Meta] = [:]
    private var inflight: [URL: Task<Meta?, Never>] = [:]

    func meta(for url: URL) async -> Meta? {
        if let m = cache[url] { return m }
        if let t = inflight[url] { return await t.value }
        let task = Task { await Self.fetch(url) }
        inflight[url] = task
        let meta = await task.value
        inflight[url] = nil
        if let meta { cache[url] = meta }
        return meta
    }

    private static func fetch(_ url: URL) async -> Meta? {
        var request = URLRequest(url: url, timeoutInterval: 10)
        request.setValue("Techgist/1.0 (iOS RSS reader)", forHTTPHeaderField: "User-Agent")
        guard let (data, _) = try? await URLSession.shared.data(for: request) else { return nil }
        let html = String(decoding: data.prefix(500_000), as: UTF8.self)
        let head = html.range(of: "</head>", options: .caseInsensitive).map { String(html[..<$0.lowerBound]) } ?? html

        var props: [String: String] = [:]
        for tag in HTMLText.allMatches("(<meta\\b[^>]*>)", in: head) {
            guard let key = HTMLText.firstMatch("(?:property|name)\\s*=\\s*[\"']([^\"']+)[\"']", in: tag)?.lowercased(),
                  let content = HTMLText.firstMatch("content\\s*=\\s*(?:\"([^\"]*)\"|'([^']*)')", in: tag),
                  props[key] == nil else { continue }
            props[key] = HTMLText.decodeEntities(content)
        }

        let image = (props["og:image"] ?? props["twitter:image"])
            .flatMap { URL(string: $0, relativeTo: url)?.absoluteURL }
            .flatMap { $0.scheme == "https" ? $0 : nil }
        let description = (props["og:description"] ?? props["description"] ?? props["twitter:description"])
            .map { HTMLText.truncate(HTMLText.plain($0), words: 60) }
            .flatMap { $0.isEmpty ? nil : $0 }
        return Meta(image: image, description: description)
    }
}
