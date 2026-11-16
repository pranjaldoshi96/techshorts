import XCTest

/// Launches the app on a fixture edition (the engine's, re-dated to now so it counts as fresh) as on a first launch.
class FeedUITestCase: XCTestCase {
    var app: XCUIApplication!

    /// The size category to launch with, e.g. `UICTContentSizeCategoryAccessibilityXXXL`. Set from the command line
    /// with `TEST_RUNNER_CONTENT_SIZE=…`; nil keeps the simulator's own setting.
    var contentSize: String? { ProcessInfo.processInfo.environment["CONTENT_SIZE"] }

    override func setUpWithError() throws {
        continueAfterFailure = false
        app = XCUIApplication()
        app.launchEnvironment["UITEST_RESET"] = "1"
        app.launchEnvironment["EDITION_URL"] = try Self.freshEdition().absoluteString
    }

    func launch(offerAlreadyAnswered: Bool = true) {
        if offerAlreadyAnswered { app.launchArguments += ["-notificationsOffered", "YES"] }
        if let contentSize { app.launchArguments += ["-UIPreferredContentSizeCategoryName", contentSize] }
        // The test runner lives in the same simulator, so this is what the app sees too.
        print("SIMULATOR SETTINGS: reduceMotion=\(UIAccessibility.isReduceMotionEnabled) increaseContrast=\(UIAccessibility.isDarkerSystemColorsEnabled) textSize=\(contentSize ?? "simulator default")")
        app.launch()
        XCTAssertTrue(card.waitForExistence(timeout: 20), "The first card never appeared")
    }

    /// The engine's edition from the unit tests' fixtures, with `publishedAt` moved to now.
    static func freshEdition() throws -> URL {
        let fixture = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("TechgistTests/Fixtures/edition-v2-engine.json")
        var json = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: fixture)) as? [String: Any])
        json["publishedAt"] = ISO8601DateFormatter().string(from: Date())
        let file = FileManager.default.temporaryDirectory.appendingPathComponent("techgist-uitest-edition.json")
        try JSONSerialization.data(withJSONObject: json).write(to: file, options: .atomic)
        return file
    }

    // MARK: The feed

    var card: XCUIElement { app.buttons.matching(identifier: "story-card").firstMatch }

    var screen: CGRect { app.windows.firstMatch.frame }

    /// The headline and text of the card under the middle of the screen, or nil while the feed is between cards.
    func visibleCard() -> String? {
        let middle = CGPoint(x: screen.midX, y: screen.midY)
        for element in app.buttons.matching(identifier: "story-card").allElementsBoundByIndex where element.exists {
            let frame = element.frame
            // At rest a card starts at the top of the feed, just under the chips.
            // The start of the label is the headline; its end ("2 hours ago") changes while a test runs.
            if frame.contains(middle), frame.minY > 0, frame.minY < screen.height * 0.3 { return String(element.label.prefix(60)) }
        }
        return nil
    }

    /// Waits for the feed to rest on a card and returns it.
    func settledCard(file: StaticString = #filePath, line: UInt = #line) -> String {
        let deadline = Date().addingTimeInterval(6)
        var last: String?
        while Date() < deadline {
            let now = visibleCard()
            if let now, now == last { return now }
            last = now
            usleep(150_000)
        }
        XCTFail("The feed did not come to rest on a card", file: file, line: line)
        return ""
    }

    func point(_ x: CGFloat, _ y: CGFloat) -> XCUICoordinate {
        app.windows.firstMatch.coordinate(withNormalizedOffset: CGVector(dx: x, dy: y))
    }

    /// What a mouse or trackpad drag in the simulator does, and a careful finger: press, move a short way at an
    /// even pace, stop, and only then let go. Nothing is flicked, so the feed gets no speed to coast on.
    func drag(fromY: CGFloat, by points: CGFloat, x: CGFloat = 0.5, hold: TimeInterval = 0.05) {
        let start = point(x, fromY)
        let end = start.withOffset(CGVector(dx: 0, dy: -points))
        start.press(forDuration: hold, thenDragTo: end, withVelocity: 300, thenHoldForDuration: 0.25)
    }

    // MARK: The reader (in-app Safari)

    var feedIsInFront: Bool {
        let settings = app.buttons["Settings"].firstMatch
        return settings.exists && settings.isHittable
    }

    func waitForReader(timeout: TimeInterval = 10) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if !feedIsInFront { return true }
            usleep(200_000)
        }
        return false
    }

    func closeReader(file: StaticString = #filePath, line: UInt = #line) {
        // Safari's own button is "Done" up to iOS 18 and "Close" from iOS 26.
        for name in ["Close", "Done"] {
            let button = app.buttons[name].firstMatch
            if button.waitForExistence(timeout: 3) {
                button.tap()
                break
            }
        }
        let deadline = Date().addingTimeInterval(6)
        while !feedIsInFront, Date() < deadline { usleep(200_000) }
        XCTAssertTrue(feedIsInFront, "The reader did not close", file: file, line: line)
    }

    func select(tab: String) {
        let chip = app.buttons[tab].firstMatch
        // The chips scroll sideways when they don't all fit (large text, small phones).
        var tries = 0
        while !(chip.frame.maxX <= screen.maxX && chip.frame.minX >= 0), tries < 4 {
            point(0.8, 0).withOffset(CGVector(dx: 0, dy: chipBarY)).press(forDuration: 0.05, thenDragTo: point(0.2, 0).withOffset(CGVector(dx: 0, dy: chipBarY)))
            tries += 1
        }
        chip.tap()
    }

    private var chipBarY: CGFloat { app.buttons["Top"].firstMatch.frame.midY }
}
