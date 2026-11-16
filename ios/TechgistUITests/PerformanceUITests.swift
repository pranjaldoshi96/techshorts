import XCTest

/// Measures late frames while paging through the feed. The app (Debug builds) counts frames on its main thread that
/// arrive more than a whole frame late and writes the totals to a file this test reads.
/// Frame timing in the simulator depends on the Mac, so the limit is generous; read the printed numbers.
final class PerformanceUITests: FeedUITestCase {
    func testPagingHitches() throws {
        let log = FileManager.default.temporaryDirectory.appendingPathComponent("techgist-frames.txt")
        try? FileManager.default.removeItem(at: log)
        app.launchEnvironment["FRAME_LOG"] = log.path
        launch()
        _ = settledCard()
        // Let the launch finish, then count only the paging.
        sleep(2)
        let before = try counts(log)
        for _ in 0..<15 { app.swipeUp(velocity: .fast) }
        for _ in 0..<15 { app.swipeDown(velocity: .fast) }
        for step in 0..<10 { drag(fromY: 0.6, by: step % 2 == 0 ? 90 : -90) }
        sleep(2)
        let after = try counts(log)
        let frames = after.frames - before.frames, hitches = after.hitches - before.hitches
        print("PAGING FRAMES: \(frames) frames, \(hitches) late by more than one frame, worst gap since launch \(after.worst) ms")
        XCTAssertGreaterThan(frames, 600)
        // The pager builds the next card just after each snap ends, with the feed at rest; those frames are late
        // too but nobody sees them, and this count cannot tell them apart. Measured 5-7% in a Debug simulator
        // (16 Nov 2026); 18% was the figure when a drag redrew every card.
        XCTAssertLessThan(Double(hitches) / Double(frames), 0.10, "More than 10% of frames were late while paging")
    }

    private func counts(_ log: URL) throws -> (frames: Int, hitches: Int, worst: Int) {
        let parts = try String(contentsOf: log, encoding: .utf8).split(separator: " ").compactMap { Int($0) }
        XCTAssertEqual(parts.count, 3)
        return (parts[0], parts[1], parts[2])
    }
}
