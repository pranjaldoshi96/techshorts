import UserNotifications
import XCTest
@testable import Techgist

/// The PUSH CONTRACT as the app speaks it.
final class PushRegistrationTests: XCTestCase {
    private let base = URL(string: "https://push.example.com")!
    private let token = "00ab10ff"
    private var sent: [URLRequest] = []
    private var statuses: [Int?] = []

    override func setUp() {
        super.setUp()
        let name = "push-tests-\(UUID().uuidString)"
        PushRegistration.defaults = UserDefaults(suiteName: name)!
        PushRegistration.baseURL = base
        sent = []
        statuses = []
        PushRegistration.send = { [unowned self] request in
            self.sent.append(request)
            return self.statuses.isEmpty ? 200 : self.statuses.removeFirst()
        }
    }

    override func tearDown() {
        PushRegistration.baseURL = PushConfig.liveURL
        PushRegistration.defaults = .standard
        super.tearDown()
    }

    func testTheRegistryIsHTTPSAndTestRunsNeverUseIt() throws {
        let url = try XCTUnwrap(PushConfig.registerURL)
        XCTAssertEqual(url.scheme, "https")
        XCTAssertEqual(url.host, "techgist.213-210-36-218.sslip.io")
        XCTAssertEqual(url.path, "")
        XCTAssertNil(PushConfig.liveURL, "A test run must not register its simulator with the real registry")
    }

    func testPushIsOffWithoutARegistry() async {
        PushRegistration.baseURL = nil
        await PushRegistration.register(token: token)
        await PushRegistration.unregister()
        XCTAssertTrue(sent.isEmpty)
        XCTAssertFalse(PushRegistration.isRegistered)
    }

    func testRegisterPostsTheContractBody() async throws {
        await PushRegistration.register(token: token)
        let request = try XCTUnwrap(sent.first)
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.url?.absoluteString, "https://push.example.com/v1/devices")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: try XCTUnwrap(request.httpBody)) as? [String: Any])
        XCTAssertEqual(Set(body.keys), ["token", "env", "tz", "locale", "appVersion"])
        XCTAssertEqual(body["token"] as? String, token)
        XCTAssertTrue(["sandbox", "production"].contains(try XCTUnwrap(body["env"] as? String)))
        XCTAssertEqual(body["tz"] as? String, TimeZone.current.identifier)
        XCTAssertNotNil(TimeZone(identifier: try XCTUnwrap(body["tz"] as? String)))
        let locale = try XCTUnwrap(body["locale"] as? String)
        XCTAssertEqual(locale, PushRegistration.localeTag(Locale.current.identifier))
        XCTAssertNotNil(locale.range(of: "^[A-Za-z0-9_-]{1,35}$", options: .regularExpression), locale)
        XCTAssertEqual(body["appVersion"] as? String, Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String)
        XCTAssertEqual(PushRegistration.registeredToken, token)
        XCTAssertTrue(PushRegistration.isRegistered)
    }

    func testLocaleIsSentWithoutIOSSettings() {
        XCTAssertEqual(PushRegistration.localeTag("en_US@rg=inzzzz"), "en_US")   // a region override, as on an Indian iPhone set to US English
        XCTAssertEqual(PushRegistration.localeTag("en_IN@calendar=gregorian;numbers=latn"), "en_IN")
        XCTAssertEqual(PushRegistration.localeTag("hi_IN"), "hi_IN")
        XCTAssertEqual(PushRegistration.localeTag("zh-Hant_TW"), "zh-Hant_TW")
    }

    func testEveryLaunchRegistersAgain() async {
        await PushRegistration.register(token: token)
        await PushRegistration.register(token: token)
        XCTAssertEqual(sent.map(\.httpMethod), ["POST", "POST"])
    }

    func testAFailedRegistrationIsNotRemembered() async {
        statuses = [500]
        await PushRegistration.register(token: token)
        XCTAssertNil(PushRegistration.registeredToken)
        statuses = [nil]
        await PushRegistration.register(token: token)
        XCTAssertNil(PushRegistration.registeredToken)
    }

    func testANewTokenReplacesTheOldOne() async {
        await PushRegistration.register(token: token)
        await PushRegistration.register(token: "beef01")
        XCTAssertEqual(sent.map(\.httpMethod), ["POST", "DELETE", "POST"])
        XCTAssertEqual(sent[1].url?.absoluteString, "https://push.example.com/v1/devices/00ab10ff")
        XCTAssertEqual(PushRegistration.registeredToken, "beef01")
    }

    func testOptingOutDeletesTheDevice() async {
        await PushRegistration.register(token: token)
        await PushRegistration.unregister()
        XCTAssertEqual(sent.last?.httpMethod, "DELETE")
        XCTAssertEqual(sent.last?.url?.absoluteString, "https://push.example.com/v1/devices/00ab10ff")
        XCTAssertNil(sent.last?.httpBody)
        XCTAssertNil(PushRegistration.registeredToken)
        // Nothing registered, nothing to delete.
        await PushRegistration.unregister()
        XCTAssertEqual(sent.count, 2)
    }

    func testAnOptOutThatCannotReachTheServerIsTriedAgain() async {
        await PushRegistration.register(token: token)
        statuses = [nil]
        await PushRegistration.unregister()
        XCTAssertEqual(PushRegistration.registeredToken, token, "Still registered as far as the server knows")
        statuses = [404]
        await PushRegistration.unregister()
        XCTAssertNil(PushRegistration.registeredToken)
    }

    func testTokensOnlyTravelOverHTTPSAndMustBeHex() {
        XCTAssertNil(PushRegistration.registerRequest(base: URL(string: "http://push.example.com")!, token: token))
        XCTAssertNil(PushRegistration.registerRequest(base: base, token: "../admin"))
        XCTAssertNil(PushRegistration.unregisterRequest(base: base, token: ""))
        XCTAssertEqual(PushRegistration.registerRequest(base: URL(string: "https://push.example.com/")!, token: token)?.url?.absoluteString,
                       "https://push.example.com/v1/devices")
    }

    func testPushNeedsBothTheSwitchAndThePermission() {
        XCTAssertTrue(PushRegistration.wantsPush(alertsOn: true, status: .authorized))
        XCTAssertTrue(PushRegistration.wantsPush(alertsOn: true, status: .provisional))
        XCTAssertFalse(PushRegistration.wantsPush(alertsOn: false, status: .authorized))
        XCTAssertFalse(PushRegistration.wantsPush(alertsOn: true, status: .denied))
        XCTAssertFalse(PushRegistration.wantsPush(alertsOn: true, status: .notDetermined))
    }

    func testAPushPayloadFindsItsStoryByEventIdOrLink() throws {
        // The payload of the PUSH CONTRACT, as iOS hands it to the app.
        let payload: [AnyHashable: Any] = [
            "aps": ["alert": ["title": "T", "body": "B"], "sound": "techgist.caf", "mutable-content": 1, "thread-id": "ev_2"],
            "eventId": "ev_2", "url": "https://example.com/moved", "image": "https://example.com/p.jpg",
        ]
        XCTAssertEqual(StoryNotification.eventId(from: payload), "ev_2")
        XCTAssertEqual(StoryNotification.storyURL(from: payload)?.absoluteString, "https://example.com/moved")
        func story(_ name: String, event: String) -> Article {
            Article(url: URL(string: "https://example.com/\(name)")!, title: name, excerpt: "", content: "", source: "Wired",
                    category: .ai, publishedAt: Date(), eventId: event)
        }
        let stories = [story("one", event: "ev_1"), story("two", event: "ev_2")]
        // The link in the push isn't in the edition (the lead outlet changed); the event id still finds the card.
        XCTAssertEqual(StoryNotification.story(url: StoryNotification.storyURL(from: payload), eventId: "ev_2", in: stories)?.title, "two")
        XCTAssertEqual(StoryNotification.story(url: URL(string: "https://example.com/one"), eventId: "ev_2", in: stories)?.title, "one")
        XCTAssertNil(StoryNotification.story(url: nil, eventId: "ev_9", in: stories))
        XCTAssertNil(StoryNotification.eventId(from: ["eventId": ""]))
    }

    @MainActor func testTheStoreFocusesOnAPushedEvent() {
        func story(_ name: String, event: String, rank: Int) -> Article {
            Article(url: URL(string: "https://example.com/\(name)")!, title: name, excerpt: "", content: "", source: "Wired",
                    category: .dev, publishedAt: Date(), rank: rank, eventId: event)
        }
        let cache = ArticleCache(updatedAt: Date(), articles: [story("one", event: "ev_1", rank: 0), story("two", event: "ev_2", rank: 1)])
        let store = NewsStore(readState: ReadState(readAt: [:]), cache: cache, service: { _ in ([], [], .direct) })
        XCTAssertTrue(store.focus(url: URL(string: "https://example.com/gone"), eventId: "ev_2"))
        XCTAssertEqual(store.category, .dev)
        XCTAssertEqual(store.focus?.id, "https://example.com/two")
        XCTAssertFalse(store.focus(url: nil, eventId: "ev_404"))
        XCTAssertFalse(store.focus(url: nil, eventId: nil))
    }

    func testLocalAlertsCarryTheEventIdAndTheServersAlertText() async {
        var article = Article(url: URL(string: "https://example.com/a")!, title: "Publisher title", excerpt: "", content: "", source: "Wired",
                              category: .top, publishedAt: Date(), headline: "Card headline", summary: "First. Second.", eventId: "ev_7")
        article.notification = AlertText(title: "Alert title", body: "Alert body.")
        let content = await StoryNotification.content(for: article, kind: .breaking)
        XCTAssertEqual(content.title, "Alert title")
        XCTAssertEqual(content.body, "Alert body.")
        XCTAssertEqual(StoryNotification.eventId(from: content.userInfo), "ev_7")
    }
}
