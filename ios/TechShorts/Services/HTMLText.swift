import Foundation

/// Lightweight HTML-to-text helpers. Feed bodies are small, so regex is good enough and avoids main-thread HTML parsing.
enum HTMLText {
    static func plain(_ html: String) -> String {
        var s = html.replacingOccurrences(of: "<(script|style|figcaption)[^>]*>[\\s\\S]*?</\\1>", with: " ", options: [.regularExpression, .caseInsensitive])
        s = s.replacingOccurrences(of: "<[^>]+>", with: " ", options: .regularExpression)
        s = decodeEntities(s)
        return s.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static let boilerplate = [
        "The post .* appeared first on .*$", "Continue reading.*$", "Read more.*$", "Read the full .*$",
        "\\[(…|\\.\\.\\.)\\]\\s*$", "\\s*Discussion \\| Link\\s*$",
        "^This (story|article) originally appeared in .*?sign up here\\s*\\.\\s*",
    ]

    /// Strips publisher boilerplate such as "The post … appeared first on …".
    static func clean(_ text: String) -> String {
        var t = text
        for p in boilerplate { t = t.replacingOccurrences(of: p, with: "", options: [.regularExpression, .caseInsensitive]) }
        return t.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// Cuts to `words`, preferring to end on a full sentence.
    static func truncate(_ text: String, words maxWords: Int) -> String {
        let words = text.split(separator: " ")
        guard words.count > maxWords else { return text }
        let cut = words.prefix(maxWords).joined(separator: " ")
        let stops = [". ", "? ", "! "].compactMap { cut.range(of: $0, options: .backwards)?.lowerBound }
        if let last = stops.max(), cut.distance(from: cut.startIndex, to: last) > cut.count / 2 {
            return String(cut[...last])
        }
        return cut.trimmingCharacters(in: CharacterSet(charactersIn: ",;:-– ")) + "…"
    }

    static func firstImage(in html: String) -> URL? {
        allMatches("<img[^>]+src=[\"']([^\"']+)[\"']", in: html)
            .map(decodeEntities)
            .first { $0.hasPrefix("https://") && !$0.contains("feedburner") && !$0.contains("pixel") && !$0.contains("gravatar") }
            .flatMap(URL.init(string:))
    }

    // MARK: Regex helpers (return the first capture group that matched)

    static func firstMatch(_ pattern: String, in s: String) -> String? {
        guard let re = try? NSRegularExpression(pattern: pattern, options: .caseInsensitive),
              let m = re.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)) else { return nil }
        return group(of: m, in: s)
    }

    static func allMatches(_ pattern: String, in s: String) -> [String] {
        guard let re = try? NSRegularExpression(pattern: pattern, options: .caseInsensitive) else { return [] }
        return re.matches(in: s, range: NSRange(s.startIndex..., in: s)).compactMap { group(of: $0, in: s) }
    }

    private static func group(of m: NSTextCheckingResult, in s: String) -> String? {
        for i in 1..<max(m.numberOfRanges, 2) {
            if let r = Range(m.range(at: i), in: s) { return String(s[r]) }
        }
        return nil
    }

    // MARK: Entities

    private static let named: [String: String] = [
        "amp": "&", "lt": "<", "gt": ">", "quot": "\"", "apos": "'", "nbsp": " ", "hellip": "…",
        "mdash": "—", "ndash": "–", "rsquo": "’", "lsquo": "‘", "rdquo": "”", "ldquo": "“", "copy": "©", "trade": "™",
    ]

    static func decodeEntities(_ s: String) -> String {
        guard s.contains("&") else { return s }
        var out = ""
        var i = s.startIndex
        while i < s.endIndex {
            if s[i] == "&", let semi = s[i...].prefix(12).firstIndex(of: ";"),
               let decoded = decodeOne(s[s.index(after: i)..<semi]) {
                out += decoded
                i = s.index(after: semi)
                continue
            }
            out.append(s[i])
            i = s.index(after: i)
        }
        return out
    }

    private static func decodeOne(_ name: Substring) -> String? {
        let scalar: UInt32?
        if name.hasPrefix("#x") || name.hasPrefix("#X") { scalar = UInt32(name.dropFirst(2), radix: 16) }
        else if name.hasPrefix("#") { scalar = UInt32(name.dropFirst()) }
        else { return named[name.lowercased()] }
        return scalar.flatMap(Unicode.Scalar.init).map { String(Character($0)) }
    }
}
