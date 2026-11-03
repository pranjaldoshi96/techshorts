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
    exit(failures > 3 ? 1 : 0)
}
semaphore.wait()
