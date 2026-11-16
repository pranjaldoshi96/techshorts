import UIKit
import UserNotifications

/// How a story looks as a notification: the headline as the title, one line of new information as the body,
/// the story's picture, Techgist's own chime, and Read and Save buttons.
/// The same category and payload keys are used by server pushes (see `TechgistNotificationService`).
enum StoryNotification {
    static let category = "STORY"
    static let readAction = "READ"
    static let saveAction = "SAVE"
    /// The sound the reader chose in Settings.
    static var sound: UNNotificationSound { AlertSound.current.notificationSound }

    enum Kind: String {
        case breaking, brief
    }

    static func registerCategories() {
        let read = UNNotificationAction(identifier: readAction, title: "Read",
                                        options: [.foreground], icon: UNNotificationActionIcon(systemImageName: "book"))
        let save = UNNotificationAction(identifier: saveAction, title: "Save",
                                        options: [], icon: UNNotificationActionIcon(systemImageName: "bookmark"))
        let story = UNNotificationCategory(identifier: category, actions: [read, save], intentIdentifiers: [],
                                           hiddenPreviewsBodyPlaceholder: "A new tech story",
                                           categorySummaryFormat: "%u more stories", options: [])
        UNUserNotificationCenter.current().setNotificationCategories([story])
    }

    /// Asks for permission once; later calls return the current answer.
    static func requestPermission() async -> Bool {
        let granted = (try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound])) ?? false
        return granted
    }

    /// The notification for `article`. Downloads the picture (8 s at most) and attaches it when it is a JPEG, PNG or GIF.
    static func content(for article: Article, kind: Kind, subtitle: String? = nil) async -> UNMutableNotificationContent {
        let content = UNMutableNotificationContent()
        content.title = article.alertTitle
        if let subtitle { content.subtitle = subtitle }
        content.body = article.alertBody
        content.sound = sound
        content.categoryIdentifier = category
        content.threadIdentifier = kind.rawValue
        content.interruptionLevel = .active
        content.relevanceScore = kind == .breaking ? 1 : 0.6
        content.targetContentIdentifier = article.id
        content.userInfo = userInfo(for: article)
        if let attachment = await attachment(for: article) { content.attachments = [attachment] }
        return content
    }

    /// `url` finds the story in the feed; `article` lets Save work even when the story has left the edition.
    static func userInfo(for article: Article) -> [String: Any] {
        var info: [String: Any] = ["url": article.url.absoluteString]
        if let eventId = article.eventId { info["eventId"] = eventId }
        var slim = article
        slim.content = ""
        if let data = try? JSONEncoder().encode(slim), data.count < 3000, let json = String(data: data, encoding: .utf8) {
            info["article"] = json
        }
        return info
    }

    /// The story link carried by a notification.
    static func storyURL(from userInfo: [AnyHashable: Any]) -> URL? {
        (userInfo["url"] as? String).flatMap(URL.init(string:))
    }

    /// The server's event id carried by a notification.
    static func eventId(from userInfo: [AnyHashable: Any]) -> String? {
        (userInfo["eventId"] as? String).flatMap { $0.isEmpty ? nil : $0 }
    }

    /// The story an alert is about: matched by its link first, then by the event it reports.
    static func story(url: URL?, eventId: String?, in articles: [Article]) -> Article? {
        if let url, let match = articles.first(where: { $0.url == url }) { return match }
        if let eventId, let match = articles.first(where: { $0.eventId == eventId }) { return match }
        return nil
    }

    /// The story carried by a notification, when it was small enough to ride along.
    static func article(from userInfo: [AnyHashable: Any]) -> Article? {
        guard let json = userInfo["article"] as? String else { return nil }
        return try? JSONDecoder().decode(Article.self, from: Data(json.utf8))
    }

    private static func attachment(for article: Article) async -> UNNotificationAttachment? {
        guard let url = article.images?.std ?? article.image else { return nil }
        let download = Task { await ImageLoader.shared.data(for: url) }
        let timeout = Task {
            try? await Task.sleep(for: .seconds(8))
            download.cancel()
        }
        let data = await download.value
        timeout.cancel()
        guard let data, let ext = imageExtension(of: data) else { return nil }
        // The system moves the file into its own store, so it needs a copy of its own.
        let file = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + "." + ext)
        guard (try? data.write(to: file)) != nil else { return nil }
        return try? UNNotificationAttachment(identifier: "image", url: file)
    }

    /// Notifications accept JPEG, PNG and GIF pictures; WebP and anything else is left out.
    static func imageExtension(of data: Data) -> String? {
        let head = [UInt8](data.prefix(4))
        if head.starts(with: [0xFF, 0xD8, 0xFF]) { return "jpg" }
        if head.starts(with: [0x89, 0x50, 0x4E, 0x47]) { return "png" }
        if head.starts(with: [0x47, 0x49, 0x46]) { return "gif" }
        return nil
    }

    /// Shows a sample of the alert a few seconds from now, so its look and sound can be checked.
    static func sendSample(_ article: Article) async {
        let content = await content(for: article, kind: .breaking)
        let trigger = UNTimeIntervalNotificationTrigger(timeInterval: 5, repeats: false)
        try? await UNUserNotificationCenter.current().add(
            UNNotificationRequest(identifier: "sample-\(UUID().uuidString)", content: content, trigger: trigger))
    }
}
