import XCTest
@testable import Techgist

/// Pull to refresh. SwiftUI cancels the task it runs `.refreshable` in within milliseconds (the view under the
/// spinner is rebuilt when `isRefreshing` changes), and a cancelled task cancels every network request it started.
/// These tests use a service that behaves like the network: slow, and empty-handed when its task is cancelled.
@MainActor
final class RefreshTests: XCTestCase {
    private var cacheBefore: Data?

    override func setUp() async throws {
        // A successful refresh writes the story cache; put back whatever the simulator had.
        cacheBefore = try? Data(contentsOf: Disk.url(ArticleCache.fileName, in: .cachesDirectory))
    }

    override func tearDown() async throws {
        let url = Disk.url(ArticleCache.fileName, in: .cachesDirectory)
        if let cacheBefore { try cacheBefore.write(to: url, options: .atomic) } else { try? FileManager.default.removeItem(at: url) }
    }

    private func story(_ name: String) -> Article {
        Article(url: URL(string: "https://example.com/\(name)")!, title: name, excerpt: "", content: "", source: "Wired",
                category: .top, publishedAt: Date(), rank: 0)
    }

    /// Answers after `delay`, or with nothing at all if the task it runs in is cancelled first, as URLSession does.
    private func networkLike(_ fresh: [Article], delay: Duration = .milliseconds(300)) -> NewsStore.Service {
        { _ in
            do { try await Task.sleep(for: delay) } catch { return ([], ["cancelled"], .direct) }
            return (fresh, [], .edition(publishedAt: Date(), degraded: false))
        }
    }

    private func store(cached: [Article], service: @escaping NewsStore.Service) -> NewsStore {
        NewsStore(readState: ReadState(readAt: [:]),
                  cache: ArticleCache(updatedAt: Date().addingTimeInterval(-600), articles: cached), service: service)
    }

    func testARefreshWhoseCallerIsCancelledStillLoads() async {
        let store = store(cached: [story("old")], service: networkLike([story("new")]))
        let pull = Task { await store.refresh() }
        // What SwiftUI does to the refreshable task about 15 ms in.
        try? await Task.sleep(for: .milliseconds(15))
        pull.cancel()
        await pull.value
        XCTAssertEqual(store.articles.map(\.title), ["new"])
        XCTAssertFalse(store.isOffline, "A cancelled caller is not a lost connection")
        XCTAssertFalse(store.isRefreshing)
        XCTAssertEqual(store.failedSources, [])
    }

    /// A read story sinks in the new order; the feed must not follow it down the tab.
    func testARefreshWithStoriesOnScreenGoesBackToTheTopAndPublishesNothingMeanwhile() async {
        let store = store(cached: [story("old")], service: networkLike([story("new")], delay: .milliseconds(100)))
        let before = store.scrollToTopRequests
        let pull = Task { await store.refresh() }
        try? await Task.sleep(for: .milliseconds(30))
        // Mid-fetch nothing is published, so the feed is not rebuilt under the pull.
        XCTAssertFalse(store.isRefreshing)
        XCTAssertEqual(store.articles.map(\.title), ["old"])
        await pull.value
        XCTAssertEqual(store.scrollToTopRequests, before + 1)
        XCTAssertEqual(store.refreshesCompleted, 1)
    }

    /// On a fast connection the stories still arrive no sooner than the pull's minimum, when the finger is up.
    func testAPullHoldsAFastResultUntilItsMinimum() async {
        let fresh = [story("new")]
        let store = store(cached: [story("old")], service: { _ in (fresh, [], .direct) })
        let pull = Task { await store.refresh(atLeast: .milliseconds(300)) }
        try? await Task.sleep(for: .milliseconds(100))
        XCTAssertEqual(store.articles.map(\.title), ["old"])
        await pull.value
        XCTAssertEqual(store.articles.map(\.title), ["new"])
    }

    func testAFirstLoadShowsItIsLoading() async {
        let store = NewsStore(readState: ReadState(readAt: [:]), cache: nil, service: networkLike([story("new")], delay: .milliseconds(100)))
        let load = Task { await store.refresh() }
        try? await Task.sleep(for: .milliseconds(30))
        XCTAssertTrue(store.isRefreshing)
        await load.value
        XCTAssertFalse(store.isRefreshing)
        XCTAssertEqual(store.articles.map(\.title), ["new"])
    }

    func testTwoRefreshesAtOnceFetchOnce() async {
        var calls = 0
        let fresh = [story("new")]
        let store = store(cached: [story("old")], service: { _ in
            calls += 1
            try? await Task.sleep(for: .milliseconds(100))
            return (fresh, [], .direct)
        })
        async let a: Void = store.refresh()
        async let b: Void = store.refresh()
        _ = await (a, b)
        XCTAssertEqual(calls, 1)
    }

    func testACheckForNewStoriesWhoseCallerIsCancelledStillFindsThem() async {
        let store = store(cached: [story("old")], service: networkLike([story("new")]))
        let check = Task { await store.checkForNewStories() }
        try? await Task.sleep(for: .milliseconds(15))
        check.cancel()
        await check.value
        XCTAssertFalse(store.isOffline)
        store.showWaitingStories()
        XCTAssertEqual(store.articles.map(\.title), ["new"])
    }

    func testARefreshThatReallyGetsNothingIsOffline() async {
        let store = store(cached: [story("old")], service: { _ in ([], ["Wired"], .direct) })
        await store.refresh()
        XCTAssertTrue(store.isOffline)
        XCTAssertEqual(store.articles.map(\.title), ["old"], "The stories on the phone stay")
        XCTAssertEqual(store.failedSources, ["Wired"])
    }

    func testOfflineClearsOnTheNextGoodRefresh() async {
        var online = false
        let fresh = [story("new")]
        let store = store(cached: [story("old")], service: { _ in online ? (fresh, [], .direct) : ([], [], .direct) })
        await store.refresh()
        XCTAssertTrue(store.isOffline)
        online = true
        await store.refresh()
        XCTAssertFalse(store.isOffline)
        XCTAssertEqual(store.articles.map(\.title), ["new"])
    }
}
