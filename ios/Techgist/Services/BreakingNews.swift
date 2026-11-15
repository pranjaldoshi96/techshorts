import Foundation
import UserNotifications

/// Alerts for big stories, shown by the app itself until server push arrives. When iOS lets the app refresh in the
/// background, a new Top story becomes a local notification if it is marked breaking by the edition, or is covered by
/// 3+ outlets, and is under 6 hours old. At most 1 per refresh and 3 per 24 hours, never the same story twice.
/// Stories already seen in the app don't alert. Timing depends on when iOS runs the refresh.
enum BreakingNews {
    static var isEnabled: Bool { UserDefaults.standard.bool(forKey: Keys.breakingNews) }

    private static let seenKey = "breakingNewsSeen"     // story ids already alerted or seen in the app
    private static let sentKey = "breakingNewsSentAt"   // times of recent alerts, for the daily cap
    static let minOutlets = 3, maxAgeHours = 6.0, maxPerDay = 3

    /// Asks for permission and records whether alerts are on.
    static func enable() async -> Bool {
        let granted = await StoryNotification.requestPermission()
        UserDefaults.standard.set(granted, forKey: Keys.breakingNews)
        return granted
    }

    static func disable() {
        UserDefaults.standard.set(false, forKey: Keys.breakingNews)
    }

    /// Big stories in `articles` that are not in `seen`, best first: breaking ones, then the spiciest, then by Top rank.
    static func candidates(in articles: [Article], seen: Set<String>, now: Date = Date()) -> [Article] {
        articles
            .filter { $0.topRank != nil && ($0.isBreaking || $0.coverage >= minOutlets) }
            .filter { now.timeIntervalSince($0.publishedAt) < maxAgeHours * 3600 && !seen.contains($0.id) }
            .sorted { a, b in
                if a.isBreaking != b.isBreaking { return a.isBreaking }
                if a.spiceLevel != b.spiceLevel { return a.spiceLevel > b.spiceLevel }
                return a.topRank! < b.topRank!
            }
    }

    private static var seen: Set<String> { Set(UserDefaults.standard.stringArray(forKey: seenKey) ?? []) }

    /// While the app is open, current big stories are on screen anyway, so they won't alert later.
    static func markSeen(_ articles: [Article]) {
        remember(candidates(in: articles, seen: seen).map(\.id))
    }

    /// After a background refresh: alert the best new big story, within the daily cap.
    static func alertIfNeeded(_ articles: [Article], now: Date = Date()) async {
        guard isEnabled, let story = candidates(in: articles, seen: seen, now: now).first else { return }
        let recent = (UserDefaults.standard.array(forKey: sentKey) as? [Date] ?? []).filter { now.timeIntervalSince($0) < 86_400 }
        guard recent.count < maxPerDay else { return }

        let content = await StoryNotification.content(for: story, kind: .breaking)
        let request = UNNotificationRequest(identifier: "big-story-\(story.id)", content: content, trigger: nil)
        guard (try? await UNUserNotificationCenter.current().add(request)) != nil else { return }

        remember([story.id])
        UserDefaults.standard.set(recent + [now], forKey: sentKey)
    }

    private static func remember(_ ids: [String]) {
        guard !ids.isEmpty else { return }
        let all = (UserDefaults.standard.stringArray(forKey: seenKey) ?? []) + ids
        UserDefaults.standard.set(Array(all.suffix(500)), forKey: seenKey)
    }
}
