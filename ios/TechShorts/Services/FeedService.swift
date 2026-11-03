import Foundation

/// Fetches every feed directly from the device. No backend needed.
struct FeedService {
    static let perFeedLimit = 12

    /// `onProgress` receives the merged stories so far after each feed finishes, so a first launch
    /// can show something before the slowest feed responds.
    func fetchAll(_ sources: [FeedSource] = FeedSource.all,
                  onProgress: (@Sendable ([Article]) async -> Void)? = nil) async -> (articles: [Article], failed: [String]) {
        let results = await withTaskGroup(of: (FeedSource, [Article]?).self) { group in
            for source in sources {
                group.addTask { (source, try? await fetch(source)) }
            }
            var out: [(FeedSource, [Article]?)] = []
            for await r in group {
                out.append(r)
                if let onProgress, r.1?.isEmpty == false { await onProgress(Self.merge(out.compactMap(\.1))) }
            }
            return out
        }
        let failed = results.filter { $0.1 == nil }.map(\.0.name).sorted()
        return (Self.merge(results.compactMap(\.1)), failed)
    }

    private static func merge(_ feeds: [[Article]]) -> [Article] {
        // A story can appear in several feeds (e.g. TechCrunch and TechCrunch Startups): keep the more specific category.
        let all = feeds.joined().sorted { ($0.category == .top ? 1 : 0) < ($1.category == .top ? 1 : 0) }
        var seen = Set<String>()
        let unique = all.filter { seen.insert(dedupeKey($0.url)).inserted }
        return unique.sorted { $0.publishedAt > $1.publishedAt }
    }

    func fetch(_ source: FeedSource) async throws -> [Article] {
        var request = URLRequest(url: source.url, timeoutInterval: 20)
        request.setValue("TechShorts/1.0 (iOS RSS reader)", forHTTPHeaderField: "User-Agent")
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw URLError(.badServerResponse)
        }
        // Blogs post less often, so allow older posts there.
        let maxAgeDays: Double = source.category == .blogs ? 45 : 7
        let cutoff = Date().addingTimeInterval(-maxAgeDays * 86_400)
        return Array(
            FeedParser.parse(data)
                .compactMap { Article(item: $0, source: source) }
                .filter { $0.publishedAt > cutoff && !ContentFilter.isLowValue($0) }
                .prefix(Self.perFeedLimit)
        )
    }

    static func dedupeKey(_ url: URL) -> String {
        ((url.host ?? "") + url.path).lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "/"))
    }
}

extension Article {
    init?(item: FeedItem, source: FeedSource) {
        let title = HTMLText.plain(item.title)
        guard !title.isEmpty,
              let url = URL(string: item.link.trimmingCharacters(in: .whitespacesAndNewlines)),
              url.scheme == "https" || url.scheme == "http",
              let date = FeedDate.parse(item.date) else { return nil }

        var body = HTMLText.clean(HTMLText.plain(item.html))
        if body.count > title.count, body.lowercased().hasPrefix(title.lowercased()) {
            body = String(body.dropFirst(title.count)).trimmingCharacters(in: .whitespacesAndNewlines)
        }
        // Hacker News bodies are just "Article URL: … Comments URL: …"; those get a meta description later instead.
        let usable = !body.hasPrefix("Article URL:") && body.caseInsensitiveCompare(title) != .orderedSame
        self.init(
            url: url,
            title: title,
            excerpt: usable ? HTMLText.truncate(body, words: 60) : "",
            content: usable ? HTMLText.truncate(body, words: 1000) : "",
            image: item.image ?? HTMLText.firstImage(in: item.html),
            source: source.name,
            category: source.category,
            publishedAt: date
        )
    }
}

/// Drops shopping deals, buying guides and sponsored posts, which aren't news.
enum ContentFilter {
    private static let title = try! NSRegularExpression(pattern: [
        "\\d+% off", "\\$\\d+ off", "\\bon sale\\b", "\\b(best|lowest) price", "all-time low", "black friday", "cyber monday",
        "prime day", "gift guide", "coupon", "promo code", "\\bbest\\b.{0,60}\\bdeals?\\b", "\\bdeals? of the (day|week)\\b",
        "\\bsponsored\\b", "^(the )?\\d+ best\\b", "\\bbest\\b.*\\(20\\d\\d\\)",
    ].joined(separator: "|"), options: .caseInsensitive)

    private static let path = try! NSRegularExpression(pattern: [
        "/deals?/", "deal-sale", "-deals$", "coupon", "gift-guide", "/sponsored",
    ].joined(separator: "|"), options: .caseInsensitive)

    static func isLowValue(_ article: Article) -> Bool {
        matches(title, article.title) || matches(path, article.url.path)
    }

    private static func matches(_ re: NSRegularExpression, _ s: String) -> Bool {
        re.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)) != nil
    }
}
