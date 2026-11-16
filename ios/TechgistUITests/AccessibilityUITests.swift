import XCTest

/// Runs Apple's accessibility audit (what Accessibility Inspector checks: contrast, clipped text, missing labels,
/// small hit regions, text that ignores the text size, wrong traits) on each main screen.
///
/// Run it again at other settings to cover them, e.g.
/// `TEST_RUNNER_CONTENT_SIZE=UICTContentSizeCategoryAccessibilityXXXL xcodebuild test -only-testing:TechgistUITests/AccessibilityUITests …`
/// and after `xcrun simctl ui <device> increase_contrast enabled`.
final class AccessibilityUITests: FeedUITestCase {
    /// `inScrollingSheet`: the screen is a half-height sheet whose content scrolls and which can be pulled to full
    /// height, so the audit's prediction that text "may be clipped at larger sizes" does not apply to it.
    private func audit(_ screen: String, inScrollingSheet: Bool = false, file: StaticString = #filePath, line: UInt = #line) throws {
        continueAfterFailure = true
        // `TEST_RUNNER_SCREENSHOT_DIR=/some/folder` keeps a picture of every audited screen, to look at.
        if let folder = ProcessInfo.processInfo.environment["SCREENSHOT_DIR"] {
            let name = screen.replacingOccurrences(of: " ", with: "-").replacingOccurrences(of: ",", with: "") + ".png"
            try? XCUIScreen.main.screenshot().pngRepresentation.write(to: URL(fileURLWithPath: folder).appendingPathComponent(name))
        }
        var found: [String] = []
        let window = self.screen
        try app.performAccessibilityAudit { issue in
            if Self.isExpected(issue, in: window) { return true }
            if inScrollingSheet, issue.auditType == .textClipped { return true }
            var what = issue.compactDescription + " (" + issue.detailedDescription + ")"
            if let element = issue.element {
                let frame = element.frame
                what += " [\"\(element.label.prefix(50))\" at \(Int(frame.minX)),\(Int(frame.minY)) \(Int(frame.width))x\(Int(frame.height))]"
            }
            found.append(what)
            return true
        }
        XCTAssertTrue(found.isEmpty, "\(screen): \(found.count) accessibility issues\n" + found.joined(separator: "\n"), file: file, line: line)
    }

    /// Findings that are not defects in the app. Each rule says why.
    private static func isExpected(_ issue: XCUIAccessibilityAuditIssue, in window: CGRect) -> Bool {
        let label = issue.element?.label ?? ""
        let frame = issue.element?.frame ?? .zero
        // A source's initial in its coloured circle: decoration beside the source's name, white on a fixed colour
        // of at least 6.5:1, never larger than twice its size. The audit samples the photo around the circle.
        let isMonogram = label.count == 1 && frame.width <= 64 && abs(frame.width - frame.height) < 1
        // The system's own empty-state view ("Nothing saved", "Search stories") and its caption.
        let isEmptyState = ["Nothing saved", "Search stories", "Tap the bookmark on a story to keep it here.",
                            "Find a story by headline, summary or outlet."].contains(label)
        switch issue.auditType {
        case .hitRegion:
            // The clear button inside the system's search field.
            return label == "Clear text"
        case .elementDetection:
            // "This element appears to display text": publishers' photos often contain words (a screenshot, a
            // sign). The card's label already carries the headline and summary; the picture is decoration.
            return issue.element == nil
        case .dynamicType:
            // Navigation-bar items never scale in iOS; pressing and holding one shows it large instead.
            if frame.maxY < 130, frame.height <= 44 { return true }
            if ["Done", "Close", "Clear all"].contains(label), frame.height <= 44 { return true }
            // The last row of Settings, at the bottom edge of the sheet. It is ordinary text and does scale (checked
            // at the largest size); the audit cannot follow it off screen when it enlarges the text.
            if label == "Stories" || label.hasPrefix("Techgist edition") || label.hasPrefix("Direct from") { return true }
            // The card's footer buttons stop growing at the third accessibility size so all of them stay on one
            // row (like a tab bar), and each has a Large Content Viewer label.
            if label.hasPrefix("Read more") || label == "Read" { return true }
            return isMonogram || isEmptyState
        case .textClipped:
            // The audit predicts clipping "at larger Dynamic Type sizes" from a line limit. These have one on purpose:
            // the footer label (which falls back to a shorter label instead of truncating), the alert preview
            // (two and three lines, like a real banner), and the system's search field.
            if label.hasPrefix("Read more") || label == "Read" { return true }
            if issue.element?.elementType == .searchField || isEmptyState { return true }
            return issue.element == nil
        case .contrast:
            // A row cut off by the bottom edge of the screen is measured against whatever is behind the cut.
            if frame.maxY > window.maxY - 4 || frame.minY < 0 { return true }
            // iOS 26 blurs and fades whatever scrolls under the top bars (the scroll-edge effect), and the audit
            // measures text there against that blur. The same text passes everywhere below it.
            if frame.minY < 200 { return true }
            return isMonogram
        case .sufficientElementDescription:
            // The system keyboard's suggestion bar.
            return issue.detailedDescription.contains("TUIPrediction")
        default:
            return false
        }
    }

    /// Lets the feed come to rest. Unlike `settledCard()` this also works at the accessibility text sizes, where
    /// the feed is a free-scrolling list of tall cards.
    private func settle() { usleep(1_500_000) }

    func testFeedCard() throws {
        launch()
        settle()
        try audit("feed")
    }

    func testSecondCardAndOtherTabs() throws {
        launch()
        drag(fromY: 0.6, by: 90)
        settle()
        try audit("second card")
        for tab in ["AI", "Reads"] {
            select(tab: tab)
            settle()
            try audit(tab)
        }
    }

    func testEndOfTab() throws {
        // At the accessibility sizes a tab is a hundred screens long; the end card is the same view at any size.
        try XCTSkipIf(contentSize?.contains("Accessibility") == true)
        launch()
        select(tab: "Startups")
        let top = app.buttons["Back to top"]
        var steps = 0
        while !(top.exists && top.isHittable), steps < 120 {
            app.swipeUp(velocity: .fast)
            steps += 1
        }
        XCTAssertTrue(top.exists && top.isHittable)
        try audit("end of tab")
    }

    func testSettings() throws {
        launch()
        app.buttons["Settings"].firstMatch.tap()
        XCTAssertTrue(app.navigationBars["Settings"].waitForExistence(timeout: 5))
        try audit("settings")
    }

    func testSavedEmptyAndWithAStory() throws {
        launch()
        app.buttons["Saved stories"].firstMatch.tap()
        XCTAssertTrue(app.navigationBars["Saved"].waitForExistence(timeout: 5))
        try audit("saved, empty")
        app.buttons["Done"].firstMatch.tap()
        app.buttons["Save story"].firstMatch.tap()
        app.buttons["Saved stories"].firstMatch.tap()
        XCTAssertTrue(app.navigationBars["Saved"].waitForExistence(timeout: 5))
        try audit("saved, one story")
    }

    func testSearch() throws {
        launch()
        app.buttons["Search"].firstMatch.tap()
        let field = app.searchFields.firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        try audit("search, empty")
        field.tap()
        field.typeText("Nvidia")
        try audit("search, results")
    }

    func testFullCoverage() throws {
        launch()
        settle()
        app.buttons["More"].firstMatch.tap()
        let coverage = app.buttons["Full coverage"].firstMatch
        XCTAssertTrue(coverage.waitForExistence(timeout: 5))
        coverage.tap()
        XCTAssertTrue(app.navigationBars["Full coverage"].waitForExistence(timeout: 5))
        try audit("full coverage", inScrollingSheet: true)
    }

    func testNotificationsOffer() throws {
        launch(offerAlreadyAnswered: false)
        // The first save is one of the two moments the offer appears.
        app.buttons["Save story"].firstMatch.tap()
        XCTAssertTrue(app.buttons["Not now"].waitForExistence(timeout: 5))
        try audit("alerts offer", inScrollingSheet: true)
    }
}
