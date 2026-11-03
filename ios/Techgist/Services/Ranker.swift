import Foundation

/// Orders stories by importance rather than time alone:
/// - **Coverage**: a story reported by several publishers (matched by headline) ranks higher.
/// - **Hacker News points** for stories on the HN front page.
/// - **Source weight**: high-volume community feeds count for a little less.
/// - **Age**: scores decay with time, so today's news still leads.
/// Same-story duplicates within a category collapse into one card that notes the other outlets.
enum Ranker {
    static func rank(_ articles: [Article], now: Date = Date()) -> [Article] {
        let tokens = articles.map { titleTokens($0.title) }
        let idf = inverseDocumentFrequency(tokens)
        let groups = clusters(articles, tokens, idf)

        var ranked: [Category: [Scored]] = [:]
        for (groupIndex, group) in groups.enumerated() {
            let members = group.map { articles[$0] }
            let sourceNames = members.flatMap { [$0.source] + ($0.alsoCoveredBy ?? []) }
            let points = members.compactMap(\.points).max()

            for category in Category.allCases {
                let inCategory = members.filter { $0.category == category }
                // Blog posts on the same topic are different opinions, not duplicates, so keep them all.
                let leads = category == .blogs ? inCategory : inCategory.max(by: { quality($0) < quality($1) }).map { [$0] } ?? []
                for var lead in leads {
                    let others = otherPublishers(sourceNames, excluding: lead.source)
                    lead.alsoCoveredBy = others.isEmpty ? nil : others
                    lead.points = points
                    lead.topRank = nil
                    ranked[category, default: []].append(Scored(article: lead, score: score(lead, coverage: others.count + 1, now: now), group: groupIndex))
                }
            }
        }

        // The Top tab: Top sources plus the strongest stories from every other tab, one card per story.
        var topPicks = ranked[.top] ?? []
        var topGroups = Set(topPicks.map(\.group))
        for category in Category.allCases where category != .top {
            let best = (ranked[category] ?? []).sorted { $0.score > $1.score }
            for pick in best.prefix(topPicksPerTab[category] ?? 0) where topGroups.insert(pick.group).inserted {
                topPicks.append(pick)
            }
        }
        let topOrder = Dictionary(spreadSources(topPicks).enumerated().map { ($1.id, $0) }, uniquingKeysWith: min)

        return Category.allCases.flatMap { category in
            spreadSources(ranked[category] ?? []).map { article in
                var a = article
                a.topRank = topOrder[a.id]
                return a
            }
        }
    }

    private struct Scored {
        var article: Article
        var score: Double
        var group: Int
    }

    /// How many of each tab's best stories also appear in Top.
    private static let topPicksPerTab: [Category: Int] = [.ai: 6, .startups: 4, .dev: 3, .blogs: 3]

    static func score(_ article: Article, coverage: Int, now: Date) -> Double {
        let ageHours = max(0, now.timeIntervalSince(article.publishedAt) / 3600)
        let coverageBoost = 1 + 0.8 * Double(min(coverage - 1, 3))
        let pointsBoost = article.points.map { 1 + min(1, max(0, log2(Double($0) / 150)) * 0.5) } ?? 1
        // Half the score after 12 hours, a third after a day: fresh enough for a daily app without burying big stories.
        return FeedSource.weight(of: article.source) * coverageBoost * pointsBoost / (ageHours + 12)
    }

    /// Picks which copy of a story to show: trusted source, then one with an image and text, then the earliest report.
    private static func quality(_ a: Article) -> Double {
        FeedSource.weight(of: a.source) + (a.image != nil ? 0.2 : 0) + (a.excerpt.isEmpty ? 0 : 0.2)
            - a.publishedAt.timeIntervalSinceReferenceDate / 1e12
    }

    /// One source name per publisher other than the lead's (TechCrunch and TechCrunch Startups are one publisher).
    private static func otherPublishers(_ names: [String], excluding lead: String) -> [String] {
        var seen: Set<String> = [FeedSource.publisher(of: lead)]
        return names.filter { seen.insert(FeedSource.publisher(of: $0)).inserted }
    }

    /// Orders by score, but each further story from a source already shown counts for 25% less,
    /// so one prolific feed can't fill the top of a tab.
    private static func spreadSources(_ scored: [Scored]) -> [Article] {
        var pending = scored
        var shown: [String: Int] = [:]
        var out: [Article] = []
        while !pending.isEmpty {
            let adjusted = { (i: Int) in pending[i].score * pow(0.75, Double(shown[pending[i].article.source] ?? 0)) }
            let best = pending.indices.max { adjusted($0) < adjusted($1) }!
            let article = pending.remove(at: best).article
            shown[article.source, default: 0] += 1
            out.append(article)
        }
        return out
    }

    // MARK: Matching headlines

    private static let stopwords: Set<String> = [
        "the", "and", "for", "with", "from", "that", "this", "its", "are", "was", "has", "have", "how", "why", "what",
        "who", "when", "where", "which", "will", "into", "about", "after", "over", "now", "new", "more", "can", "you", "your", "our", "says",
        "say", "said", "just", "out", "not", "but", "all", "get", "gets", "here", "than", "they", "their", "his", "her",
        "report", "reportedly", "launches", "announces", "update", "week", "today", "first", "could", "would",
    ]

    static func titleTokens(_ title: String) -> Set<String> {
        let words = title.lowercased()
            .replacingOccurrences(of: "['’]s\\b", with: "", options: .regularExpression)
            .components(separatedBy: CharacterSet.alphanumerics.inverted)
        return Set(words.filter { ($0.count >= 3 || $0.contains(where: \.isNumber)) && !stopwords.contains($0) })
    }

    private static func inverseDocumentFrequency(_ docs: [Set<String>]) -> [String: Double] {
        var counts: [String: Int] = [:]
        for doc in docs { for t in doc { counts[t, default: 0] += 1 } }
        let n = Double(max(docs.count, 1))
        return counts.mapValues { log(n / Double($0)) + 0.1 }
    }

    /// Two headlines are the same story when they share at least three words, the rarer ones mostly overlap,
    /// and they were published within 3 days. Tuned on live feeds: real matches score 0.43–0.5, near misses share 2 words.
    static func sameStory(_ a: Set<String>, _ b: Set<String>, idf: [String: Double]) -> Bool {
        let shared = a.intersection(b)
        guard shared.count >= 3 else { return false }
        let weight = { (s: Set<String>) in s.reduce(0) { $0 + (idf[$1] ?? 0) } }
        return weight(shared) / min(weight(a), weight(b)) >= 0.4
    }

    /// Groups article indices into stories with union–find.
    private static func clusters(_ articles: [Article], _ tokens: [Set<String>], _ idf: [String: Double]) -> [[Int]] {
        var parent = Array(articles.indices)
        func root(_ i: Int) -> Int {
            var i = i
            while parent[i] != i { parent[i] = parent[parent[i]]; i = parent[i] }
            return i
        }
        for i in articles.indices {
            for j in articles.indices where j > i
                && abs(articles[i].publishedAt.timeIntervalSince(articles[j].publishedAt)) < 3 * 86_400
                && sameStory(tokens[i], tokens[j], idf: idf) {
                parent[root(j)] = root(i)
            }
        }
        return Array(Dictionary(grouping: articles.indices, by: root).values)
    }
}
