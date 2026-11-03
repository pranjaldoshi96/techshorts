// Builds a Techgist edition with the app's own feed and ranking code:
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
            source: $0.source ?? "Techgist", category: $0.category, publishedAt: $0.publishedAt)
}
let pinnedKeys = Set(pinned.map { FeedService.dedupeKey($0.url) })
var articles = result.articles.filter { article in
    !pinnedKeys.contains(FeedService.dedupeKey(article.url))
        && !blocked.contains { $0 == article.source || article.url.absoluteString.contains($0) }
}

// A story that one of today's top Reddit posts links to counts as one more outlet covering it, which lifts its rank
// and shows on the card as "+N more". Only exact links match. Reddit's JSON API needs an account and its open RSS
// allows about one request at a time, so all subreddits come in one combined request (the day's top 100 posts).
// If Reddit is unreachable the edition simply goes out without the boost.
let subreddits = ["technology", "programming", "MachineLearning", "LocalLLaMA", "artificial", "startups", "webdev"]

func redditLinks() async -> Set<String>? {
    let url = URL(string: "https://www.reddit.com/r/\(subreddits.joined(separator: "+"))/top/.rss?t=day&limit=100")!
    var request = URLRequest(url: url, timeoutInterval: 30)
    request.setValue("linux:techgist-publisher:1.0", forHTTPHeaderField: "User-Agent")
    for attempt in 1...2 {
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              let status = (response as? HTTPURLResponse)?.statusCode else { return nil }
        if status == 429, attempt == 1 {
            try? await Task.sleep(nanoseconds: 35_000_000_000)   // Reddit's limit resets after 30 s
            continue
        }
        guard status == 200 else { return nil }
        let links = FeedParser.parse(data).compactMap { post in
            HTMLText.allMatches("href=\"([^\"]+)\"", in: post.html)
                .map(HTMLText.decodeEntities)
                .first { $0.hasPrefix("http") && !$0.contains("reddit.com") && !$0.contains("redd.it") }
                .flatMap(URL.init(string:))
                .map(FeedService.dedupeKey)
        }
        return Set(links)
    }
    return nil
}

if let discussed = await redditLinks() {
    var boosted = 0
    for i in articles.indices where discussed.contains(FeedService.dedupeKey(articles[i].url))
        && !(articles[i].alsoCoveredBy ?? []).contains("Reddit") {
        articles[i].alsoCoveredBy = (articles[i].alsoCoveredBy ?? []) + ["Reddit"]
        boosted += 1
    }
    if boosted > 0 { articles = Ranker.rank(articles) }
    print("Reddit: \(discussed.count) linked articles in today's top posts, \(boosted) of ours boosted")
} else {
    print("Reddit: unreachable, publishing without it")
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
