import XCTest
@testable import Techgist

/// The rule that decides which card the feed rests on after a swipe.
final class FeedPagingTests: XCTestCase {
    private let page: CGFloat = 800
    private func rest(start: CGFloat, projected: CGFloat, cards: Int = 10, heading: CGFloat? = nil) -> CGFloat {
        FeedPaging.restingOffset(start: start, projected: projected, page: page, content: page * CGFloat(cards), heading: heading)
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

    // MARK: Quick flicks, one after another

    func testAFlickMadeWhileTheFeedIsStillMovingGoesOnePastTheCardItWasHeadingTo() {
        // Card 6 to 7 has just begun (a quarter of the way) when the next flick lands. It used to count from
        // card 6 and arrive at 7 again.
        XCTAssertEqual(rest(start: 6 * 800 + 200, projected: 6 * 800 + 600, heading: 7 * 800), 8 * 800)
        // The same on the way back up.
        XCTAssertEqual(rest(start: 6 * 800 - 200, projected: 6 * 800 - 600, heading: 5 * 800), 4 * 800)
    }

    /// Flicks one after another, each beginning when the feed has covered `progress` of the way to its heading.
    private func flick(_ count: Int, from card: CGFloat, progress: CGFloat, gap: TimeInterval = 0.2, memory: FeedPaging.Memory = .init()) -> CGFloat? {
        var offset = card * page, now: TimeInterval = 100
        for _ in 0..<count {
            let target = FeedPaging.restingOffset(start: offset, projected: offset + 500, page: page, content: page * 30,
                                                  heading: memory.heading(forGestureAt: offset, now: now))
            memory.head(to: target, now: now)
            offset += (target - offset) * progress
            now += gap
        }
        return memory.heading
    }

    func testQuickFlicksMoveOneCardEach() {
        // The review's cases: flicks 0.1, 0.2 and 0.3 s apart moved 1 or 2 cards instead of 3 and 4.
        XCTAssertEqual(flick(3, from: 6, progress: 0.1, gap: 0.1), 9 * 800)
        XCTAssertEqual(flick(3, from: 6, progress: 0.3), 9 * 800)
        XCTAssertEqual(flick(4, from: 10, progress: 0.45, gap: 0.3), 14 * 800)
        XCTAssertEqual(flick(3, from: 7, progress: 1, gap: 0.5), 10 * 800, "Flicks that wait for the feed to settle")
    }

    func testAskingTwiceAboutOneGestureGivesOneAnswer() {
        let memory = FeedPaging.Memory()
        memory.head(to: 7 * 800, now: 100)
        let start: CGFloat = 6 * 800 + 200
        for _ in 0..<3 {
            memory.head(to: FeedPaging.restingOffset(start: start, projected: start + 500, page: page, content: page * 20,
                                                     heading: memory.heading(forGestureAt: start, now: 100.2)), now: 100.2)
        }
        XCTAssertEqual(memory.heading, 8 * 800)
    }

    func testAnOldHeadingIsForgotten() {
        let memory = FeedPaging.Memory()
        memory.head(to: 7 * 800, now: 100)
        XCTAssertEqual(memory.heading(forGestureAt: 6 * 800 + 200, now: 100.3), 7 * 800)
        XCTAssertNil(memory.heading(forGestureAt: 6 * 800 + 300, now: 102), "Two seconds on, the feed is there for some other reason")
    }

    func testTurningBackMidWayReturnsToTheCardBeingLeft() {
        XCTAssertEqual(rest(start: 6 * 800 + 200, projected: 6 * 800 + 100, heading: 7 * 800), 6 * 800)
        XCTAssertEqual(rest(start: 6 * 800 + 200, projected: 6 * 800 + 210, heading: 7 * 800), 6 * 800,
                       "A touch that stops the feed and goes nowhere leaves it on the nearest card")
    }

    func testALayoutChangeIsNotAGesture() {
        // SwiftUI asks again when the text size changes, with nothing swiped and the feed a few points off a
        // card. A heading left over from the last swipe must not send the feed anywhere (it hung the
        // accessibility audit, which changes the text size right after a swipe).
        XCTAssertEqual(rest(start: 5.67, projected: 5.67, heading: 706.67), 0)
        XCTAssertEqual(rest(start: 800 - 3, projected: 800 - 3, heading: 800), 800)
    }

    func testAHeadingIsIgnoredAtRestAndWhenOutOfReach() {
        XCTAssertEqual(rest(start: 800, projected: 800 + 90, heading: 7 * 800), 1600, "At rest: one card from here")
        XCTAssertEqual(rest(start: 300, projected: 400, heading: 7 * 800), 800, "After a jump to the top, a far-off heading means nothing")
        XCTAssertEqual(rest(start: 6 * 800 + 200, projected: 9000, cards: 8, heading: 7 * 800), 7 * 800, "Never past the last card")
    }

    func testAnUnsizedFeedIsLeftAlone() {
        XCTAssertEqual(FeedPaging.restingOffset(start: 0, projected: 123, page: 0, content: 0), 123)
    }
}
