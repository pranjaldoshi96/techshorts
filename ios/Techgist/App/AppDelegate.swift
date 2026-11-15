import UIKit
import UserNotifications

/// Shows alerts while the app is open, and acts on a tapped alert or its Read and Save buttons.
final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    /// What to do with a tapped alert: show its card in the feed, or open the full story.
    enum Request: Equatable {
        case show(URL)
        case read(URL)
    }

    /// Set by the feed once it is on screen.
    @MainActor static var handle: ((Request) -> Void)?
    @MainActor private static var pending: Request?

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        StoryNotification.registerCategories()
        Task { await PushRegistration.registerIfPossible() }
        return true
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        PushRegistration.didRegister(deviceToken: deviceToken)
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        PushRegistration.didFail(error)
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification)
        async -> UNNotificationPresentationOptions {
        [.banner, .list, .sound]
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse) async {
        let info = response.notification.request.content.userInfo
        guard let url = StoryNotification.storyURL(from: info) else { return }
        switch response.actionIdentifier {
        case StoryNotification.saveAction:
            // Runs in the background, possibly with no feed on screen.
            if let article = StoryNotification.article(from: info)
                ?? ArticleCache.load()?.articles.first(where: { $0.url == url }) {
                NewsStore.saveFromNotification(article)
            }
        case StoryNotification.readAction:
            Self.deliver(.read(url))
        case UNNotificationDefaultActionIdentifier:
            Self.deliver(.show(url))
        default:
            break
        }
    }

    @MainActor private static func deliver(_ request: Request) {
        if let handle { handle(request) } else { pending = request }
    }

    /// Called once the feed is on screen, so an alert that launched the app still opens its story.
    @MainActor static func takePending() -> Request? {
        defer { pending = nil }
        return pending
    }
}
