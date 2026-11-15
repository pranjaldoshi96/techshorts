import Foundation
import SwiftUI

/// Which stories have been read: on screen for 1.5 seconds, or opened. Kept for 7 days, on this device only.
@MainActor
final class ReadState: ObservableObject {
    static let shared = ReadState()
    static let keepFor: TimeInterval = 7 * 86_400
    private static let fileName = "read.json"

    @Published private(set) var readAt: [String: Date]
    private var unsaved = 0

    init(readAt: [String: Date]? = nil, now: Date = Date()) {
        let stored = readAt ?? Disk.load([String: Date].self, from: Self.fileName, in: .cachesDirectory) ?? [:]
        self.readAt = stored.filter { now.timeIntervalSince($0.value) < Self.keepFor }
    }

    var ids: Set<String> { Set(readAt.keys) }

    func isRead(_ id: String) -> Bool { readAt[id] != nil }

    func markRead(_ id: String, now: Date = Date()) {
        guard readAt[id] == nil else { return }
        readAt[id] = now
        unsaved += 1
        persist()
    }

    func persist() {
        guard unsaved > 0 else { return }
        unsaved = 0
        Disk.save(readAt, to: Self.fileName, in: .cachesDirectory)
    }
}
