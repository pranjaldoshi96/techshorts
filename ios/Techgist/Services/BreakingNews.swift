import UIKit
import UserNotifications

/// Alerts for big stories, shown by the app itself. When iOS lets the app refresh in the background, any new
/// Top story covered by 3+ outlets (Reddit counts as one) and under 6 hours old becomes a local notification:
/// at most 1 per refresh and 3 per 24 hours, never the same story twice. Stories already seen in the app don't alert.
/// No push server or iCloud is involved, so timing depends on when iOS runs the refresh.
enum BreakingNews {
    static var isEnabled: Bool { UserDefaults.standard.bool(forKey: Keys.breakingNews) }

    private static let seenKey = "breakingNewsSeen"     // story ids already alerted or seen in the app
    private static let sentKey = "breakingNewsSentAt"   // times of recent alerts, for the daily cap
    private static let minOutlets = 3, maxAgeHours = 6.0, maxPerDay = 3

    /// Asks for permission and records whether alerts are on.
    static func enable() async -> Bool {
        let granted = (try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound])) ?? false
        UserDefaults.standard.set(granted, forKey: Keys.breakingNews)
        return granted
    }

    static func disable() {
        UserDefaults.standard.set(false, forKey: Keys.breakingNews)
    }

    /// Big stories in `articles` that haven't alerted or been seen yet, best first.
    static func candidates(in articles: [Article], now: Date = Date()) -> [Article] {
        let seen = Set(UserDefaults.standard.stringArray(forKey: seenKey) ?? [])
        return articles
            .filter { $0.topRank != nil && 1 + ($0.alsoCoveredBy?.count ?? 0) >= minOutlets }
            .filter { now.timeIntervalSince($0.publishedAt) < maxAgeHours * 3600 && !seen.contains($0.id) }
            .sorted { $0.topRank! < $1.topRank! }
    }

    /// While the app is open, current big stories are on screen anyway, so they won't alert later.
    static func markSeen(_ articles: [Article]) {
        remember(candidates(in: articles).map(\.id))
    }

    /// After a background refresh: alert the best new big story, within the daily cap.
    static func alertIfNeeded(_ articles: [Article], now: Date = Date()) async {
        guard isEnabled, let story = candidates(in: articles, now: now).first else { return }
        let recent = (UserDefaults.standard.array(forKey: sentKey) as? [Date] ?? []).filter { now.timeIntervalSince($0) < 86_400 }
        guard recent.count < maxPerDay else { return }

        let content = UNMutableNotificationContent()
        let others = story.alsoCoveredBy?.count ?? 0
        content.title = others > 0 ? "\(story.source) + \(others) more" : story.source
        content.body = story.displayTitle
        content.sound = .default
        content.userInfo = ["url": story.url.absoluteString]
        let request = UNNotificationRequest(identifier: "big-story-\(story.id)", content: content, trigger: nil)
        guard (try? await UNUserNotificationCenter.current().add(request)) != nil else { return }

        remember([story.id])
        UserDefaults.standard.set(recent + [now], forKey: sentKey)
    }

    private static func remember(_ ids: [String]) {
        guard !ids.isEmpty else { return }
        let seen = (UserDefaults.standard.stringArray(forKey: seenKey) ?? []) + ids
        UserDefaults.standard.set(Array(seen.suffix(500)), forKey: seenKey)
    }

    /// The story link carried by a tapped alert.
    static func storyURL(from userInfo: [AnyHashable: Any]) -> URL? {
        (userInfo["url"] as? String).flatMap(URL.init(string:))
    }
}

/// Shows alerts while the app is open and opens the story when one is tapped.
final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    /// Set by the feed; opens a tapped alert's story.
    @MainActor static var openStory: ((URL) -> Void)?
    @MainActor private static var pending: URL?

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        return true
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification)
        async -> UNNotificationPresentationOptions {
        [.banner, .sound]
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse) async {
        guard let url = BreakingNews.storyURL(from: response.notification.request.content.userInfo) else { return }
        await MainActor.run {
            if let open = Self.openStory { open(url) } else { Self.pending = url }
        }
    }

    /// Called once the feed is on screen, so an alert that launched the app still opens its story.
    @MainActor static func takePending() -> URL? {
        defer { pending = nil }
        return pending
    }
}
