import UIKit
import UserNotifications
import XCTest
@testable import Techgist

final class NotificationContentTests: XCTestCase {
    private func writeJPEG() throws -> URL {
        let image = UIGraphicsImageRenderer(size: CGSize(width: 720, height: 450)).image { context in
            UIColor.systemTeal.setFill()
            context.fill(CGRect(x: 0, y: 0, width: 720, height: 450))
        }
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("story-\(UUID().uuidString).jpg")
        try XCTUnwrap(image.jpegData(compressionQuality: 0.8)).write(to: url)
        return url
    }

    private func story(image: URL?) -> Article {
        Article(url: URL(string: "https://example.com/story-\(UUID().uuidString)")!, title: "Publisher title", excerpt: "", content: "",
                image: image, source: "Wired", category: .top, publishedAt: Date(), topRank: 0,
                headline: "Headline for the alert", summary: "First sentence. Second sentence.",
                alertLine: "A line written for the alert.")
    }

    func testAlertCarriesHeadlineLinePictureSoundAndActions() async throws {
        let content = await StoryNotification.content(for: story(image: try writeJPEG()), kind: .breaking)
        XCTAssertEqual(content.title, "Headline for the alert")
        XCTAssertEqual(content.body, "A line written for the alert.")
        XCTAssertEqual(content.categoryIdentifier, StoryNotification.category)
        XCTAssertEqual(content.threadIdentifier, "breaking")
        XCTAssertEqual(content.sound, UNNotificationSound(named: UNNotificationSoundName("techgist.caf")))
        XCTAssertEqual(content.attachments.count, 1)
        XCTAssertNotNil(StoryNotification.storyURL(from: content.userInfo))
    }

    func testAlertWithoutAPictureStillGoesOut() async {
        let content = await StoryNotification.content(for: story(image: nil), kind: .brief, subtitle: "Your morning gist")
        XCTAssertTrue(content.attachments.isEmpty)
        XCTAssertEqual(content.subtitle, "Your morning gist")
        XCTAssertEqual(content.threadIdentifier, "brief")
    }

    func testChimeIsBundledAndShort() throws {
        let url = try XCTUnwrap(Bundle.main.url(forResource: "techgist", withExtension: "caf"))
        let size = try XCTUnwrap(try url.resourceValues(forKeys: [.fileSizeKey]).fileSize)
        XCTAssertGreaterThan(size, 4_000)
        XCTAssertLessThan(size, 200_000)
    }

    func testPicturesAreDecodedAtTheRequestedSize() async throws {
        let loader = ImageLoader(directory: FileManager.default.temporaryDirectory.appendingPathComponent("images-\(UUID().uuidString)"))
        let url = try writeJPEG()
        XCTAssertNil(loader.cached(url, maxPixel: 360))
        let loaded = await loader.image(for: url, maxPixel: 360)
        let image = try XCTUnwrap(loaded)
        XCTAssertEqual(max(image.size.width, image.size.height), 360, accuracy: 1)
        // A second request is served from memory, synchronously.
        XCTAssertNotNil(loader.cached(url, maxPixel: 360))
        let missing = await loader.image(for: URL(fileURLWithPath: "/nonexistent/picture.jpg"), maxPixel: 360)
        XCTAssertNil(missing)
    }
}
