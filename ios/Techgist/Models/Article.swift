import Foundation

enum Category: String, Codable, CaseIterable, Identifiable, Sendable {
    case top = "Top", ai = "AI", startups = "Startups", dev = "Dev", blogs = "Blogs"
    var id: String { rawValue }
}

struct Article: Codable, Identifiable, Hashable, Sendable {
    let url: URL
    let title: String
    /// Short excerpt from the feed, or the page's meta description. Shown when there is no AI summary.
    var excerpt: String
    /// Plain-text body from the feed, used only as input for on-device summaries.
    var content: String
    var image: URL?
    let source: String
    let category: Category
    let publishedAt: Date
    /// Hacker News points, when the story is on the HN front page.
    var points: Int?
    /// Other outlets reporting the same story, used for ranking and shown on the card.
    var alsoCoveredBy: [String]?
    /// Position in the Top tab, which mixes Top sources with the strongest stories from every other tab.
    var topRank: Int?

    var id: String { url.absoluteString }
}

struct ArticleCache: Codable {
    var updatedAt: Date
    var articles: [Article]

    static let fileName = "articles.json"
    static func load() -> ArticleCache? { Disk.load(ArticleCache.self, from: fileName, in: .cachesDirectory) }
    static func save(_ articles: [Article], at date: Date) {
        Disk.save(ArticleCache(updatedAt: date, articles: articles), to: fileName, in: .cachesDirectory)
    }
}

enum Keys {
    static let disabledSources = "disabledSources"
    static let aiSummaries = "aiSummariesEnabled"
    static let digestEnabled = "digestEnabled"
    static let digestMinute = "digestMinuteOfDay"
}
