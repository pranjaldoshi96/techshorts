import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var store: NewsStore
    @ObservedObject private var readState = ReadState.shared
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    /// The one sheet on screen. A single value, so two sheets can never compete to present.
    @State private var sheet: Sheet?
    /// The card on screen in each tab, so a tab comes back where it was left.
    @State private var positions: [Category: String] = [:]
    @State private var pushEdge: Edge = .trailing
    @State private var highlighted: String?
    @State private var pillHidden = false
    @State private var viewedThisSession = Set<String>()
    @AppStorage(Keys.notificationsOffered) private var notificationsOffered = false
    /// Shown once to people who turned big story alerts on in 1.x, before anything is sent to the server.
    @State private var serverAlertsNotice = false

    enum Sheet: Identifiable {
        case saved, settings, search
        case coverage(Article)

        var id: String {
            switch self {
            case .saved: "saved"
            case .settings: "settings"
            case .search: "search"
            case .coverage(let article): "coverage-" + article.id
            }
        }
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                ChipBar(selection: tabSelection, unread: unreadTabs)
                ProgressLine(fraction: progress)
                if let banner {
                    StateBanner(text: banner.text, symbol: banner.symbol)
                }
                ZStack {
                    FeedView(category: store.category, items: store.visibleArticles, position: position(for: store.category),
                             highlighted: highlighted, onRead: read, onCoverage: { sheet = .coverage($0) },
                             onViewed: viewed, onSaved: { sheet = .saved }, onSwipeTab: swipeTab)
                        .id(store.category)
                        .transition(reduceMotion ? .opacity : .push(from: pushEdge))
                }
                .overlay(alignment: .top) {
                    if store.waitingCount > 0, !pillHidden {
                        NewStoriesPill(count: store.waitingCount) {
                            withAnimation(reduceMotion ? nil : .default) { store.showWaitingStories() }
                        }
                        .padding(.top, 12)
                        .transition(reduceMotion ? .opacity : .move(edge: .top).combined(with: .opacity))
                    }
                }
                .animation(reduceMotion ? .easeInOut(duration: 0.2) : .spring(response: 0.4, dampingFraction: 0.8),
                           value: store.waitingCount > 0 && !pillHidden)
                .sensoryFeedback(.impact(weight: .light), trigger: store.waitingCount > 0 && !pillHidden) { _, shown in shown }
            }
            .background(Theme.cardBackground)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button(action: scrollToTop) {
                        HStack(spacing: 8) {
                            Image("Logo")
                                .resizable()
                                .frame(width: 26, height: 26)
                                .clipShape(.rect(cornerRadius: 6))
                            Text("Techgist")
                                .font(.system(.headline, design: .serif).weight(.bold))
                                .foregroundStyle(.primary)
                                .fixedSize()
                        }
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Techgist")
                    .accessibilityHint("Goes back to the first story")
                }
                ToolbarItemGroup(placement: .topBarTrailing) {
                    Button { sheet = .search } label: { Image(systemName: "magnifyingglass") }
                        .accessibilityLabel("Search")
                    Button { sheet = .saved } label: { Image(systemName: "bookmark") }
                        .accessibilityLabel("Saved stories")
                    Button { sheet = .settings } label: { Image(systemName: "gearshape") }
                        .accessibilityLabel("Settings")
                }
            }
            .sheet(item: $sheet) { sheet in
                Group {
                    switch sheet {
                    case .saved: SavedView()
                    case .settings: SettingsView()
                    case .search: SearchView(onSelect: show)
                    case .coverage(let article): CoverageSheet(article: article, onRead: read)
                    }
                }
                .tint(Theme.accent)
            }
        }
        .task {
            serverAlertsNotice = await PushRegistration.noticeIsDue()
            await askForNotificationsOnce()
        }
        .alert("Big story alerts now come from Techgist's server", isPresented: $serverAlertsNotice) {
            Button("Keep alerts on") { Task { await PushRegistration.answerNotice(keep: true) } }
            Button("Turn off alerts") { Task { await PushRegistration.answerNotice(keep: false) } }
        } message: {
            Text("So alerts arrive as the news breaks, Techgist's server keeps this phone's notification address, time zone and language. Nothing else, and it is deleted when you turn alerts off.")
        }
        .task {
            // While the app is open, look for a newer edition every 5 minutes and offer it with the pill.
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(300))
                await store.checkForNewStories()
            }
        }
        .task(id: store.waitingCount) {
            // The pill steps aside after 8 seconds; the stories still arrive on the next refresh or tab change.
            pillHidden = false
            guard store.waitingCount > 0 else { return }
            try? await Task.sleep(for: .seconds(8))
            if !Task.isCancelled { pillHidden = true }
        }
        .onChange(of: store.scrollToTopRequests) {
            positions = [:]
            scrollToTop()
        }
        .onChange(of: store.focus) { _, focus in
            guard let focus else { return }
            positions[focus.category] = focus.id
            highlighted = focus.id
            Task {
                try? await Task.sleep(for: .seconds(1.6))
                if highlighted == focus.id { highlighted = nil }
            }
        }
        .onAppear {
            AppDelegate.handle = handle
            if let request = AppDelegate.takePending() { handle(request) }
        }
    }

    // MARK: Tabs and position

    private var tabSelection: Binding<Category> {
        Binding(get: { store.category }, set: { new in
            let order = Category.allCases
            pushEdge = (order.firstIndex(of: new) ?? 0) >= (order.firstIndex(of: store.category) ?? 0) ? .trailing : .leading
            if new == store.category { scrollToTop() } else { store.category = new }
        })
    }

    /// A sideways swipe on the feed moves one tab over; at either end it does nothing.
    private func swipeTab(_ step: Int) {
        let order = Category.allCases
        guard let now = order.firstIndex(of: store.category), order.indices.contains(now + step) else { return }
        withAnimation(reduceMotion ? nil : .snappy(duration: 0.25)) { tabSelection.wrappedValue = order[now + step] }
    }

    private func position(for category: Category) -> Binding<String?> {
        Binding(get: { positions[category] }, set: { positions[category] = $0 })
    }

    private func scrollToTop() {
        guard let first = store.visibleArticles.first else { return }
        withAnimation(reduceMotion ? nil : .default) { positions[store.category] = first.id }
    }

    private var progress: Double {
        let items = store.visibleArticles
        guard !items.isEmpty else { return 0 }
        guard let id = positions[store.category] else { return 1 / Double(items.count) }
        if id == FeedView.endID { return 1 }
        let index = items.firstIndex { $0.id == id } ?? 0
        return Double(index + 1) / Double(items.count)
    }

    private var unreadTabs: Set<Category> {
        Set(Category.allCases.filter { tab in
            store.articles(in: tab).contains { !readState.isRead($0.id) }
        })
    }

    private var banner: (text: String, symbol: String)? {
        guard !store.articles.isEmpty else { return nil }
        if store.isOffline, let updated = store.lastUpdated {
            return ("Offline · stories from \(RelativeTime.spoken(updated))", "wifi.slash")
        }
        switch store.origin {
        case .direct:
            return ("Summaries catching up · showing headlines", "clock.arrow.circlepath")
        case .edition(_, degraded: true):
            return ("Summaries catching up · showing headlines", "clock.arrow.circlepath")
        case .edition(let publishedAt, _) where Date().timeIntervalSince(publishedAt) > 2 * 3600:
            return ("Updated \(RelativeTime.spoken(publishedAt))", "clock")
        default:
            return nil
        }
    }

    // MARK: Actions

    private func read(_ article: Article) {
        readState.markRead(article.id)
        Reader.open(article.url)
    }

    private func viewed(_ article: Article) {
        readState.markRead(article.id)
        viewedThisSession.insert(article.id)
    }

    /// On the first launch iOS asks whether Techgist may send notifications. Yes turns on the morning gist and
    /// big story alerts; no leaves both off. Either can be changed in Settings afterwards. Asked once.
    private func askForNotificationsOnce() async {
        guard !notificationsOffered, !serverAlertsNotice else { return }
        notificationsOffered = true
        let granted = await StoryNotification.requestPermission()
        UserDefaults.standard.set(granted, forKey: Keys.digestEnabled)
        UserDefaults.standard.set(granted, forKey: Keys.breakingNews)
        if granted {
            PushRegistration.markAnswered()
            await DailyDigest.reschedule(with: store.topStory)
        }
        await PushRegistration.sync()
    }

    /// Brings a story's card on screen, or opens the story when it isn't in the edition any more.
    private func show(_ article: Article) {
        if !store.focus(on: article.url) { read(article) }
    }

    private func handle(_ request: AppDelegate.Request) {
        sheet = nil
        switch request {
        case .show(let url, let eventId):
            // The card first; the full story is one tap away, as everywhere else. Only a story that has left
            // the edition (an old alert) opens its page, since there is no card to show.
            Task {
                if await !store.showStory(url: url, eventId: eventId), let url { Reader.open(url) }
            }
        case .read(let url):
            store.focus(on: url)
            readState.markRead(url.absoluteString)
            Reader.open(url)
        }
    }
}
