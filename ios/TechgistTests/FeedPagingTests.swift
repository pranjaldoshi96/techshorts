import XCTest
@testable import Techgist

/// What a swipe on the paged feed means.
final class FeedPagingTests: XCTestCase {
    func testAShortSlowDragGoesToTheNextCard() {
        // The owner's case: a 90-point drag that ends at rest.
        XCTAssertEqual(FeedPaging.step(moved: -90, projected: -90), 1)
        XCTAssertEqual(FeedPaging.step(moved: -40, projected: -40), 1)
        XCTAssertEqual(FeedPaging.step(moved: 90, projected: 90), -1)
    }

    func testATwitchStaysPut() {
        XCTAssertEqual(FeedPaging.step(moved: -20, projected: -30), 0)
        XCTAssertEqual(FeedPaging.step(moved: 36, projected: 36), 0)
        XCTAssertEqual(FeedPaging.step(moved: 0, projected: 0), 0)
    }

    func testAQuickFlickCountsEvenWhenTheFingerBarelyMoved() {
        XCTAssertEqual(FeedPaging.step(moved: -20, projected: -400), 1)
        XCTAssertEqual(FeedPaging.step(moved: 20, projected: 400), -1)
    }

    func testAHardFlickStillMovesOnlyOneCard() {
        XCTAssertEqual(FeedPaging.step(moved: -300, projected: -5000), 1)
        XCTAssertEqual(FeedPaging.step(moved: 300, projected: 5000), -1)
    }

    func testASidewaysSwipeChangesTab() {
        XCTAssertEqual(FeedPaging.tabStep(dx: -80, dy: 10), 1, "Swiping left shows the tab to the right")
        XCTAssertEqual(FeedPaging.tabStep(dx: 80, dy: -10), -1)
    }

    func testAShortOrDiagonalSwipeDoesNotChangeTab() {
        XCTAssertNil(FeedPaging.tabStep(dx: -40, dy: 0))
        XCTAssertNil(FeedPaging.tabStep(dx: -80, dy: 70))
    }
}
