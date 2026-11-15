import UIKit
import UserNotifications

/// Server push is not live yet. Everything below is ready for it and stays idle until `registerURL` is set.
enum PushConfig {
    /// Where the device token is sent (the registry's `/v1/register`). Nil means: don't ask iOS for a token and
    /// don't upload anything. Set it together with the Push Notifications capability on the app target.
    static let registerURL: URL? = nil
}

/// Asks iOS for a push token, keeps it, and sends it to the registry when one is configured.
enum PushRegistration {
    private static let tokenKey = "pushDeviceToken"
    private static let uploadedKey = "pushTokenUploaded"

    /// The APNs device token as hex, once iOS has issued one.
    static var deviceToken: String? { UserDefaults.standard.string(forKey: tokenKey) }

    static var environment: String {
        #if DEBUG
        return "sandbox"
        #else
        return "production"
        #endif
    }

    /// Registers with APNs when push is configured and the reader has allowed notifications.
    @MainActor static func registerIfPossible() async {
        guard PushConfig.registerURL != nil else { return }
        let status = await UNUserNotificationCenter.current().notificationSettings().authorizationStatus
        guard status == .authorized || status == .provisional else { return }
        UIApplication.shared.registerForRemoteNotifications()
    }

    static func didRegister(deviceToken data: Data) {
        UserDefaults.standard.set(hex(data), forKey: tokenKey)
        Task { await upload() }
    }

    static func didFail(_ error: Error) {
        // No token: local alerts keep working, and the next launch tries again.
        UserDefaults.standard.removeObject(forKey: tokenKey)
    }

    static func hex(_ data: Data) -> String { data.map { String(format: "%02x", $0) }.joined() }

    /// What the registry needs to deliver in the reader's morning and respect their switches.
    static func registration(token: String) -> [String: Any] {
        [
            "token": token,
            "env": environment,
            "tz": TimeZone.current.identifier,
            "brief": DailyDigest.isEnabled,
            "briefMinute": DailyDigest.minuteOfDay,
            "breaking": BreakingNews.isEnabled,
            "appVersion": Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "",
        ]
    }

    /// Sends the token and settings. Skipped when nothing changed since the last successful upload.
    static func upload() async {
        guard let url = PushConfig.registerURL, let token = deviceToken,
              let body = try? JSONSerialization.data(withJSONObject: registration(token: token), options: [.sortedKeys]) else { return }
        let fingerprint = String(decoding: body, as: UTF8.self)
        guard UserDefaults.standard.string(forKey: uploadedKey) != fingerprint else { return }
        var request = URLRequest(url: url, timeoutInterval: 15)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = body
        guard let (_, response) = try? await URLSession.shared.data(for: request),
              let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else { return }
        UserDefaults.standard.set(fingerprint, forKey: uploadedKey)
    }
}
