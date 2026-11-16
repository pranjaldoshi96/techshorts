import UIKit
import UserNotifications

enum PushConfig {
    /// The base URL of Techgist's device registry, e.g. `https://push.example.com` (HTTPS, no trailing path).
    /// The app POSTs to `<registerURL>/v1/devices` and DELETEs `<registerURL>/v1/devices/<token>`.
    /// Nil means server push is off: the app never asks iOS for a token and sends nothing; alerts stay local.
    static let registerURL: URL? = nil
}

/// Server push for big stories. The device registers after the reader allows notifications and has "Big story
/// alerts" on: at that moment, on every launch after it, and whenever iOS issues a new token. Turning the switch off
/// (or taking the permission away) removes the device from the registry.
///
/// The registry learns the APNs token, whether it is a sandbox or production token, the time zone, the locale and
/// the app version. Nothing else leaves the device.
enum PushRegistration {
    /// The token the registry currently holds for this device; nil when it holds none.
    private static let registeredKey = "pushRegisteredToken"

    // Replaced in tests.
    static var baseURL: URL? = PushConfig.registerURL
    static var defaults: UserDefaults = .standard
    /// Sends a request and returns the HTTP status, or nil when there was no answer.
    static var send: (URLRequest) async -> Int? = { request in
        guard let (_, response) = try? await URLSession.shared.data(for: request) else { return nil }
        return (response as? HTTPURLResponse)?.statusCode
    }

    static var registeredToken: String? { defaults.string(forKey: registeredKey) }

    /// True while the server is the one sending big-story alerts to this device, so the app must not send its own.
    static var isRegistered: Bool { baseURL != nil && registeredToken != nil }

    static var environment: String {
        #if DEBUG
        return "sandbox"
        #else
        return "production"
        #endif
    }

    /// Brings the registry in line with the reader's choices. Called on launch, after the permission prompt, and
    /// when "Big story alerts" changes. Never shows a prompt itself.
    @MainActor static func sync() async {
        guard baseURL != nil else { return }
        let status = await UNUserNotificationCenter.current().notificationSettings().authorizationStatus
        if wantsPush(alertsOn: BreakingNews.isEnabled, status: status) {
            // iOS answers through the app delegate, which calls `didRegister(deviceToken:)`.
            UIApplication.shared.registerForRemoteNotifications()
        } else {
            await unregister()
            UIApplication.shared.unregisterForRemoteNotifications()
        }
    }

    static func wantsPush(alertsOn: Bool, status: UNAuthorizationStatus) -> Bool {
        alertsOn && (status == .authorized || status == .provisional)
    }

    static func didRegister(deviceToken data: Data) {
        let token = hex(data)
        Task { await register(token: token) }
    }

    static func didFail(_ error: Error) {
        // No token this time: local alerts keep working, and the next launch asks again.
    }

    static func hex(_ data: Data) -> String { data.map { String(format: "%02x", $0) }.joined() }

    /// Sends the token. When iOS has issued a different token than the one the registry holds, the old one is removed.
    static func register(token: String) async {
        guard let base = baseURL, let request = registerRequest(base: base, token: token) else { return }
        if let old = registeredToken, old != token, let remove = unregisterRequest(base: base, token: old) {
            _ = await send(remove)
        }
        guard let status = await send(request), (200..<300).contains(status) else { return }
        defaults.set(token, forKey: registeredKey)
    }

    /// Removes this device from the registry. If the server can't be reached the token is kept, so the next
    /// launch tries again.
    static func unregister() async {
        guard let base = baseURL, let token = registeredToken,
              let request = unregisterRequest(base: base, token: token) else { return }
        guard let status = await send(request), (200..<300).contains(status) || status == 404 || status == 410 else { return }
        defaults.removeObject(forKey: registeredKey)
    }

    // MARK: The wire format (PUSH CONTRACT)

    static func registration(token: String) -> [String: Any] {
        [
            "token": token,
            "env": environment,
            "tz": TimeZone.current.identifier,
            "locale": Locale.current.identifier,
            "appVersion": Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "",
        ]
    }

    static func registerRequest(base: URL, token: String) -> URLRequest? {
        guard isUsable(base), isToken(token),
              let body = try? JSONSerialization.data(withJSONObject: registration(token: token), options: [.sortedKeys]) else { return nil }
        var request = URLRequest(url: base.appendingPathComponent("v1/devices"), timeoutInterval: 15)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = body
        return request
    }

    static func unregisterRequest(base: URL, token: String) -> URLRequest? {
        guard isUsable(base), isToken(token) else { return nil }
        var request = URLRequest(url: base.appendingPathComponent("v1/devices").appendingPathComponent(token), timeoutInterval: 15)
        request.httpMethod = "DELETE"
        return request
    }

    /// Tokens only ever travel over HTTPS.
    private static func isUsable(_ base: URL) -> Bool { base.scheme == "https" && base.host != nil }

    private static func isToken(_ token: String) -> Bool {
        !token.isEmpty && token.allSatisfy { $0.isHexDigit }
    }
}
