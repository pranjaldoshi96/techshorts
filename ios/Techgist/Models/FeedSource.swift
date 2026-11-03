import Foundation

struct FeedSource: Identifiable, Hashable, Sendable {
    let name: String
    let category: Category
    let url: URL
    /// Ranking multiplier. High-volume community feeds get less than 1 so they don't crowd out news.
    let weight: Double
    var id: String { name }

    init(_ name: String, _ category: Category, _ url: String, weight: Double = 1) {
        self.name = name
        self.category = category
        self.url = URL(string: url)!
        self.weight = weight
    }

    private static let byName = Dictionary(all.map { ($0.name, $0) }, uniquingKeysWith: { a, _ in a })

    static func weight(of name: String) -> Double { byName[name]?.weight ?? 1 }

    /// Feeds from the same site (TechCrunch and TechCrunch Startups) count as one publisher.
    static func publisher(of name: String) -> String {
        guard let host = byName[name]?.url.host else { return name }
        let parts = host.split(separator: ".")
        return parts.count >= 2 ? String(parts[parts.count - 2]) : host
    }

    static let all: [FeedSource] = [
        FeedSource("TechCrunch", .top, "https://techcrunch.com/feed/"),
        FeedSource("The Verge", .top, "https://www.theverge.com/rss/index.xml"),
        FeedSource("Ars Technica", .top, "https://feeds.arstechnica.com/arstechnica/index"),
        FeedSource("Wired", .top, "https://www.wired.com/feed/rss"),
        FeedSource("Hacker News", .top, "https://hnrss.org/frontpage?points=150"),

        FeedSource("MIT Tech Review", .ai, "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
        FeedSource("The Decoder", .ai, "https://the-decoder.com/feed/"),
        FeedSource("Google AI", .ai, "https://blog.google/technology/ai/rss/"),
        FeedSource("OpenAI", .ai, "https://openai.com/news/rss.xml"),
        FeedSource("Latent Space", .ai, "https://www.latent.space/feed"),

        FeedSource("TechCrunch Startups", .startups, "https://techcrunch.com/category/startups/feed/"),
        FeedSource("Product Hunt", .startups, "https://www.producthunt.com/feed", weight: 0.7),
        FeedSource("Lenny's Newsletter", .startups, "https://www.lennysnewsletter.com/feed"),

        FeedSource("GitHub Blog", .dev, "https://github.blog/feed/"),
        FeedSource("Cloudflare", .dev, "https://blog.cloudflare.com/rss/"),
        FeedSource("Changelog", .dev, "https://changelog.com/feed"),
        FeedSource("DEV Community", .dev, "https://dev.to/feed", weight: 0.4),

        FeedSource("Simon Willison", .blogs, "https://simonwillison.net/atom/everything/"),
        FeedSource("Pragmatic Engineer", .blogs, "https://newsletter.pragmaticengineer.com/feed"),
        FeedSource("Stratechery", .blogs, "https://stratechery.com/feed/"),
        FeedSource("Martin Fowler", .blogs, "https://martinfowler.com/feed.atom"),
        FeedSource("Julia Evans", .blogs, "https://jvns.ca/atom.xml"),
    ]
}
