import Foundation
#if canImport(FoundationModels)
import FoundationModels
#endif

/// Writes Inshorts-style ~60-word summaries with Apple's on-device model (Apple Intelligence, iOS 26+).
/// Nothing leaves the device. On unsupported devices the feed excerpt is shown instead.
actor Summarizer {
    static let shared = Summarizer()

    private static let fileName = "summaries.json"
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
        You write news briefs for a tech news app. Summarize the article in 50 to 60 words as a single paragraph. \
        Lead with the key fact (who did what) and then why it matters. Be neutral and factual and use only information \
        from the article. Do not add opinions, hype, emojis, a headline, or phrases like "The article" or "In this post".
        """

    private static func generate(_ article: Article) async -> String? {
        #if canImport(FoundationModels)
        if #available(iOS 26, *) {
            let session = LanguageModelSession(instructions: instructions)
            let prompt = "Headline: \(article.title)\nPublished by: \(article.source)\n\nArticle text:\n\(article.content.prefix(6000))"
            guard let response = try? await session.respond(to: prompt, options: GenerationOptions(temperature: 0.2)) else {
                return nil
            }
            var text = response.content.trimmingCharacters(in: .whitespacesAndNewlines)
            text = text.replacingOccurrences(of: "^(Summary|Brief):\\s*", with: "", options: [.regularExpression, .caseInsensitive])
            text = text.trimmingCharacters(in: CharacterSet(charactersIn: "\"“” "))
            return text.isEmpty ? nil : HTMLText.truncate(text, words: 75)
        }
        #endif
        return nil
    }
}
