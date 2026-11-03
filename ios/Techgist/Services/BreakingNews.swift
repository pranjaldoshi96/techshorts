import CloudKit
import UIKit
import UserNotifications

/// Alerts for big stories. The publishing server saves an `Alert` record (title, body, url) to the app's public
/// CloudKit database, and iCloud pushes it to every phone subscribed here. Phones never talk to our server, and
/// nothing about the reader is collected.
enum BreakingNews {
    static let container = CKContainer(identifier: "iCloud.com.pranjald.techgist")
    static let subscriptionID = "breaking-news"

    static var isEnabled: Bool { UserDefaults.standard.bool(forKey: Keys.breakingNews) }

    enum Failure: Error {
        case notificationsDenied, noICloud, failed
    }

    static func enable() async throws {
        let granted = (try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound])) ?? false
        guard granted else { throw Failure.notificationsDenied }
        guard (try? await container.accountStatus()) == .available else { throw Failure.noICloud }
        await MainActor.run { UIApplication.shared.registerForRemoteNotifications() }

        let subscription = CKQuerySubscription(recordType: "Alert", predicate: NSPredicate(value: true),
                                               subscriptionID: subscriptionID, options: .firesOnRecordCreation)
        let info = CKSubscription.NotificationInfo()
        // "%1$@" isn't in any strings file, so iOS shows the record's field as is.
        info.titleLocalizationKey = "%1$@"
        info.titleLocalizationArgs = ["title"]
        info.alertLocalizationKey = "%1$@"
        info.alertLocalizationArgs = ["body"]
        info.soundName = "default"
        info.desiredKeys = ["url"]
        subscription.notificationInfo = info
        do {
            _ = try await container.publicCloudDatabase.save(subscription)
        } catch {
            throw Failure.failed
        }
        UserDefaults.standard.set(true, forKey: Keys.breakingNews)
    }

    static func disable() async {
        UserDefaults.standard.set(false, forKey: Keys.breakingNews)
        _ = try? await container.publicCloudDatabase.deleteSubscription(withID: subscriptionID)
    }

    /// The story link carried by a tapped alert.
    static func storyURL(from userInfo: [AnyHashable: Any]) -> URL? {
        guard let note = CKNotification(fromRemoteNotificationDictionary: userInfo) as? CKQueryNotification,
              let link = note.recordFields?["url"] as? String else { return nil }
        return URL(string: link)
    }
}

/// Re-registers for pushes on launch and opens the story when an alert is tapped.
final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    /// Set by the feed; opens a tapped alert's story.
    @MainActor static var openStory: ((URL) -> Void)?
    @MainActor private static var pending: URL?

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        if BreakingNews.isEnabled { application.registerForRemoteNotifications() }
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
