import XCTest

/// The feed must move to the next story with an ordinary short swipe, anywhere on the card, every time.
/// These tests fail if the card on screen does not change.
final class FeedScrollingUITests: FeedUITestCase {
    /// Where drags start, as a fraction of the screen's height: over the picture, the headline, the summary,
    /// the coverage row and the footer buttons.
    private let startPoints: [CGFloat] = [0.30, 0.50, 0.62, 0.74, 0.90]

    /// Pages forward `count` cards with short, slow drags that end at rest, checking each one lands on a new card.
    private func pageForward(_ count: Int, by distance: CGFloat = 90, tab: String) {
        var seen = [settledCard()]
        for step in 0..<count {
            drag(fromY: startPoints[step % startPoints.count], by: distance)
            let now = settledCard()
            XCTAssertNotEqual(now, seen.last, "\(tab): drag \(step + 1) from \(startPoints[step % startPoints.count]) of the way down left the same card on screen")
            XCTAssertFalse(seen.contains(now), "\(tab): drag \(step + 1) went back to an earlier card")
            seen.append(now)
        }
        XCTAssertEqual(Set(seen).count, count + 1)
    }

    func testShortSlowDragsMoveThroughTenCardsInTopAndAI() {
        launch()
        pageForward(10, tab: "Top")
        select(tab: "AI")
        pageForward(10, tab: "AI")
    }

    func testShortSlowDragsMoveThroughTenCardsInStartupsDevAndReads() {
        launch()
        for tab in ["Startups", "Dev", "Reads"] {
            select(tab: tab)
            pageForward(10, tab: tab)
        }
    }

    func testFlicksMoveOneCardAtATime() {
        launch()
        var seen = [settledCard()]
        for _ in 0..<10 {
            app.swipeUp(velocity: .fast)
            let now = settledCard()
            XCTAssertFalse(seen.contains(now), "A flick did not bring a new card")
            seen.append(now)
        }
        // One card per flick, never two: ten flicks down the list, ten back up, and the first card is on screen again.
        for step in 0..<10 {
            app.swipeDown(velocity: .fast)
            XCTAssertEqual(settledCard(), seen[9 - step], "Flick \(step + 1) back up")
        }
    }

    func testDraggingBackShowsThePreviousCard() {
        launch()
        let first = settledCard()
        drag(fromY: 0.6, by: 90)
        let second = settledCard()
        XCTAssertNotEqual(second, first)
        drag(fromY: 0.4, by: -90)
        XCTAssertEqual(settledCard(), first)
    }

    /// A mouse button (or a finger) that rests on the card before moving used to open the press-and-hold menu,
    /// and the drag then picked whichever menu item it ended on.
    func testPressingAndHoldingBeforeDraggingStillMovesToTheNextCard() {
        launch()
        var last = settledCard()
        for (step, start) in startPoints.enumerated() {
            drag(fromY: start, by: 120, hold: 0.9)
            let now = settledCard()
            XCTAssertNotEqual(now, last, "Held drag \(step + 1) from \(start)")
            XCTAssertTrue(feedIsInFront, "Something opened over the feed")
            last = now
        }
    }

    func testFeedScrollsRightAfterLaunchWithNoWait() {
        launch()
        // No settling first: the very first thing a person does is swipe.
        drag(fromY: 0.6, by: 90)
        let second = settledCard()
        drag(fromY: 0.6, by: 90)
        XCTAssertNotEqual(settledCard(), second)
    }

    func testFeedScrollsAfterReturningFromTheReader() {
        launch()
        let first = settledCard()
        point(0.5, 0.6).tap()
        XCTAssertTrue(waitForReader(), "Tapping a card did not open the story")
        closeReader()
        XCTAssertEqual(settledCard(), first, "The feed lost its place behind the reader")
        pageForward(3, tab: "after the reader")
    }

    func testFeedScrollsAfterASheetIsDismissed() {
        launch()
        app.buttons["Settings"].tap()
        XCTAssertTrue(app.navigationBars["Settings"].waitForExistence(timeout: 5))
        app.buttons["Done"].firstMatch.tap()
        pageForward(3, tab: "after Settings")
        app.buttons["Saved stories"].firstMatch.tap()
        XCTAssertTrue(app.navigationBars["Saved"].waitForExistence(timeout: 5))
        app.buttons["Done"].firstMatch.tap()
        pageForward(3, tab: "after Saved")
    }

    /// The alerts offer appears once the eighth card has been read; the feed must keep working behind and after it.
    func testFeedScrollsAfterTheNotificationsOfferIsDismissed() {
        launch(offerAlreadyAnswered: false)
        let notNow = app.buttons["Not now"]
        XCTAssertFalse(notNow.exists, "The offer must not appear at launch")
        var steps = 0
        while !notNow.exists, steps < 14 {
            // A card counts as read after a second and a half on screen.
            sleep(2)
            if notNow.exists { break }
            drag(fromY: 0.6, by: 90)
            _ = settledCard()
            steps += 1
        }
        XCTAssertTrue(notNow.waitForExistence(timeout: 5), "The offer never appeared")
        XCTAssertGreaterThanOrEqual(steps, 7, "The offer appeared before eight stories were read")
        notNow.tap()
        XCTAssertTrue(app.buttons["Settings"].waitForExistence(timeout: 5))
        pageForward(3, tab: "after the offer")
    }

    /// People who turned big story alerts on in 1.x are told once that alerts now come from the server, and
    /// choose, before anything is sent. The feed works behind and after the notice.
    func testTheServerAlertsNoticeIsShownOnceAndTheFeedPagesAfterIt() {
        app.launchEnvironment["UITEST_ALERTS_NOTICE"] = "1"
        launch()
        let notice = app.alerts["Big story alerts now come from Techgist's server"]
        XCTAssertTrue(notice.waitForExistence(timeout: 5), "The notice did not appear")
        XCTAssertTrue(notice.buttons["Keep alerts on"].exists)
        notice.buttons["Turn off alerts"].tap()
        XCTAssertFalse(notice.waitForExistence(timeout: 2))
        pageForward(3, tab: "after the notice")
        app.buttons["Settings"].firstMatch.tap()
        let toggle = app.switches["Big story alerts"].firstMatch
        XCTAssertTrue(toggle.waitForExistence(timeout: 5))
        XCTAssertEqual(toggle.value as? String, "0", "Turn off alerts left the switch on")
    }

    /// Pulling down on the first card refreshes; it must not page, open anything or leave the feed stuck.
    /// (The story that was on screen has been read by then, so the refreshed tab opens on the next unread one.)
    func testPullToRefreshLeavesTheFeedOnACardAndItStillPages() {
        launch()
        _ = settledCard()
        point(0.5, 0.35).press(forDuration: 0.05, thenDragTo: point(0.5, 0.75), withVelocity: 600, thenHoldForDuration: 0.4)
        XCTAssertFalse(settledCard().isEmpty)
        XCTAssertTrue(feedIsInFront)
        pageForward(3, tab: "after a refresh")
    }

    /// The same pull against a slow edition, as over the network. SwiftUI cancels the task it runs the refresh in
    /// a few milliseconds after it starts; the fetch must finish anyway, and must not end in "Offline".
    func testPullToRefreshOverASlowNetworkLoadsAndIsNotOffline() {
        app.launchEnvironment["EDITION_DELAY"] = "1.5"
        launch()
        _ = settledCard()
        let offline = app.descendants(matching: .any).matching(NSPredicate(format: "label BEGINSWITH 'Offline'")).firstMatch
        XCTAssertFalse(offline.exists)
        point(0.5, 0.35).press(forDuration: 0.05, thenDragTo: point(0.5, 0.75), withVelocity: 600, thenHoldForDuration: 0.4)
        // Long enough for the slow fetch to end either way.
        XCTAssertFalse(offline.waitForExistence(timeout: 5), "A refresh with the network up ended in the Offline banner")
        XCTAssertFalse(settledCard().isEmpty)
        pageForward(2, tab: "after a slow refresh")
    }

    func testSwipingLeftOpensTheStoryAndASlantedSwipeDoesNot() {
        launch()
        // Mostly downward, a little sideways: that is a scroll, not "read".
        point(0.6, 0.55).press(forDuration: 0.05, thenDragTo: point(0.45, 0.4), withVelocity: 400, thenHoldForDuration: 0.2)
        _ = settledCard()
        XCTAssertTrue(feedIsInFront, "A slanted scroll opened the story")
        point(0.8, 0.6).press(forDuration: 0.05, thenDragTo: point(0.25, 0.6), withVelocity: 800, thenHoldForDuration: 0.1)
        XCTAssertTrue(waitForReader(), "Swiping left did not open the story")
        closeReader()
        pageForward(2, tab: "after swiping left to read")
    }

    func testEndOfTabIsReachableAndBackToTopWorks() {
        launch()
        select(tab: "Startups")
        let first = settledCard()
        let top = app.buttons["Back to top"]
        var steps = 0
        while !(top.exists && top.isHittable), steps < 20 {
            app.swipeUp(velocity: .fast)
            steps += 1
        }
        XCTAssertTrue(top.isHittable, "Never reached the end of the tab")
        top.tap()
        XCTAssertEqual(settledCard(), first)
    }
}
