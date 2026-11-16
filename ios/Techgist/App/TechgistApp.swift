import SwiftUI

@main
struct TechgistApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var store = NewsStore()
    @Environment(\.scenePhase) private var scenePhase

    init() {
        #if DEBUG
        UITestSupport.prepare()
        #endif
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(store)
                .tint(Theme.accent)
                .task { await store.becameActive() }
        }
        .onChange(of: scenePhase) { _, phase in
            switch phase {
            case .active:
                Task { await store.becameActive() }
            case .background:
                store.enteredBackground()
                BackgroundRefresh.schedule()
            default:
                break
            }
        }
        .backgroundTask(.appRefresh(BackgroundRefresh.identifier)) {
            await BackgroundRefresh.run()
        }
    }
}

#if DEBUG
/// Hooks for the UI tests, compiled into Debug builds only.
/// `UITEST_RESET=1` starts the app as on a first launch: no cached edition, nothing read or saved, default settings.
/// (`EDITION_URL`, read by `FeedService`, points the app at a fixture edition.)
enum UITestSupport {
    static func prepare() {
        guard ProcessInfo.processInfo.environment["UITEST_RESET"] == "1" else { return }
        let files = FileManager.default
        for (name, directory) in [("articles.json", FileManager.SearchPathDirectory.cachesDirectory), ("read.json", .cachesDirectory),
                                  ("saved.json", .applicationSupportDirectory)] {
            try? files.removeItem(at: Disk.url(name, in: directory))
        }
        if let domain = Bundle.main.bundleIdentifier { UserDefaults.standard.removePersistentDomain(forName: domain) }
    }
}
#endif
