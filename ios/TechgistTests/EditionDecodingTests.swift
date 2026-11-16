import XCTest
@testable import Techgist

/// The app must keep reading the v1 `edition.json` that 1.2 reads, and must also read editions that add 2.0 fields.
final class EditionDecodingTests: XCTestCase {
    /// `edition-v1-live.json` is the live edition as fetched on 16 Nov 2026 from
    /// https://raw.githubusercontent.com/pranjaldoshi96/techshorts/edition/edition.json, unchanged.
    private func liveData() throws -> Data {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "edition-v1-live", withExtension: "json"))
        return try Data(contentsOf: url)
    }

    /// The decoder 1.2 shipped: synthesized `Codable` over the v1 fields.
    private struct V1Article: Decodable, Equatable {
        let url: URL, title: String, excerpt: String, content: String
        let image: URL?
        let source: String, category: Techgist.Category, publishedAt: Date
        let points: Int?, alsoCoveredBy: [String]?, topRank: Int?, headline: String?, summary: String?
    }
    private struct V1Edition: Decodable {
        let publishedAt: Date
        let articles: [V1Article]
    }

    func testLiveV1EditionDecodesExactlyAs12Did() throws {
        let data = try liveData()
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let old = try decoder.decode(V1Edition.self, from: data)
        let new = try XCTUnwrap(Edition.decode(data))

        XCTAssertEqual(new.publishedAt, old.publishedAt)
        XCTAssertEqual(new.articles.count, old.articles.count)
        XCTAssertEqual(new.articles.count, 235)
        for (a, b) in zip(new.articles, old.articles) {
            let same = V1Article(url: a.url, title: a.title, excerpt: a.excerpt, content: a.content, image: a.image,
                                 source: a.source, category: a.category, publishedAt: a.publishedAt, points: a.points,
                                 alsoCoveredBy: a.alsoCoveredBy, topRank: a.topRank, headline: a.headline, summary: a.summary)
            XCTAssertEqual(same, b)
        }
    }

    func testLiveV1EditionHasNoneOfTheNewFields() throws {
        let edition = try XCTUnwrap(Edition.decode(try liveData()))
        XCTAssertNil(edition.degraded)
        for a in edition.articles {
            XCTAssertNil(a.summaryShort); XCTAssertNil(a.alertLine); XCTAssertNil(a.spice); XCTAssertNil(a.rank)
            XCTAssertNil(a.images); XCTAssertNil(a.badge); XCTAssertNil(a.kind); XCTAssertNil(a.updatedAt)
        }
        XCTAssertEqual(edition.articles.filter { $0.topRank != nil }.count, 111)
    }

    func testV1ArticleReencodesWithoutNewKeys() throws {
        // The server's safe-mode publisher encodes with this same type; nil fields must not appear in its output.
        let edition = try XCTUnwrap(Edition.decode(try liveData()))
        let json = try XCTUnwrap(String(data: try edition.encoded(), encoding: .utf8))
        for key in ["summaryShort", "alertLine", "spice", "rank", "images", "badge", "type", "updatedAt", "aiText", "eventId", "notification", "degraded"] {
            XCTAssertFalse(json.contains("\"\(key)\":"), key)
        }
        let again = try XCTUnwrap(Edition.decode(Data(json.utf8)))
        XCTAssertEqual(again.articles, edition.articles)
    }

    private func edition(_ articles: String, extra: String = "") -> Data {
        Data("{\"publishedAt\":\"2026-11-16T01:00:00Z\",\"articles\":[\(articles)]\(extra)}".utf8)
    }

    private let v1 = """
        {"url":"https://example.com/a","title":"Publisher title","excerpt":"Excerpt.","content":"Body",
         "source":"TechCrunch","category":"AI","publishedAt":"2026-11-16T00:30:00Z"}
        """

    func testNewOptionalFieldsDecode() throws {
        let article = """
            {"url":"https://example.com/b","title":"T","excerpt":"E","content":"","source":"The Verge","category":"Top",
             "publishedAt":"2026-11-15T20:00:00Z","topRank":0,"headline":"H","summary":"One. Two. Three.",
             "summaryShort":"One. Two.","alertLine":"New detail here.","spice":0.9,"rank":3,
             "images":{"hero":"https://example.com/h.webp","std":"https://example.com/s.jpg","thumb":"https://example.com/t.webp","generated":false},
             "badge":"breaking","type":"analysis","updatedAt":"2026-11-15T23:00:00Z","aiText":true,"eventId":"ev_1"}
            """
        let e = try XCTUnwrap(Edition.decode(edition(article + "," + v1, extra: ",\"degraded\":true")))
        XCTAssertEqual(e.degraded, true)
        XCTAssertEqual(e.articles.count, 2)
        let a = e.articles[0]
        XCTAssertEqual(a.summaryShort, "One. Two.")
        XCTAssertEqual(a.alertLine, "New detail here.")
        XCTAssertEqual(a.alertBody, "New detail here.")
        XCTAssertEqual(a.spice, 9)
        XCTAssertTrue(a.isHot)
        XCTAssertEqual(a.rank, 3)
        XCTAssertEqual(a.images?.hero?.absoluteString, "https://example.com/h.webp")
        XCTAssertEqual(a.cardImage(wide: true)?.absoluteString, "https://example.com/h.webp")
        XCTAssertEqual(a.cardImage(wide: false)?.absoluteString, "https://example.com/s.jpg")
        XCTAssertEqual(a.thumbImage?.absoluteString, "https://example.com/t.webp")
        XCTAssertTrue(a.isBreaking)
        XCTAssertEqual(a.kindLabel, "ANALYSIS")
        XCTAssertEqual(a.updatedAt, ISO8601DateFormatter().date(from: "2026-11-15T23:00:00Z"))
        XCTAssertEqual(a.aiText, true)
        XCTAssertEqual(a.eventId, "ev_1")
        // The plain v1 article beside it is untouched.
        XCTAssertNil(e.articles[1].spice)
        XCTAssertEqual(e.articles[1].shortSummary, "Excerpt.")
    }

    func testNotifLineIsAcceptedAsTheAlertLine() throws {
        let article = v1.replacingOccurrences(of: "\"source\"", with: "\"notifLine\":\"From the server.\",\"source\"")
        let e = try XCTUnwrap(Edition.decode(edition(article)))
        XCTAssertEqual(e.articles.first?.alertLine, "From the server.")
    }

    func testMistypedNewFieldsAreIgnoredNotFatal() throws {
        let article = v1.replacingOccurrences(
            of: "\"source\"",
            with: "\"spice\":\"very\",\"rank\":\"first\",\"images\":\"x.jpg\",\"badge\":7,\"updatedAt\":12,\"aiText\":\"no\",\"alertLine\":[1],\"source\"")
        let e = try XCTUnwrap(Edition.decode(edition(article, extra: ",\"degraded\":\"maybe\"")))
        let a = try XCTUnwrap(e.articles.first)
        XCTAssertNil(a.spice); XCTAssertNil(a.rank); XCTAssertNil(a.images); XCTAssertNil(a.badge)
        XCTAssertNil(a.updatedAt); XCTAssertNil(a.aiText); XCTAssertNil(a.alertLine)
        XCTAssertNil(e.degraded)
        XCTAssertEqual(a.source, "TechCrunch")
    }

    func testSpiceOnATenPointScaleIsScaled() throws {
        let article = v1.replacingOccurrences(of: "\"source\"", with: "\"spice\":8,\"source\"")
        let a = try XCTUnwrap(Edition.decode(edition(article))?.articles.first)
        XCTAssertEqual(a.spiceLevel, 0.8, accuracy: 0.0001)
        XCTAssertTrue(a.isHot)
    }

    func testSpiceOfOneIsTheBottomOfTheScaleNotTheTop() throws {
        let article = v1.replacingOccurrences(of: "\"source\"", with: "\"spice\":1,\"source\"")
        let a = try XCTUnwrap(Edition.decode(edition(article))?.articles.first)
        XCTAssertEqual(a.spice, 1)
        XCTAssertFalse(a.isHot)
    }

    // MARK: The engine's edition (EDITION FIELD CONTRACT)

    /// `edition-v2-engine.json` is the edition the new engine published on 16 Nov 2026
    /// (/var/lib/techgist/edition/edition.json on the server), unchanged.
    private func engineEdition() throws -> Edition {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "edition-v2-engine", withExtension: "json"))
        return try XCTUnwrap(Edition.decode(try Data(contentsOf: url)))
    }

    func testEngineEditionDecodesEveryArticleWithItsNewFields() throws {
        let e = try engineEdition()
        XCTAssertEqual(e.articles.count, 126)
        XCTAssertEqual(e.degraded, false)
        for a in e.articles {
            XCTAssertNotNil(a.eventId, a.id)
            XCTAssertTrue((1...10).contains(try XCTUnwrap(a.spice)), a.id)
            XCTAssertNotNil(a.image, a.id)
            XCTAssertEqual(a.aiText, true)
            XCTAssertFalse(a.alertTitle.isEmpty); XCTAssertFalse(a.alertBody.isEmpty)
        }
        let withAlert = e.articles.filter { $0.notification != nil }
        XCTAssertEqual(withAlert.count, 125)
        for a in withAlert {
            let text = try XCTUnwrap(a.notification)
            XCTAssertLessThanOrEqual(try XCTUnwrap(text.title).count, 40, a.id)
            XCTAssertLessThanOrEqual(try XCTUnwrap(text.body).count, 110, a.id)
            XCTAssertEqual(a.alertTitle, text.title); XCTAssertEqual(a.alertBody, text.body)
        }
        let lead = try XCTUnwrap(e.articles.first { $0.topRank == 0 })
        XCTAssertEqual(lead.eventId, "ev_553277a985")
        XCTAssertEqual(lead.spice, 8)
        XCTAssertTrue(lead.isHot)
        XCTAssertEqual(lead.notification?.title, "Huang weighs return as Nvidia CEO")
        // The one story without alert text falls back to its headline and first sentence.
        let plain = try XCTUnwrap(e.articles.first { $0.notification == nil })
        XCTAssertEqual(plain.alertTitle, plain.displayTitle)
    }

    func testEngineEditionFillsEveryTabAndTopFollowsTopRank() throws {
        let e = try engineEdition()
        for tab in Techgist.Category.allCases {
            XCTAssertGreaterThanOrEqual(FeedOrdering.stories(in: tab, from: e.articles).count, 10, tab.rawValue)
        }
        let top = FeedOrdering.stories(in: .top, from: e.articles)
        XCTAssertEqual(top.compactMap(\.topRank), Array(0..<top.count))
    }

    func testEngineEditionSurvivesTheCacheRoundTrip() throws {
        let e = try engineEdition()
        let again = try XCTUnwrap(Edition.decode(try e.encoded()))
        XCTAssertEqual(again.articles, e.articles)
        // The app's own cache uses the default coding; alert text, spice and the event id must survive it too.
        let cached = try JSONDecoder().decode([Article].self, from: try JSONEncoder().encode(e.articles))
        XCTAssertEqual(cached, e.articles)
    }

    func testRankOrdersATabAndTheAlertTextComesFromNotification() throws {
        func story(_ name: String, rank: Int, minutesOld: Int) -> String {
            v1.replacingOccurrences(of: "/a\"", with: "/\(name)\"")
                .replacingOccurrences(of: "00:30:00Z", with: String(format: "00:%02d:00Z", 59 - minutesOld))
                .replacingOccurrences(of: "\"source\"", with: "\"rank\":\(rank),\"spice\":7,\"eventId\":\"ev_\(name)\",\"alertLine\":\"Older line.\",\"notification\":{\"title\":\"Alert \(name)\",\"body\":\"Body \(name).\"},\"source\"")
        }
        // The newest story has the worst rank; the rank wins.
        let json = [story("newest", rank: 2, minutesOld: 1), story("oldest", rank: 0, minutesOld: 50), story("middle", rank: 1, minutesOld: 20)]
        let e = try XCTUnwrap(Edition.decode(edition(json.joined(separator: ","))))
        let ai = FeedOrdering.stories(in: .ai, from: e.articles)
        XCTAssertEqual(ai.map(\.eventId), ["ev_oldest", "ev_middle", "ev_newest"])
        XCTAssertEqual(ai[0].alertTitle, "Alert oldest")
        XCTAssertEqual(ai[0].alertBody, "Body oldest.")
        XCTAssertEqual(ai[0].spice, 7)
    }

    func testMistypedNotificationIsIgnored() throws {
        let article = v1.replacingOccurrences(of: "\"source\"", with: "\"notification\":\"hello\",\"source\"")
        let a = try XCTUnwrap(Edition.decode(edition(article))?.articles.first)
        XCTAssertNil(a.notification)
        XCTAssertEqual(a.alertTitle, "Publisher title")
        XCTAssertEqual(a.alertBody, "Excerpt.")
    }

    func testAnArticleThatDoesNotDecodeIsDroppedAndTheRestKept() throws {
        let unknown = v1.replacingOccurrences(of: "\"AI\"", with: "\"India\"").replacingOccurrences(of: "/a\"", with: "/c\"")
        let e = try XCTUnwrap(Edition.decode(edition(unknown + "," + v1)))
        XCTAssertEqual(e.articles.map(\.url.absoluteString), ["https://example.com/a"])
        // With nothing left the edition isn't fresh, so the app falls back to the feeds as 1.2 does.
        let none = try XCTUnwrap(Edition.decode(edition(unknown)))
        XCTAssertFalse(none.isFresh(at: none.publishedAt))
    }

    func testAMissingRequiredV1FieldStillFailsThatArticle() throws {
        let broken = v1.replacingOccurrences(of: "\"excerpt\":\"Excerpt.\",", with: "")
        XCTAssertEqual(Edition.decode(edition(broken))?.articles.count, 0)
        XCTAssertNil(Edition.decode(Data("{\"articles\":[]}".utf8)))
        XCTAssertNil(Edition.decode(Data("not json".utf8)))
    }

    func testEditionIsFreshForEightHours() throws {
        let e = try XCTUnwrap(Edition.decode(edition(v1)))
        XCTAssertTrue(e.isFresh(at: e.publishedAt.addingTimeInterval(8 * 3600 - 1)))
        XCTAssertFalse(e.isFresh(at: e.publishedAt.addingTimeInterval(8 * 3600 + 1)))
    }

    func testCachedStoriesFrom12StillLoad() throws {
        // 1.2 wrote its cache and Saved list with the default date strategy and only the v1 keys.
        let json = """
            {"updatedAt":784000000,"articles":[{"url":"https://example.com/a","title":"T","excerpt":"E","content":"C",
            "source":"Wired","category":"Dev","publishedAt":783990000,"topRank":4}]}
            """
        let cache = try JSONDecoder().decode(ArticleCache.self, from: Data(json.utf8))
        XCTAssertEqual(cache.articles.first?.topRank, 4)
        XCTAssertEqual(cache.articles.first?.category, .dev)
    }

    func testShortSummaryKeepsWholeSentences() {
        let text = "OpenAI raised $6.6B at a $157B valuation. The U.S. round was led by Thrive Capital. It plans to spend the money on chips."
        XCTAssertEqual(Article.firstSentences(2, of: text),
                       "OpenAI raised $6.6B at a $157B valuation. The U.S. round was led by Thrive Capital.")
        XCTAssertEqual(Article.firstSentences(1, of: text), "OpenAI raised $6.6B at a $157B valuation.")
        XCTAssertEqual(Article.firstSentences(2, of: "Only one sentence here."), "Only one sentence here.")
        XCTAssertEqual(Article.firstSentences(2, of: ""), "")
    }
}
