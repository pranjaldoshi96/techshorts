import XCTest
@testable import Techgist

/// The rule that decides which card the feed rests on after a swipe.
final class FeedPagingTests: XCTestCase {
    private let page: CGFloat = 800
    private func rest(start: CGFloat, projected: CGFloat, cards: Int = 10) -> CGFloat {
        FeedPaging.restingOffset(start: start, projected: projected, page: page, content: page * CGFloat(cards))
    }

    func testAShortDragWithNoSpeedGoesToTheNextCard() {
        // The owner's case: a 90-point drag that ends at rest. View-aligned paging sent this back to card 0.
        XCTAssertEqual(rest(start: 0, projected: 90), 800)
        XCTAssertEqual(rest(start: 800, projected: 800 + 40), 1600)
        XCTAssertEqual(rest(start: 1600, projected: 1600 - 90), 800)
    }

    func testATwitchStaysPut() {
        XCTAssertEqual(rest(start: 800, projected: 800 + 20), 800)
        XCTAssertEqual(rest(start: 800, projected: 800 - 36), 800)
    }

    func testAHardFlickStillMovesOnlyOneCard() {
        XCTAssertEqual(rest(start: 800, projected: 800 + 5000), 1600)
        XCTAssertEqual(rest(start: 800, projected: 800 - 5000), 0)
    }

    func testTheEndsAreNotOvershot() {
        XCTAssertEqual(rest(start: 0, projected: -300), 0, "Pulling down on the first card (pull to refresh)")
        XCTAssertEqual(rest(start: 7200, projected: 7200 + 400), 7200, "Pushing up on the last card")
    }

    func testAGestureThatBeginsMidSettleCountsFromTheNearestCard() {
        XCTAssertEqual(rest(start: 300, projected: 400), 800)
        XCTAssertEqual(rest(start: 500, projected: 600), 1600)
        XCTAssertEqual(rest(start: 500, projected: 400), 0)
        XCTAssertEqual(rest(start: 300, projected: 310), 0, "Left alone, it settles on the nearest card")
    }

    func testAnUnsizedFeedIsLeftAlone() {
        XCTAssertEqual(FeedPaging.restingOffset(start: 0, projected: 123, page: 0, content: 0), 123)
    }
}
