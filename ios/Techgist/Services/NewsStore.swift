import Foundation
import SwiftUI

@MainActor
final class NewsStore: ObservableObject {
    @Published private(set) var articles: [Article]
    @Published private(set) var lastUpdated: Date?
    @Published private(set) var isRefreshing = false
    @Published private(set) var failedSources: [String] = []
    @Published private(set) var origin: NewsOrigin?
    @Published private(set) var aiSummaries: [String: String] = [:]
    @Published private(set) var saved: [Article]
    @Published var category: Category = .top
    /// A newer set of stories found while reading; shown only when the reader taps the "new stories" button.
    @Published private(set) var waiting: (articles: [Article], origin: NewsOrigin, newCount: Int)?
    /// Bumped when the feed should jump back to the newest story.
    @Published private(set) var scrollToTopRequests = 0

    @Published var disabledSources: Set<String> {
        didSet { UserDefaults.standard.set(Array(disabledSources), forKey: Keys.disabledSources) }
    }
    @Published var aiSummariesEnabled: Bool {
        didSet { UserDefaults.standard.set(aiSummariesEnabled, forKey: Keys.aiSummaries) }
    }

    private static let savedFile = "saved.json"
    private static let staleAfter: TimeInterval = 15 * 60

    init() {
        let cache = ArticleCache.load()
        articles = cache?.articles ?? []
        lastUpdated = cache?.updatedAt
        saved = Disk.load([Article].self, from: Self.savedFile) ?? []
        disabledSources = Set(UserDefaults.standard.stringArray(forKey: Keys.disabledSources) ?? [])
        aiSummariesEnabled = UserDefaults.standard.object(forKey: Keys.aiSummaries) as? Bool ?? true
    }

    var visibleArticles: [Article] { articles(in: category) }

    /// Every tab shows the newest story first. Top holds Top sources plus the strongest stories from every tab
    /// (`topRank`); stories cached by older versions have no rank, so Top falls back to its own sources.
    func articles(in category: Category) -> [Article] {
        let enabled = articles.filter { !disabledSources.contains($0.source) }
        let inTab = category == .top && enabled.contains(where: { $0.topRank != nil })
            ? enabled.filter { $0.topRank != nil }
            : enabled.filter { $0.category == category }
        return inTab.sorted { $0.publishedAt > $1.publishedAt }
    }

    /// The biggest story right now (for the daily digest), not just the newest.
    var topStory: Article? {
        articles.filter { $0.topRank != nil && !disabledSources.contains($0.source) }.min { $0.topRank! < $1.topRank! }
            ?? articles(in: .top).first
    }

    /// Techgist's summary first, then one written on this iPhone, then the publisher's excerpt.
    func summary(for article: Article) -> (text: String, isAI: Bool) {
        if let s = article.summary { return (s, true) }
        if aiSummariesEnabled, let s = aiSummaries[article.id] { return (s, true) }
        return (article.excerpt, false)
    }

    // MARK: Loading

    func refreshIfStale() async {
        // A background refresh may have written a newer cache while we were suspended.
        if let cache = ArticleCache.load(), cache.updatedAt > (lastUpdated ?? .distantPast) {
            articles = cache.articles
            lastUpdated = cache.updatedAt
        }
        guard Date().timeIntervalSince(lastUpdated ?? .distantPast) > Self.staleAfter else { return }
        await refresh()
    }

    func refresh() async {
        guard !isRefreshing else { return }
        isRefreshing = true
        defer { isRefreshing = false }

        // With nothing cached (first launch), show stories as feeds arrive. Otherwise swap in the full set at once
        // so cards don't shift under someone who is reading.
        var onProgress: (@Sendable ([Article]) async -> Void)?
        if articles.isEmpty {
            onProgress = { [weak self] partial in await self?.showPartial(partial) }
        }
        let result = await FeedService().latest(onProgress: onProgress)
        failedSources = result.failed
        guard !result.articles.isEmpty else { return }
        origin = result.origin

        replaceArticles(with: result.articles)
        lastUpdated = Date()
        waiting = nil
        persist()
        // These stories are on screen now, so they shouldn't alert later.
        BreakingNews.markSeen(articles)
        await DailyDigest.reschedule(with: topStory)
    }

    /// Fills in a missing image/excerpt from the article page and generates the on-device summary.
    func prepare(_ article: Article) async {
        if article.image == nil || article.excerpt.isEmpty, let meta = await PageMetadata.shared.meta(for: article.url) {
            update(article.id) {
                if $0.image == nil { $0.image = meta.image }
                if $0.excerpt.isEmpty, let d = meta.description { $0.excerpt = d }
            }
        }
        if article.summary == nil, aiSummariesEnabled, aiSummaries[article.id] == nil,
           let s = await Summarizer.shared.summary(for: article) {
            aiSummaries[article.id] = s
        }
    }

    /// Looks for newer stories without disturbing the reader; `showWaitingStories()` brings them in.
    func checkForNewStories() async {
        guard !isRefreshing, !articles.isEmpty else { return }
        let result = await FeedService().latest()
        guard !result.articles.isEmpty else { return }
        let current = Set(articles.map(\.id))
        let newCount = result.articles.filter { !current.contains($0.id) && !disabledSources.contains($0.source) }.count
        waiting = newCount > 0 ? (result.articles, result.origin, newCount) : nil
    }

    func showWaitingStories() {
        guard let waiting else { return }
        replaceArticles(with: waiting.articles)
        origin = waiting.origin
        lastUpdated = Date()
        self.waiting = nil
        persist()
        BreakingNews.markSeen(articles)
        scrollToTopRequests += 1
        Task { await DailyDigest.reschedule(with: topStory) }
    }

    private func showPartial(_ partial: [Article]) {
        replaceArticles(with: partial)
    }

    /// Swaps in fresh feed results while keeping images and excerpts already fetched from article pages.
    private func replaceArticles(with fresh: [Article]) {
        let previous = Dictionary(articles.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        articles = fresh.map { article in
            var a = article
            if let old = previous[a.id] {
                if a.image == nil { a.image = old.image }
                if a.excerpt.isEmpty { a.excerpt = old.excerpt }
            }
            return a
        }
    }

    func persist() {
        if let lastUpdated { ArticleCache.save(articles, at: lastUpdated) }
    }

    private func update(_ id: String, _ change: (inout Article) -> Void) {
        guard let i = articles.firstIndex(where: { $0.id == id }) else { return }
        change(&articles[i])
    }

    // MARK: Saved

    func isSaved(_ article: Article) -> Bool { saved.contains { $0.id == article.id } }

    func toggleSaved(_ article: Article) {
        if let i = saved.firstIndex(where: { $0.id == article.id }) {
            saved.remove(at: i)
        } else {
            var copy = article
            if article.summary == nil, let s = aiSummaries[article.id] { copy.excerpt = s }
            saved.insert(copy, at: 0)
        }
        Disk.save(saved, to: Self.savedFile)
    }
}
