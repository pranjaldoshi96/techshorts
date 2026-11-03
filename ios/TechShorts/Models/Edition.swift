import Foundation

/// A ranked set of stories built and published by the TechShorts server (see `server/` in the repo).
/// The app shows it while it's under 8 hours old, and fetches the publishers' feeds itself when it isn't.
struct Edition: Codable, Sendable {
    var publishedAt: Date
    var articles: [Article]

    static let url = URL(string: "https://pranjaldoshi96.github.io/TechShorts/edition.json")!
    static let maxAge: TimeInterval = 8 * 3600

    var isFresh: Bool { Date().timeIntervalSince(publishedAt) < Self.maxAge && !articles.isEmpty }

    static func decode(_ data: Data) -> Edition? {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return try? decoder.decode(Edition.self, from: data)
    }

    func encoded() throws -> Data {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return try encoder.encode(self)
    }
}

/// Where the stories on screen came from.
enum NewsOrigin: Equatable, Sendable {
    case edition(publishedAt: Date)
    case direct
}
