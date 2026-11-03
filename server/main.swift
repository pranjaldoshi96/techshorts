// Builds a TechShorts edition with the app's own feed and ranking code:
// fetches every feed, fills in missing images and excerpts from article pages,
// applies editorial.json (pinned stories, blocked links), and writes edition.json.
// Usage: publisher <editorial.json> <edition.json>. publish.sh runs it; see README.md.
import Foundation

struct Editorial: Decodable {
    struct Story: Decodable {
        var title: String
        var url: URL
        var excerpt: String
        var image: URL?
        var category: Category
        var source: String?
        var publishedAt: Date
    }
    /// Your own stories, shown first in their category until removed from the file.
    var pinned: [Story]?
    /// Stories to leave out: a source name, or any part of a story's link.
    var blocked: [String]?
}

let args = CommandLine.arguments
guard args.count == 3 else {
    print("usage: publisher <editorial.json> <edition.json>")
    exit(2)
}

var editorial = Editorial()
if let data = FileManager.default.contents(atPath: args[1]) {
    let decoder = JSONDecoder()
    decoder.dateDecodingStrategy = .iso8601
    do { editorial = try decoder.decode(Editorial.self, from: data) } catch {
        print("✗ \(args[1]) is not valid: \(error)")
        exit(1)
    }
}

let result = await FeedService().fetchAll()
if !result.failed.isEmpty { print("Feeds that failed: \(result.failed.joined(separator: ", "))") }
// A thin result usually means the network was down. Publishing nothing lets the app fall back to direct fetching.
guard result.articles.count >= 60 else {
    print("✗ Only \(result.articles.count) stories; not publishing.")
    exit(1)
}

let blocked = editorial.blocked ?? []
let pinned = (editorial.pinned ?? []).map {
    Article(url: $0.url, title: $0.title, excerpt: $0.excerpt, content: "", image: $0.image,
            source: $0.source ?? "TechShorts", category: $0.category, publishedAt: $0.publishedAt)
}
let pinnedKeys = Set(pinned.map { FeedService.dedupeKey($0.url) })
var articles = result.articles.filter { article in
    !pinnedKeys.contains(FeedService.dedupeKey(article.url))
        && !blocked.contains { $0 == article.source || article.url.absoluteString.contains($0) }
}

// Fill in what the feeds left out here, so phones don't each have to load the article pages.
let missing = articles.indices.filter { articles[$0].image == nil || articles[$0].excerpt.isEmpty }
for start in stride(from: 0, to: missing.count, by: 8) {
    let batch = missing[start..<min(start + 8, missing.count)]
    let metas = await withTaskGroup(of: (Int, PageMetadata.Meta?).self) { group in
        for i in batch {
            let url = articles[i].url
            group.addTask { (i, await PageMetadata.shared.meta(for: url)) }
        }
        var out: [(Int, PageMetadata.Meta?)] = []
        for await r in group { out.append(r) }
        return out
    }
    for case let (i, meta?) in metas {
        if articles[i].image == nil { articles[i].image = meta.image }
        if articles[i].excerpt.isEmpty, let d = meta.description { articles[i].excerpt = d }
    }
}

// Ranked stories are grouped by category; pinned stories go first in theirs.
let ordered = Category.allCases.flatMap { c in pinned.filter { $0.category == c } + articles.filter { $0.category == c } }
let edition = Edition(publishedAt: Date(), articles: ordered)
do {
    try edition.encoded().write(to: URL(fileURLWithPath: args[2]), options: .atomic)
} catch {
    print("✗ Couldn't write \(args[2]): \(error)")
    exit(1)
}

let stillMissing = ordered.filter { $0.image == nil }.count
print("✓ \(ordered.count) stories (\(pinned.count) pinned, \(result.articles.count - articles.count) blocked, \(stillMissing) without image)")
for c in Category.allCases { print("  \(c.rawValue): \(ordered.filter { $0.category == c }.count)") }
