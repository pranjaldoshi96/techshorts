import SwiftUI

@main
struct TechgistApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var store = NewsStore()
    @Environment(\.scenePhase) private var scenePhase

    init() {
        #if DEBUG
        UITestSupport.prepare()
        FrameMonitor.shared.startIfAsked()
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

/// Counts late frames on the main thread, for the paging performance test. `FRAME_LOG=/path` turns it on and names
/// the file that receives "frames hitches worst-gap-ms" about once a second.
final class FrameMonitor: NSObject {
    static let shared = FrameMonitor()
    private var link: CADisplayLink?
    private var last: CFTimeInterval = 0
    private var frames = 0, hitches = 0
    private var worst: CFTimeInterval = 0
    private var file: URL?

    func startIfAsked() {
        guard link == nil, let path = ProcessInfo.processInfo.environment["FRAME_LOG"] else { return }
        file = URL(fileURLWithPath: path)
        let link = CADisplayLink(target: self, selector: #selector(tick))
        // Common modes, so it keeps counting while a finger is scrolling.
        link.add(to: .main, forMode: .common)
        self.link = link
    }

    @objc private func tick(_ link: CADisplayLink) {
        defer { last = link.timestamp }
        guard last > 0 else { return }
        let gap = link.timestamp - last
        frames += 1
        // A hitch: the main thread came back more than one whole frame late.
        if gap > 2 * link.duration {
            hitches += 1
            worst = max(worst, gap)
        }
        if frames % 60 == 0, let file {
            try? "\(frames) \(hitches) \(Int(worst * 1000))".write(to: file, atomically: true, encoding: .utf8)
        }
    }
}
#endif
