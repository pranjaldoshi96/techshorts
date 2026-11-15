import Foundation
import SwiftUI

@MainActor
final class NewsStore: ObservableObject {
    /// Everything in the current edition (or the direct-feed fallback).
    @Published private(set) var articles: [Article]
    /// What each tab shows, in order. Rebuilt only when a new edition is applied, never while someone is reading,
    /// so the list doesn't re-order under a finger.
    @Published private(set) var feeds: [Category: [Article]] = [:]
    @Published private(set) var lastUpdated: Date?
    @Published private(set) var isRefreshing = false
    @Published private(set) var failedSources: [String] = []
    @Published private(set) var origin: NewsOrigin?
    /// True when the last attempt to load stories got nothing back (usually no connection).
    @Published private(set) var isOffline = false
    @Published private(set) var saved: [Article]
    @Published var category: Category = .top {
        didSet { if category != oldValue { updateWaitingCount() } }
    }
    /// How many unseen stories the waiting edition has near the top of the current tab; the pill shows this.
    @Published private(set) var waitingCount = 0
    /// Bumped when the feed should jump back to the first story.
    @Published private(set) var scrollToTopRequests = 0
    /// A story to bring on screen and highlight, set when an alert or a search result is tapped.
    @Published var focus: Focus?

    @Published var disabledSources: Set<String> {
        didSet {
            UserDefaults.standard.set(Array(disabledSources), forKey: Keys.disabledSources)
            if disabledSources != oldValue { rebuildFeeds() }
        }
    }

    struct Focus: Equatable {
        let id: String
        let category: Category
        var token = UUID()
    }

    /// A newer set of stories found while reading; shown when the reader taps the "new stories" pill.
    private var waiting: (articles: [Article], origin: NewsOrigin?)?
    /// Set by the feed: true while the reader is still on the first card of the current tab.
    var readerIsAtTop = true
    private var backgroundedAt: Date?
    private var isChecking = false
    private let readState: ReadState
    typealias Progress = @Sendable ([Article]) async -> Void
    typealias Service = (Progress?) async -> (articles: [Article], failed: [String], origin: NewsOrigin)
    private let service: Service

    private nonisolated static let savedFile = "saved.json"
    private static let staleAfter: TimeInterval = 10 * 60
    nonisolated static let savedChanged = Notification.Name("TechgistSavedChanged")

    init(readState: ReadState? = nil,
         cache: ArticleCache? = ArticleCache.load(),
         service: Service? = nil) {
        self.readState = readState ?? .shared
        self.service = service ?? { await FeedService().latest(onProgress: $0) }
        articles = (cache?.articles ?? []).map(Self.slim)
        lastUpdated = cache?.updatedAt
        origin = cache?.origin
        saved = Self.loadSaved()
        disabledSources = Set(UserDefaults.standard.stringArray(forKey: Keys.disabledSources) ?? [])
        rebuildFeeds()
        NotificationCenter.default.addObserver(forName: Self.savedChanged, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.saved = Self.loadSaved() }
        }
    }

    var visibleArticles: [Article] { feeds[category] ?? [] }

    func articles(in category: Category) -> [Article] { feeds[category] ?? [] }

    /// The biggest story right now (for the daily digest and the alert preview), not just the newest.
    var topStory: Article? { FeedOrdering.topStory(in: articles, muted: disabledSources) }

    /// Re-applies the ordering rules: rank, muted sources, unread first.
    private func rebuildFeeds() {
        let read = readState.ids
        var next: [Category: [Article]] = [:]
        for category in Category.allCases {
            next[category] = FeedOrdering.stories(in: category, from: articles, muted: disabledSources, read: read)
        }
        feeds = next
    }

    // MARK: Loading

    /// Called on launch and whenever the app comes to the front.
    func becameActive() async {
        // A background refresh may have written a newer cache while we were suspended.
        if let cache = ArticleCache.load(), cache.updatedAt > (lastUpdated ?? .distantPast) {
            waiting = (cache.articles.map(Self.slim), cache.origin ?? origin)
            lastUpdated = cache.updatedAt
        }
        let awayLong = backgroundedAt.map { Date().timeIntervalSince($0) >= Self.staleAfter } ?? false
        backgroundedAt = nil

        if articles.isEmpty {
            await refresh()
        } else if Date().timeIntervalSince(lastUpdated ?? .distantPast) > Self.staleAfter || waiting != nil {
            await checkForNewStories()
            // Nobody is mid-read right after a launch or a long break, so the newer stories can simply appear.
            if waiting != nil, awayLong || readerIsAtTop { showWaitingStories() }
        } else if awayLong {
            rebuildFeeds()
            scrollToTopRequests += 1
        }
    }

    func enteredBackground() {
        backgroundedAt = Date()
        persist()
        readState.persist()
    }

    /// Pull to refresh, the "Try again" button and first launch: fetch and show the result straight away.
    func refresh() async {
        guard !isRefreshing else { return }
        isRefreshing = true
        defer { isRefreshing = false }

        // With nothing to show yet (first launch, and the edition unavailable), stories appear as each feed
        // answers instead of after the slowest one. Otherwise the full set is swapped in at once.
        var onProgress: Progress?
        if articles.isEmpty {
            onProgress = { [weak self] partial in await self?.showPartial(partial) }
        }
        let result = await service(onProgress)
        failedSources = result.failed
        guard !result.articles.isEmpty else {
            isOffline = true
            return
        }
        isOffline = false
        apply(result.articles.map(Self.slim), origin: result.origin)
    }

    /// Looks for newer stories without disturbing the reader; `showWaitingStories()` brings them in.
    func checkForNewStories() async {
        guard !isRefreshing, !isChecking, !articles.isEmpty else { return }
        isChecking = true
        defer { isChecking = false }
        let result = await service(nil)
        guard !result.articles.isEmpty else {
            isOffline = true
            return
        }
        isOffline = false
        let fresh = result.articles.map(Self.slim)
        if fresh == articles {
            origin = result.origin
            lastUpdated = Date()
            return
        }
        waiting = (fresh, result.origin)
        updateWaitingCount()
    }

    func showWaitingStories() {
        guard let waiting else { return }
        apply(waiting.articles, origin: waiting.origin)
        scrollToTopRequests += 1
    }

    private func apply(_ fresh: [Article], origin newOrigin: NewsOrigin?) {
        articles = fresh
        origin = newOrigin
        lastUpdated = Date()
        waiting = nil
        waitingCount = 0
        rebuildFeeds()
        persist()
        // These stories are on screen now, so they shouldn't alert later.
        BreakingNews.markSeen(articles)
        Task { await DailyDigest.reschedule(with: topStory) }
    }

    private func showPartial(_ partial: [Article]) {
        // Only while the first load is still running: later results must not shuffle cards someone is reading.
        guard isRefreshing, lastUpdated == nil, readerIsAtTop else { return }
        articles = partial.map(Self.slim)
        origin = .direct
        rebuildFeeds()
    }

    private func updateWaitingCount() {
        guard let waiting else { waitingCount = 0; return }
        let fresh = FeedOrdering.stories(in: category, from: waiting.articles, muted: disabledSources, read: readState.ids)
        waitingCount = FeedOrdering.newCount(in: fresh, notIn: feeds[category] ?? [])
    }

    func persist() {
        if let lastUpdated { ArticleCache.save(articles, at: lastUpdated, origin: origin) }
    }

    // MARK: Finding a story

    func article(withURL url: URL) -> Article? {
        let id = url.absoluteString
        return articles.first { $0.id == id } ?? waiting?.articles.first { $0.id == id } ?? saved.first { $0.id == id }
    }

    /// Brings a story on screen and highlights it. Returns false when the story isn't in any tab.
    @discardableResult
    func focus(on url: URL) -> Bool {
        let id = url.absoluteString
        if !articles.contains(where: { $0.id == id }), waiting?.articles.contains(where: { $0.id == id }) == true {
            showWaitingStories()
        }
        let tabs = [category] + Category.allCases
        guard let tab = tabs.first(where: { feeds[$0]?.contains { $0.id == id } == true }) else { return false }
        category = tab
        focus = Focus(id: id, category: tab)
        return true
    }

    // MARK: Saved

    private static func loadSaved() -> [Article] { Disk.load([Article].self, from: savedFile) ?? [] }

    func isSaved(_ article: Article) -> Bool { saved.contains { $0.id == article.id } }

    func toggleSaved(_ article: Article) {
        if let i = saved.firstIndex(where: { $0.id == article.id }) {
            saved.remove(at: i)
        } else {
            saved.insert(Self.slim(article), at: 0)
        }
        Disk.save(saved, to: Self.savedFile)
    }

    func clearSaved() {
        saved = []
        Disk.save(saved, to: Self.savedFile)
    }

    /// Saves a story without a running feed, for the Save button on a notification.
    nonisolated static func saveFromNotification(_ article: Article) {
        var saved = Disk.load([Article].self, from: savedFile) ?? []
        guard !saved.contains(where: { $0.id == article.id }) else { return }
        saved.insert(slim(article), at: 0)
        Disk.save(saved, to: savedFile)
        NotificationCenter.default.post(name: savedChanged, object: nil)
    }

    /// The card needs the summary, not the article body.
    private nonisolated static func slim(_ article: Article) -> Article {
        var copy = article
        copy.content = ""
        return copy
    }
}
