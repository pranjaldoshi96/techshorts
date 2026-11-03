import Foundation
#if canImport(FoundationModels)
import FoundationModels
#endif

/// Writes Inshorts-style ~60-word summaries with Apple's on-device model (Apple Intelligence, iOS 26+).
/// Nothing leaves the device. On unsupported devices the feed excerpt is shown instead.
actor Summarizer {
    static let shared = Summarizer()

    // v2: summaries are fact-checked against the article; older cached ones weren't.
    private static let fileName = "summaries-v2.json"
    private var cache: [String: String] = Disk.load([String: String].self, from: fileName, in: .cachesDirectory) ?? [:]
    private var inflight: [String: Task<String?, Never>] = [:]

    static var isSupported: Bool {
        #if canImport(FoundationModels)
        if #available(iOS 26, *) { return SystemLanguageModel.default.isAvailable }
        #endif
        return false
    }

    static var statusText: String {
        #if canImport(FoundationModels)
        if #available(iOS 26, *) {
            switch SystemLanguageModel.default.availability {
            case .available:
                return "Summaries are written on your iPhone with Apple Intelligence. Nothing is sent to a server."
            case .unavailable(.appleIntelligenceNotEnabled):
                return "Turn on Apple Intelligence in Settings to get on-device summaries."
            case .unavailable(.modelNotReady):
                return "Apple Intelligence is still downloading. Summaries will appear when it's ready."
            case .unavailable:
                return "This iPhone doesn't support Apple Intelligence, so publisher excerpts are shown instead."
            }
        }
        #endif
        return "On-device summaries need iOS 26 and an Apple Intelligence iPhone. Publisher excerpts are shown instead."
    }

    func summary(for article: Article) async -> String? {
        if let s = cache[article.id] { return s }
        if let t = inflight[article.id] { return await t.value }
        // Short feed bodies are already a fine excerpt, and summarizing them invites the model to pad.
        guard Self.isSupported, article.content.split(separator: " ").count >= 80 else { return nil }

        let task = Task { await Self.generate(article) }
        inflight[article.id] = task
        let summary = await task.value
        inflight[article.id] = nil
        if let summary {
            cache[article.id] = summary
            if cache.count > 600 { cache = Dictionary(uniqueKeysWithValues: cache.prefix(400).map { ($0.key, $0.value) }) }
            Disk.save(cache, to: Self.fileName, in: .cachesDirectory)
        }
        return summary
    }

    private static let instructions = """
        You write news briefs for a tech news app. Summarize the article text in 40 to 60 words as a single paragraph. \
        Use only facts stated in the article text. Never add numbers, specifications, prices, dates, names or products \
        that the text does not mention. The text is often only the opening of the article; if so, summarize just that \
        opening and do not guess the rest. Be neutral. Do not add opinions, hype, emojis, a headline, or phrases like \
        "The article" or "In this post".
        """

    private static func generate(_ article: Article) async -> String? {
        #if canImport(FoundationModels)
        if #available(iOS 26, *) {
            let session = LanguageModelSession(instructions: instructions)
            let text = String(article.content.prefix(6000))
            let prompt = "Headline: \(article.title)\nPublished by: \(article.source)\n\nArticle text:\n\(text)"
            guard let response = try? await session.respond(to: prompt, options: GenerationOptions(temperature: 0)) else {
                return nil
            }
            var summary = response.content.trimmingCharacters(in: .whitespacesAndNewlines)
            summary = summary.replacingOccurrences(of: "^(Summary|Brief):\\s*", with: "", options: [.regularExpression, .caseInsensitive])
            // e.g. "Sure, here is a summary of the article text:"
            summary = summary.replacingOccurrences(of: "^(Sure|Here|Okay|Certainly)\\b[^:\\n]{0,80}:\\s*", with: "", options: [.regularExpression, .caseInsensitive])
            summary = summary.trimmingCharacters(in: CharacterSet(charactersIn: "\"“” "))
            guard !summary.isEmpty, isGrounded(summary, in: [article.title, article.source, text].joined(separator: " ")) else {
                return nil
            }
            return HTMLText.truncate(summary, words: 75)
        }
        #endif
        return nil
    }

    /// The on-device model sometimes fills a short teaser with plausible but invented details (specs, prices,
    /// colours). A summary passes only if every number and every capitalized name in it also appears in the source.
    static func isGrounded(_ summary: String, in source: String) -> Bool {
        let haystack = normalized(source)
        let numbers = HTMLText.allMatches("(\\d[\\d.,]*[A-Za-z%]{0,3})", in: summary)
        for number in numbers where !haystack.contains(normalized(number)) { return false }

        for sentence in summary.components(separatedBy: CharacterSet(charactersIn: ".!?:;")) {
            let words = sentence.split(whereSeparator: { $0 == " " || $0 == "-" || $0 == "/" || $0 == "(" || $0 == ")" })
            for word in words.dropFirst() {
                let name = word.split(whereSeparator: { $0 == "'" || $0 == "’" }).first.map(String.init) ?? ""
                let letters = name.trimmingCharacters(in: .letters.inverted)
                guard letters.count >= 3, letters.first?.isUppercase == true else { continue }
                if !haystack.contains(letters.lowercased()) { return false }
            }
        }
        return true
    }

    private static func normalized(_ s: String) -> String {
        s.lowercased().replacingOccurrences(of: ",", with: "").trimmingCharacters(in: CharacterSet(charactersIn: ". "))
    }
}
