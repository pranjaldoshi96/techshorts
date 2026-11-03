import Foundation
import UserNotifications
import BackgroundTasks

/// A repeating local notification ("Your daily TechShorts") carrying the latest top headline.
/// Local notifications need no server or push certificate.
enum DailyDigest {
    static let requestID = "daily-digest"

    static var isEnabled: Bool { UserDefaults.standard.bool(forKey: Keys.digestEnabled) }
    static var minuteOfDay: Int { UserDefaults.standard.object(forKey: Keys.digestMinute) as? Int ?? 8 * 60 }

    /// Asks for permission and records whether the digest is on.
    static func enable() async -> Bool {
        let granted = (try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound])) ?? false
        UserDefaults.standard.set(granted, forKey: Keys.digestEnabled)
        return granted
    }

    static func reschedule(with top: Article?) async {
        let center = UNUserNotificationCenter.current()
        center.removePendingNotificationRequests(withIdentifiers: [requestID])
        guard isEnabled else { return }

        let content = UNMutableNotificationContent()
        content.title = "Your daily TechShorts"
        if let top, Date().timeIntervalSince(top.publishedAt) < 18 * 3600 {
            content.body = top.title
        } else {
            content.body = "Today's tech, AI and startup news, 60 words a story."
        }
        content.sound = .default
        let trigger = UNCalendarNotificationTrigger(
            dateMatching: DateComponents(hour: minuteOfDay / 60, minute: minuteOfDay % 60), repeats: true)
        try? await center.add(UNNotificationRequest(identifier: requestID, content: content, trigger: trigger))
    }
}

/// Refreshes the story cache (and the digest headline) while the app is in the background.
enum BackgroundRefresh {
    static let identifier = (Bundle.main.bundleIdentifier ?? "TechShorts") + ".refresh"

    static func schedule() {
        let request = BGAppRefreshTaskRequest(identifier: identifier)
        request.earliestBeginDate = Date().addingTimeInterval(3 * 3600)
        try? BGTaskScheduler.shared.submit(request)
    }

    static func run() async {
        schedule()
        let result = await FeedService().latest()
        guard !result.articles.isEmpty else { return }
        ArticleCache.save(result.articles, at: Date())
        let disabled = Set(UserDefaults.standard.stringArray(forKey: Keys.disabledSources) ?? [])
        await DailyDigest.reschedule(with: result.articles.first { $0.category == .top && !disabled.contains($0.source) })
    }
}
