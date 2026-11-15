import Foundation

/// A ranked set of stories built and published by the Techgist server (see `server/` in the repo).
/// The app shows it while it's under 8 hours old, and fetches the publishers' feeds itself when it isn't.
struct Edition: Codable, Sendable {
    var publishedAt: Date
    var articles: [Article]
    /// True when the server published without its AI text (publisher titles and excerpts only).
    var degraded: Bool?

    static let url = URL(string: "https://raw.githubusercontent.com/pranjaldoshi96/techshorts/edition/edition.json")!
    static let maxAge: TimeInterval = 8 * 3600

    var isFresh: Bool { isFresh(at: Date()) }
    func isFresh(at now: Date) -> Bool { now.timeIntervalSince(publishedAt) < Self.maxAge && !articles.isEmpty }

    enum CodingKeys: String, CodingKey { case publishedAt, articles, degraded }

    static func decode(_ data: Data) -> Edition? {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return try? decoder.decode(Edition.self, from: data)
    }

    func encoded() throws -> Data {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return try encoder.encode(self)
    }
}

extension Edition {
    private struct Lossy: Decodable {
        let article: Article?
        init(from decoder: Decoder) throws { article = try? Article(from: decoder) }
    }

    /// 1.2 rejected the whole edition when one article didn't decode (an unknown category, say) and fell back to
    /// the raw feeds. 2.0 drops just that article. An edition with nothing left isn't fresh, so the fallback still runs.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        publishedAt = try c.decode(Date.self, forKey: .publishedAt)
        articles = try c.decode([Lossy].self, forKey: .articles).compactMap(\.article)
        degraded = (try? c.decodeIfPresent(Bool.self, forKey: .degraded)) ?? nil
    }
}

/// Where the stories on screen came from.
enum NewsOrigin: Equatable, Sendable {
    case edition(publishedAt: Date, degraded: Bool)
    case direct
}
