import Foundation

enum Category: String, Codable, CaseIterable, Identifiable, Sendable {
    case top = "Top", ai = "AI", startups = "Startups", dev = "Dev", blogs = "Blogs"
    var id: String { rawValue }
}

/// Sized copies of a story's picture. Every field is optional, so an edition may send any subset.
struct ImageVariants: Codable, Hashable, Sendable {
    /// About 1290 px wide, for the card on phones 390 pt and wider.
    var hero: URL?
    /// About 720 px wide, for small phones and notification attachments.
    var std: URL?
    /// About 360 px wide, for lists and the compact card.
    var thumb: URL?
    /// True when the server drew the picture itself because the story had none.
    var generated: Bool?
}

/// The text of a story's alert as the server wrote it: a title of at most 40 characters and a body of at most 110.
struct AlertText: Codable, Hashable, Sendable {
    var title: String?
    var body: String?
}

struct Article: Codable, Identifiable, Hashable, Sendable {
    let url: URL
    let title: String
    /// Short excerpt from the feed, or the page's meta description. Shown when there is no AI summary.
    var excerpt: String
    /// Plain-text body from the feed. The app no longer reads it; the v1 edition still carries it.
    var content: String
    var image: URL?
    let source: String
    let category: Category
    let publishedAt: Date
    /// Hacker News points, when the story is on the HN front page.
    var points: Int?
    /// Other outlets reporting the same story, used for ranking and shown on the card.
    var alsoCoveredBy: [String]?
    /// The other outlets' own reports, by outlet name, so each name in the coverage list opens its story.
    var coverageLinks: [String: URL]?
    /// Position in the Top tab, which mixes Top sources with the strongest stories from every other tab.
    var topRank: Int?
    /// Techgist's own headline and summary, written on the server by an LLM and checked against the article.
    var headline: String?
    var summary: String?

    // Fields added for app 2.0. All are optional and decode leniently (a missing or mistyped value is nil),
    // so the v1 edition that 1.2 reads and any newer edition both decode. See `init(from:)`.

    /// The first two sentences of the summary, written to stand alone. Shown when the full summary doesn't fit.
    var summaryShort: String?
    /// One short sentence with new information, used as the body of an alert. JSON key `alertLine` (or `notifLine`).
    var alertLine: String?
    /// How tap-worthy the story is, 1 to 10. Drives the "Hot" badge and alert choice. An older 0-to-1 fraction is
    /// read as tenths.
    var spice: Int?
    /// 0-based position inside the story's own tab (`category`). `topRank` is the position in Top.
    var rank: Int?
    var images: ImageVariants?
    /// `breaking` or `developing`.
    var badge: String?
    /// The server's story type (`news`, `analysis`, `opinion`, `interview`, `release_notes`, ...). JSON key `type`.
    var kind: String?
    /// The last material update, when the story changed after it was first reported.
    var updatedAt: Date?
    /// False when the card carries the publisher's title and excerpt because no checked AI text exists.
    var aiText: Bool?
    /// The server's id for the event the story reports. A push names its story by this id.
    var eventId: String?
    /// The title and body the server wrote for this story's alert, checked against the article like the headline.
    var notification: AlertText?

    /// The headline to show: Techgist's when there is one, otherwise the publisher's.
    var displayTitle: String { headline ?? title }

    var id: String { url.absoluteString }

    enum CodingKeys: String, CodingKey {
        case url, title, excerpt, content, image, source, category, publishedAt, points, alsoCoveredBy, topRank
        case headline, summary
        case summaryShort, alertLine, spice, rank, images, badge, kind = "type", updatedAt, aiText, eventId, notification, coverageLinks
    }
}

extension Article {
    private struct AnyKey: CodingKey {
        var stringValue: String
        var intValue: Int? { nil }
        init(_ s: String) { stringValue = s }
        init?(stringValue: String) { self.stringValue = stringValue }
        init?(intValue: Int) { nil }
    }

    /// The v1 fields decode exactly as the 1.2 release decoded them (same keys, same types, same failures).
    /// The 2.0 fields never make an article fail: a value of the wrong type is treated as absent.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        url = try c.decode(URL.self, forKey: .url)
        title = try c.decode(String.self, forKey: .title)
        excerpt = try c.decode(String.self, forKey: .excerpt)
        content = try c.decode(String.self, forKey: .content)
        image = try c.decodeIfPresent(URL.self, forKey: .image)
        source = try c.decode(String.self, forKey: .source)
        category = try c.decode(Category.self, forKey: .category)
        publishedAt = try c.decode(Date.self, forKey: .publishedAt)
        points = try c.decodeIfPresent(Int.self, forKey: .points)
        alsoCoveredBy = try c.decodeIfPresent([String].self, forKey: .alsoCoveredBy)
        topRank = try c.decodeIfPresent(Int.self, forKey: .topRank)
        headline = try c.decodeIfPresent(String.self, forKey: .headline)
        summary = try c.decodeIfPresent(String.self, forKey: .summary)

        func lenient<T: Decodable>(_ key: CodingKeys) -> T? { (try? c.decodeIfPresent(T.self, forKey: key)) ?? nil }
        summaryShort = lenient(.summaryShort)
        alertLine = lenient(.alertLine)
        if alertLine == nil, let other = try? decoder.container(keyedBy: AnyKey.self) {
            alertLine = (try? other.decodeIfPresent(String.self, forKey: AnyKey("notifLine"))) ?? nil
        }
        if let whole: Int = lenient(.spice) {
            spice = whole > 0 ? min(whole, 10) : nil
        } else if let number: Double = lenient(.spice), number > 0 {
            spice = min(max(Int((number <= 1 ? number * 10 : number).rounded()), 1), 10)
        }
        rank = lenient(.rank)
        images = lenient(.images)
        badge = lenient(.badge)
        kind = lenient(.kind)
        updatedAt = lenient(.updatedAt)
        aiText = lenient(.aiText)
        eventId = lenient(.eventId)
        notification = lenient(.notification)
        if let links: [String: String] = lenient(.coverageLinks) {
            let found = links.compactMapValues { URL(string: $0) }.filter { $0.value.scheme?.hasPrefix("http") == true }
            coverageLinks = found.isEmpty ? nil : found
        }
    }
}

// MARK: What a card shows

extension Article {
    /// Techgist's summary when there is one, otherwise the publisher's excerpt.
    var displaySummary: String { summary ?? excerpt }

    /// True when the text on the card was written by Techgist's model.
    var isAISummary: Bool { summary != nil && aiText != false }

    /// The summary cut to its first two sentences, for cards where the whole summary doesn't fit.
    var shortSummary: String {
        if let s = summaryShort, !s.isEmpty { return s }
        return Self.firstSentences(2, of: displaySummary)
    }

    /// The title of an alert: the server's, else the headline on the card.
    var alertTitle: String {
        if let title = notification?.title, !title.isEmpty { return title }
        return displayTitle
    }

    /// The body of an alert: the server's (`notification.body`, or the older `alertLine`), else the summary's
    /// first sentence.
    var alertBody: String {
        if let body = notification?.body, !body.isEmpty { return body }
        if let line = alertLine, !line.isEmpty { return line }
        let first = Self.firstSentences(1, of: displaySummary)
        if !first.isEmpty { return first }
        let others = alsoCoveredBy?.count ?? 0
        return others > 0 ? "\(source) + \(others) more" : source
    }

    /// Outlets reporting the story, the lead included.
    var coverage: Int { 1 + (alsoCoveredBy?.count ?? 0) }

    /// `spice` on a 0 to 1 scale; 0 when the edition sent none.
    var spiceLevel: Double { Double(spice ?? 0) / 10 }

    var isBreaking: Bool { badge == "breaking" }
    var isDeveloping: Bool { badge == "developing" }
    var isHot: Bool { (spice ?? 0) >= 8 }

    /// A label for stories that aren't straight news, e.g. `ANALYSIS`.
    var kindLabel: String? {
        switch kind {
        case "analysis": return "ANALYSIS"
        case "opinion": return "OPINION"
        case "interview": return "INTERVIEW"
        case "release_notes": return "RELEASE"
        case "tutorial": return "TUTORIAL"
        default: return nil
        }
    }

    /// The picture for the card: the large copy on wide phones, the standard one otherwise.
    func cardImage(wide: Bool) -> URL? {
        if wide { return images?.hero ?? images?.std ?? image }
        return images?.std ?? image ?? images?.hero
    }

    /// The picture for lists and the compact card.
    var thumbImage: URL? { images?.thumb ?? images?.std ?? image }

    /// The first `count` sentences of `text`. A full stop inside a number or an abbreviation doesn't end a sentence.
    static func firstSentences(_ count: Int, of text: String) -> String {
        let chars = Array(text)
        var ends = 0
        var i = 0
        while i < chars.count {
            let ch = chars[i]
            if ch == "." || ch == "!" || ch == "?" {
                var j = i + 1
                while j < chars.count, "\"”’')".contains(chars[j]) { j += 1 }
                let atEnd = j >= chars.count
                let nextIsSpace = !atEnd && chars[j] == " "
                let nextStartsSentence = j + 1 < chars.count && (chars[j + 1].isUppercase || chars[j + 1].isNumber || "\"“‘".contains(chars[j + 1]))
                // "U.S. regulators", "Inc. said", "v2.5": a short token before the stop is an abbreviation.
                var k = i - 1
                var token = 0
                var dotted = false
                while k >= 0, chars[k] != " " {
                    if chars[k] == "." { dotted = true }
                    token += 1
                    k -= 1
                }
                let abbreviation = ch == "." && (token <= 2 || dotted) && !atEnd
                if atEnd || (nextIsSpace && nextStartsSentence && !abbreviation) {
                    ends += 1
                    if ends == count { return String(chars[..<j]) }
                }
            }
            i += 1
        }
        return text
    }
}

struct ArticleCache: Codable {
    var updatedAt: Date
    var articles: [Article]
    /// When the server published these stories. Nil when they came straight from the feeds, or from a 1.x cache.
    var editionPublishedAt: Date?
    var degraded: Bool?

    static let fileName = "articles.json"
    static func load() -> ArticleCache? { Disk.load(ArticleCache.self, from: fileName, in: .cachesDirectory) }
    static func save(_ articles: [Article], at date: Date, origin: NewsOrigin? = nil) {
        var cache = ArticleCache(updatedAt: date, articles: articles)
        if case .edition(let publishedAt, let degraded)? = origin {
            cache.editionPublishedAt = publishedAt
            cache.degraded = degraded
        }
        Disk.save(cache, to: fileName, in: .cachesDirectory)
    }

    /// Where the cached stories came from, when that is known.
    var origin: NewsOrigin? {
        editionPublishedAt.map { .edition(publishedAt: $0, degraded: degraded ?? false) }
    }
}

enum Keys {
    static let disabledSources = "disabledSources"
    static let digestEnabled = "digestEnabled"
    static let digestMinute = "digestMinuteOfDay"
    static let breakingNews = "breakingNewsEnabled"
    static let readerMode = "readerModeEnabled"
    static let notificationsOffered = "notificationsOffered"
    /// Set once the reader has chosen big story alerts knowing they come from Techgist's server.
    static let serverAlertsAnswered = "serverAlertsAnswered"
}
