import Foundation

/// Decides which stories a tab shows and in what order. Pure functions, so the rules are unit-tested.
enum FeedOrdering {
    /// The stories for one tab.
    /// 1. Muted sources are removed.
    /// 2. Top holds every story with a `topRank`, in that order. Other tabs hold their own category, ordered by the
    ///    edition's `rank` when it sends one. Without a rank (the v1 edition's other tabs, cached 1.x stories, the
    ///    direct-feed fallback) a tab is newest first.
    /// 3. Stories already read sink below unread ones, keeping their order.
    static func stories(in category: Category, from articles: [Article],
                        muted: Set<String> = [], read: Set<String> = []) -> [Article] {
        let enabled = muted.isEmpty ? articles : articles.filter { !muted.contains($0.source) }
        let ordered: [Article]
        if category == .top, enabled.contains(where: { $0.topRank != nil }) {
            ordered = sorted(enabled.filter { $0.topRank != nil }, by: \.topRank)
        } else {
            ordered = sorted(enabled.filter { $0.category == category }, by: \.rank)
        }
        guard !read.isEmpty else { return ordered }
        return ordered.filter { !read.contains($0.id) } + ordered.filter { read.contains($0.id) }
    }

    /// Ranked stories first, in rank order; unranked ones after them, newest first. Ties go to the newer story.
    private static func sorted(_ articles: [Article], by rank: KeyPath<Article, Int?>) -> [Article] {
        articles.sorted { a, b in
            switch (a[keyPath: rank], b[keyPath: rank]) {
            case let (x?, y?) where x != y: return x < y
            case (_?, nil): return true
            case (nil, _?): return false
            default: return a.publishedAt > b.publishedAt
            }
        }
    }

    /// The single biggest story right now, for the morning digest.
    static func topStory(in articles: [Article], muted: Set<String> = []) -> Article? {
        stories(in: .top, from: articles, muted: muted).first
    }

    /// How many of the first `limit` stories in `fresh` are not in `current`.
    static func newCount(in fresh: [Article], notIn current: [Article], limit: Int = 10) -> Int {
        let known = Set(current.map(\.id))
        return fresh.prefix(limit).filter { !known.contains($0.id) }.count
    }
}
