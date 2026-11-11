// The 1.2 app's own edition decoder, run on every publish by tools/check_edition.py (spec 2.13).
// Article.swift, Edition.swift and Disk.swift here are frozen copies of ios/Techgist at the 1.2 release (eafaa0b):
// 1.2 stays in users' hands after the app moves on, so these must not follow later app changes.
// Usage: v1decode <edition.json>   prints a JSON summary; exit 0 when 1.2 would show the edition, 1 when not.
import Foundation

let args = CommandLine.arguments
guard args.count == 2, let data = FileManager.default.contents(atPath: args[1]) else {
    print("{\"ok\":false,\"error\":\"usage: v1decode <edition.json>\"}")
    exit(2)
}

func fail(_ message: String) -> Never {
    let escaped = message.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"")
        .replacingOccurrences(of: "\n", with: " ")
    print("{\"ok\":false,\"error\":\"\(escaped)\"}")
    exit(1)
}

// Exactly what the app does (Edition.decode: try? with .iso8601 dates); nil means 1.2 falls back to raw feeds.
guard let edition = Edition.decode(data) else {
    // Same decoder again, with the error, so the alert says which field broke.
    let decoder = JSONDecoder()
    decoder.dateDecodingStrategy = .iso8601
    do { _ = try decoder.decode(Edition.self, from: data); fail("Edition.decode returned nil") } catch { fail("\(error)") }
}
var categories: [String: Int] = [:]
for a in edition.articles { categories[a.category.rawValue, default: 0] += 1 }
let ranked = edition.articles.filter { $0.topRank != nil }.count
let withSummary = edition.articles.filter { $0.summary != nil }.count
let cats = categories.keys.sorted().map { "\"\($0)\":\(categories[$0]!)" }.joined(separator: ",")
print("{\"ok\":true,\"articles\":\(edition.articles.count),\"topRanked\":\(ranked),\"withSummary\":\(withSummary),"
      + "\"isFresh\":\(edition.isFresh),\"categories\":{\(cats)}}")
