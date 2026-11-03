// Fetches every feed with the app's own parser and reports what each one yields.
// Run: scripts/feedcheck.sh
import Foundation

let semaphore = DispatchSemaphore(value: 0)
Task {
    var failures = 0
    for source in FeedSource.all {
        do {
            let items = try await FeedService().fetch(source)
            let noImage = items.filter { $0.image == nil }.count
            let noExcerpt = items.filter { $0.excerpt.isEmpty }.count
            print("✓ \(source.name.padding(toLength: 20, withPad: " ", startingAt: 0)) \(items.count) stories, \(noImage) without image, \(noExcerpt) without excerpt")
            if let a = items.first { print("    \(a.title)\n    \(a.excerpt.prefix(140))") }
        } catch {
            failures += 1
            print("✗ \(source.name): \(error.localizedDescription)")
        }
    }
    let all = await FeedService().fetchAll()
    print("\nTotal after dedupe: \(all.articles.count) stories; failed: \(all.failed)")
    for c in Category.allCases { print("  \(c.rawValue): \(all.articles.filter { $0.category == c }.count)") }
    let topTab = all.articles.filter { $0.topRank != nil }.sorted { $0.topRank! < $1.topRank! }
    print("\nTop tab: \(topTab.count) stories, by tab: " + Category.allCases.map { c in "\(c.rawValue) \(topTab.filter { $0.category == c }.count)" }.joined(separator: ", "))
    for a in topTab.prefix(12) { print("  [\(a.category.rawValue)] \(a.source): \(a.title.prefix(70))") }
    for c in Category.allCases {
        print("\nTop of \(c.rawValue):")
        for a in all.articles.filter({ $0.category == c }).prefix(6) {
            let age = Int(Date().timeIntervalSince(a.publishedAt) / 3600)
            let extra = [a.points.map { "\($0) pts" }, a.alsoCoveredBy.map { "also: " + $0.joined(separator: ", ") }]
                .compactMap { $0 }.joined(separator: "; ")
            print("  \(age)h  \(a.source): \(a.title.prefix(80))" + (extra.isEmpty ? "" : "  [\(extra)]"))
        }
    }
    exit(failures > 3 ? 1 : 0)
}
semaphore.wait()
