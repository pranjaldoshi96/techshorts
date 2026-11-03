import SwiftUI

@main
struct TechgistApp: App {
    @StateObject private var store = NewsStore()
    @Environment(\.scenePhase) private var scenePhase

    init() {
        // AsyncImage goes through URLSession, so a bigger shared cache keeps card images around between launches.
        URLCache.shared = URLCache(memoryCapacity: 50_000_000, diskCapacity: 200_000_000)
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(store)
        }
        .onChange(of: scenePhase) { _, phase in
            switch phase {
            case .active:
                Task { await store.refreshIfStale() }
            case .background:
                store.persist()
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
