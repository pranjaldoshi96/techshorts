import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// Fetches every feed directly. The same code runs in the app and on the Techgist server.
struct FeedService {
    static let perFeedLimit = 12

    /// The published edition while it's fresh; otherwise the publishers' feeds, fetched directly.
    func latest(onProgress: (@Sendable ([Article]) async -> Void)? = nil)
        async -> (articles: [Article], failed: [String], origin: NewsOrigin) {
        if let edition = await fetchEdition(), edition.isFresh {
            return (edition.articles, [], .edition(publishedAt: edition.publishedAt))
        }
        let result = await fetchAll(onProgress: onProgress)
        return (result.articles, result.failed, .direct)
    }

    func fetchEdition() async -> Edition? {
        var url = Edition.url
        #if DEBUG
        // Lets a test point the app at another edition, e.g. SIMCTL_CHILD_EDITION_URL=file:///…/edition.json.
        if let override = ProcessInfo.processInfo.environment["EDITION_URL"].flatMap(URL.init(string:)) { url = override }
        #endif
        let request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 10)
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              (response as? HTTPURLResponse)?.statusCode ?? 200 == 200 else { return nil }
        return Edition.decode(data)
    }

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

    static func merge(_ feeds: [[Article]]) -> [Article] {
        // The same link can appear in several feeds (TechCrunch and TechCrunch Startups, or a story on Hacker News).
        // Keep one copy in the more specific category, but remember the other sources and HN points for ranking.
        var byKey: [String: Article] = [:]
        for article in feeds.joined() {
            let key = dedupeKey(article.url)
            guard let existing = byKey[key] else { byKey[key] = article; continue }
            let preferNew = existing.category == .top && article.category != .top
            var kept = preferNew ? article : existing
            let dropped = preferNew ? existing : article
            kept.points = [kept.points, dropped.points].compactMap { $0 }.max()
            kept.alsoCoveredBy = (kept.alsoCoveredBy ?? []) + [dropped.source] + (dropped.alsoCoveredBy ?? [])
            byKey[key] = kept
        }
        return Ranker.rank(Array(byKey.values))
    }

    func fetch(_ source: FeedSource) async throws -> [Article] {
        var request = URLRequest(url: source.url, timeoutInterval: 20)
        request.setValue("Techgist/1.0 (iOS RSS reader)", forHTTPHeaderField: "User-Agent")
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
        let rawTitle = HTMLText.plain(item.title)
        // Link posts sometimes keep the page's site suffix, e.g. "… - YouTube".
        let title = rawTitle.replacingOccurrences(of: "\\s+[-–|]\\s+YouTube$", with: "", options: .regularExpression)
        guard !title.isEmpty,
              let url = URL(string: item.link.trimmingCharacters(in: .whitespacesAndNewlines)),
              url.scheme == "https" || url.scheme == "http",
              let date = FeedDate.parse(item.date) else { return nil }

        var body = HTMLText.clean(HTMLText.plain(item.html))
        for prefix in [rawTitle, title] where body.count > prefix.count && body.lowercased().hasPrefix(prefix.lowercased()) {
            body = String(body.dropFirst(prefix.count)).trimmingCharacters(in: .whitespacesAndNewlines)
            break
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
            publishedAt: date,
            points: HTMLText.firstMatch("Points: (\\d+)", in: item.html).flatMap(Int.init)
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
