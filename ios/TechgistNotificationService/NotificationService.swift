import UserNotifications

/// Runs when a server push with `mutable-content: 1` arrives: downloads the story's picture named in the payload's
/// `image` key and attaches it, so the banner shows a thumbnail and a long press shows the full picture.
/// If the picture isn't there within 8 seconds, the notification is delivered without it.
final class NotificationService: UNNotificationServiceExtension {
    private var contentHandler: ((UNNotificationContent) -> Void)?
    private var bestAttempt: UNMutableNotificationContent?
    private var download: URLSessionDataTask?

    override func didReceive(_ request: UNNotificationRequest,
                             withContentHandler contentHandler: @escaping (UNNotificationContent) -> Void) {
        self.contentHandler = contentHandler
        bestAttempt = request.content.mutableCopy() as? UNMutableNotificationContent
        guard let content = bestAttempt,
              let link = request.content.userInfo["image"] as? String,
              let url = URL(string: link), url.scheme == "https" else {
            contentHandler(request.content)
            return
        }

        var imageRequest = URLRequest(url: url)
        imageRequest.timeoutInterval = 8
        download = URLSession.shared.dataTask(with: imageRequest) { [weak self] data, response, _ in
            guard let self else { return }
            if let data, (response as? HTTPURLResponse)?.statusCode == 200, let ext = Self.imageExtension(of: data) {
                let file = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + "." + ext)
                if (try? data.write(to: file)) != nil,
                   let attachment = try? UNNotificationAttachment(identifier: "image", url: file) {
                    content.attachments = [attachment]
                }
            }
            self.deliver()
        }
        download?.resume()
    }

    override func serviceExtensionTimeWillExpire() {
        download?.cancel()
        deliver()
    }

    private func deliver() {
        guard let handler = contentHandler, let content = bestAttempt else { return }
        contentHandler = nil
        handler(content)
    }

    /// Notifications accept JPEG, PNG and GIF pictures.
    static func imageExtension(of data: Data) -> String? {
        let head = [UInt8](data.prefix(4))
        if head.starts(with: [0xFF, 0xD8, 0xFF]) { return "jpg" }
        if head.starts(with: [0x89, 0x50, 0x4E, 0x47]) { return "png" }
        if head.starts(with: [0x47, 0x49, 0x46]) { return "gif" }
        return nil
    }
}
