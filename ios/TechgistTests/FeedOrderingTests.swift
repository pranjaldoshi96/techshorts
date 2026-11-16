import XCTest
@testable import Techgist

final class FeedOrderingTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_800_000_000)

    private func story(_ name: String, _ category: Techgist.Category = .top, hoursAgo: Double, topRank: Int? = nil, rank: Int? = nil,
                       source: String = "Wired", others: [String]? = nil, badge: String? = nil, spice: Int? = nil) -> Article {
        Article(url: URL(string: "https://example.com/\(name)")!, title: name, excerpt: "", content: "", image: nil,
                source: source, category: category, publishedAt: now.addingTimeInterval(-hoursAgo * 3600),
                points: nil, alsoCoveredBy: others, topRank: topRank, headline: nil, summary: nil,
                spice: spice, rank: rank, badge: badge)
    }

    private func names(_ articles: [Article]) -> [String] { articles.map(\.title) }

    func testTopFollowsTheServersRankNotTheClock() {
        let all = [
            story("newest-but-third", hoursAgo: 0.1, topRank: 2),
            story("old-but-first", hoursAgo: 30, topRank: 0),
            story("ai-second", .ai, hoursAgo: 5, topRank: 1),
            story("ai-not-in-top", .ai, hoursAgo: 0.2),
        ]
        XCTAssertEqual(names(FeedOrdering.stories(in: .top, from: all)), ["old-but-first", "ai-second", "newest-but-third"])
    }

    func testPinnedStoriesWithNegativeRanksLead() {
        let all = [story("a", hoursAgo: 1, topRank: 0), story("pinned", hoursAgo: 9, topRank: -1)]
        XCTAssertEqual(names(FeedOrdering.stories(in: .top, from: all)), ["pinned", "a"])
    }

    func testTopFallsBackToNewestFirstWithoutRanks() {
        // Stories cached by 1.0, or straight from the feeds before ranking.
        let all = [story("older", hoursAgo: 4), story("newer", hoursAgo: 1), story("ai", .ai, hoursAgo: 0.5)]
        XCTAssertEqual(names(FeedOrdering.stories(in: .top, from: all)), ["newer", "older"])
    }

    func testOtherTabsAreNewestFirstWhenTheEditionSendsNoRank() {
        let all = [story("a", .ai, hoursAgo: 6, topRank: 0), story("b", .ai, hoursAgo: 2), story("c", .ai, hoursAgo: 4, topRank: 5),
                   story("dev", .dev, hoursAgo: 1)]
        XCTAssertEqual(names(FeedOrdering.stories(in: .ai, from: all)), ["b", "c", "a"])
    }

    func testOtherTabsFollowRankWhenTheEditionSendsOne() {
        let all = [story("third", .dev, hoursAgo: 0.5, rank: 2), story("first", .dev, hoursAgo: 20, rank: 0),
                   story("second", .dev, hoursAgo: 3, rank: 1)]
        XCTAssertEqual(names(FeedOrdering.stories(in: .dev, from: all)), ["first", "second", "third"])
    }

    func testUnrankedStoriesFollowRankedOnesNewestFirst() {
        let all = [story("unranked-old", .dev, hoursAgo: 9), story("ranked", .dev, hoursAgo: 20, rank: 0),
                   story("unranked-new", .dev, hoursAgo: 1)]
        XCTAssertEqual(names(FeedOrdering.stories(in: .dev, from: all)), ["ranked", "unranked-new", "unranked-old"])
    }

    func testEqualRanksGoToTheNewerStory() {
        let all = [story("older", hoursAgo: 5, topRank: 1), story("newer", hoursAgo: 1, topRank: 1)]
        XCTAssertEqual(names(FeedOrdering.stories(in: .top, from: all)), ["newer", "older"])
    }

    func testMutedSourcesAreRemoved() {
        let all = [story("a", hoursAgo: 1, topRank: 0, source: "Wired"), story("b", hoursAgo: 1, topRank: 1, source: "Engadget")]
        XCTAssertEqual(names(FeedOrdering.stories(in: .top, from: all, muted: ["Wired"])), ["b"])
        XCTAssertEqual(FeedOrdering.topStory(in: all, muted: ["Wired"])?.title, "b")
    }

    func testReadStoriesSinkButKeepTheirOrder() {
        let all = (0..<5).map { (i: Int) in story("s\(i)", hoursAgo: Double(i), topRank: i) }
        let read: Set<String> = [all[0].id, all[2].id]
        XCTAssertEqual(names(FeedOrdering.stories(in: .top, from: all, read: read)), ["s1", "s3", "s4", "s0", "s2"])
    }

    func testNewCountLooksOnlyAtTheTopOfTheTab() {
        let current = (0..<12).map { (i: Int) in story("s\(i)", hoursAgo: 1, topRank: i) }
        var fresh = current
        fresh.insert(story("new-top", hoursAgo: 0, topRank: -1), at: 0)
        fresh.append(story("new-deep", hoursAgo: 0, topRank: 99))
        XCTAssertEqual(FeedOrdering.newCount(in: fresh, notIn: current), 1)
        XCTAssertEqual(FeedOrdering.newCount(in: current, notIn: current), 0)
    }

    func testLiveEditionOrdering() throws {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "edition-v1-live", withExtension: "json"))
        let edition = try XCTUnwrap(Edition.decode(try Data(contentsOf: url)))
        let top = FeedOrdering.stories(in: .top, from: edition.articles)
        XCTAssertEqual(top.count, 111)
        XCTAssertEqual(top.map { $0.topRank! }, Array(0..<111))
        // The live Top is not in time order, so rank order is a real change from 1.2.
        XCTAssertNotEqual(top.map(\.id), top.sorted { $0.publishedAt > $1.publishedAt }.map(\.id))
        for tab in [Techgist.Category.ai, .startups, .dev, .blogs] {
            let stories = FeedOrdering.stories(in: tab, from: edition.articles)
            XCTAssertFalse(stories.isEmpty)
            XCTAssertTrue(stories.allSatisfy { $0.category == tab })
            XCTAssertEqual(stories.map(\.publishedAt), stories.map(\.publishedAt).sorted(by: >))
        }
    }

    // MARK: Alerts

    func testAlertCandidatesPreferBreakingThenSpiceThenRank() {
        let big = ["A", "B"]
        let all = [
            story("covered", hoursAgo: 1, topRank: 0, others: big),
            story("spicy", hoursAgo: 1, topRank: 3, others: big, spice: 9),
            story("breaking", hoursAgo: 1, topRank: 7, badge: "breaking"),
            story("thin", hoursAgo: 1, topRank: 1, others: ["A"]),
            story("stale", hoursAgo: 7, topRank: 2, others: big),
            story("seen", hoursAgo: 1, topRank: 4, others: big),
            story("not-top", .ai, hoursAgo: 1, others: big),
        ]
        let seen: Set<String> = ["https://example.com/seen"]
        XCTAssertEqual(names(BreakingNews.candidates(in: all, seen: seen, now: now)), ["breaking", "spicy", "covered"])
    }

    func testAlertBodyUsesTheAlertLineThenTheFirstSentence() {
        var a = story("a", hoursAgo: 1, others: ["BBC"])
        XCTAssertEqual(a.alertBody, "Wired + 1 more")
        a.summary = "First sentence. Second sentence."
        XCTAssertEqual(a.alertBody, "First sentence.")
        a.alertLine = "A line written for the alert."
        XCTAssertEqual(a.alertBody, "A line written for the alert.")
    }

    func testNotificationCarriesTheStoryForSave() throws {
        var a = story("a", hoursAgo: 1)
        a.summary = "S."
        a.content = String(repeating: "long body ", count: 2000)
        let info = StoryNotification.userInfo(for: a)
        XCTAssertEqual(StoryNotification.storyURL(from: info), a.url)
        let carried = try XCTUnwrap(StoryNotification.article(from: info))
        XCTAssertEqual(carried.id, a.id)
        XCTAssertEqual(carried.summary, "S.")
        XCTAssertEqual(carried.content, "")
    }

    func testNotificationAttachmentTypes() {
        XCTAssertEqual(StoryNotification.imageExtension(of: Data([0xFF, 0xD8, 0xFF, 0xE0])), "jpg")
        XCTAssertEqual(StoryNotification.imageExtension(of: Data([0x89, 0x50, 0x4E, 0x47])), "png")
        XCTAssertNil(StoryNotification.imageExtension(of: Data("RIFF".utf8)))   // WebP
    }

    func testTokensAreSentAsLowercaseHex() {
        XCTAssertEqual(PushRegistration.hex(Data([0x00, 0xAB, 0x10])), "00ab10")
    }

    // MARK: Time

    func testRelativeTime() {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "UTC")!
        calendar.locale = Locale(identifier: "en_US")
        let noon = calendar.date(from: DateComponents(year: 2026, month: 11, day: 16, hour: 12))!
        func short(_ secondsAgo: TimeInterval) -> String {
            RelativeTime.short(noon.addingTimeInterval(-secondsAgo), now: noon, calendar: calendar)
        }
        XCTAssertEqual(short(-500), "now")          // a publisher clock ahead of ours
        XCTAssertEqual(short(30), "now")
        XCTAssertEqual(short(12 * 60), "12m")
        XCTAssertEqual(short(3 * 3600 + 100), "3h")
        XCTAssertEqual(short(23 * 3600), "23h")
        XCTAssertEqual(short(30 * 3600), "Yesterday")
        XCTAssertEqual(short(12 * 86_400), "Nov 4")
    }

    func testUpdatedLabel() {
        var a = story("a", hoursAgo: 10)
        XCTAssertEqual(RelativeTime.label(for: a, now: now), "10h")
        a.updatedAt = a.publishedAt.addingTimeInterval(1800)
        XCTAssertEqual(RelativeTime.label(for: a, now: now), "10h")
        a.updatedAt = now.addingTimeInterval(-2 * 3600)
        XCTAssertEqual(RelativeTime.label(for: a, now: now), "Updated 2h")
    }
}
