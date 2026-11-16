import SafariServices
import SwiftUI

extension UIApplication {
    /// The view controller on top, for presenting UIKit screens from anywhere in the paged feed.
    @MainActor var topViewController: UIViewController? {
        let scene = connectedScenes.compactMap { $0 as? UIWindowScene }.first { $0.activationState == .foregroundActive }
            ?? connectedScenes.compactMap { $0 as? UIWindowScene }.first
        var top = scene?.keyWindow?.rootViewController
        while let presented = top?.presentedViewController { top = presented }
        return top
    }
}

/// The full story in in-app Safari, as a page sheet, in Reader mode when the page supports it. It shares Safari's
/// logins and content blockers, and keeps people in the app.
enum Reader {
    static var usesReaderMode: Bool { UserDefaults.standard.object(forKey: Keys.readerMode) as? Bool ?? true }

    @MainActor static func open(_ url: URL) {
        guard url.scheme == "https" || url.scheme == "http",
              let top = UIApplication.shared.topViewController else { return }
        // A sheet may be on its way in or out (a result tapped in Search, an alert tapped while Settings is open).
        // Presenting during that transition fails, so wait for it to finish.
        if let transition = top.transitionCoordinator ?? top.presentingViewController?.transitionCoordinator {
            transition.animate(alongsideTransition: nil) { _ in open(url) }
            return
        }
        guard !(top is SFSafariViewController) else { return }
        let configuration = SFSafariViewController.Configuration()
        configuration.entersReaderIfAvailable = usesReaderMode
        let safari = SFSafariViewController(url: url, configuration: configuration)
        safari.preferredControlTintColor = UIColor(named: "AccentColor")
        safari.modalPresentationStyle = .pageSheet
        top.present(safari, animated: true)
    }
}

/// A compact row for lists of stories: thumbnail, headline, source and age.
struct StoryRow: View {
    let article: Article

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            StoryImage(article: article, url: article.thumbImage, maxPixel: ImageLoader.thumbPixels)
                .frame(width: 56, height: 56)
                .clipShape(.rect(cornerRadius: 8))
            VStack(alignment: .leading, spacing: 4) {
                Text(article.displayTitle)
                    .font(.system(.headline, design: .serif))
                    .foregroundStyle(.primary)
                Text("\(article.source) · \(RelativeTime.short(article.publishedAt))")
                    .font(.caption)
                    .foregroundStyle(Theme.metaText)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(article.displayTitle). \(article.source), \(RelativeTime.spoken(article.publishedAt))")
        .accessibilityAddTraits(.isButton)
    }
}

struct SavedView: View {
    @EnvironmentObject private var store: NewsStore
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""
    @State private var confirmClear = false

    private var results: [Article] {
        query.isEmpty ? store.saved : store.saved.filter { StorySearch.matches($0, query) }
    }

    var body: some View {
        NavigationStack {
            Group {
                if store.saved.isEmpty {
                    ContentUnavailableView("Nothing saved", systemImage: "bookmark",
                                           description: Text("Tap the bookmark on a story to keep it here.").foregroundStyle(Theme.metaText))
                } else {
                    List {
                        ForEach(results) { article in
                            Button { Reader.open(article.url) } label: { StoryRow(article: article) }
                                .swipeActions {
                                    Button("Remove", role: .destructive) { store.toggleSaved(article) }
                                }
                        }
                    }
                    .listStyle(.plain)
                    .searchable(text: $query, prompt: "Search saved stories")
                    .overlay {
                        if results.isEmpty { ContentUnavailableView.search(text: query) }
                    }
                }
            }
            .navigationTitle("Saved")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
                if !store.saved.isEmpty {
                    ToolbarItem(placement: .topBarLeading) {
                        Button("Clear all", role: .destructive) { confirmClear = true }
                    }
                }
            }
            .confirmationDialog("Remove all saved stories?", isPresented: $confirmClear, titleVisibility: .visible) {
                Button("Remove all", role: .destructive) { store.clearSaved() }
            }
        }
    }
}

/// Matches a story against typed words: every word must appear in its headline, summary or outlets.
enum StorySearch {
    static func matches(_ article: Article, _ query: String) -> Bool {
        let haystack = ([article.displayTitle, article.title, article.displaySummary, article.source]
            + (article.alsoCoveredBy ?? [])).joined(separator: " ")
        return query.split(separator: " ").allSatisfy { haystack.localizedCaseInsensitiveContains($0) }
    }

    /// Stories in the edition and in Saved that match, edition first, no duplicates.
    static func results(for query: String, in articles: [Article], saved: [Article]) -> [Article] {
        let words = query.trimmingCharacters(in: .whitespaces)
        guard !words.isEmpty else { return [] }
        var seen = Set<String>()
        return (articles + saved).filter { matches($0, words) && seen.insert($0.id).inserted }
    }
}

/// Searches everything on this phone: the current edition's tabs and Saved.
struct SearchView: View {
    @EnvironmentObject private var store: NewsStore
    @Environment(\.dismiss) private var dismiss
    let onSelect: (Article) -> Void
    @State private var query = ""
    /// The field is focused as the sheet opens, so typing works straight away.
    @State private var searching = true

    var body: some View {
        let results = StorySearch.results(for: query, in: store.articles, saved: store.saved)
        NavigationStack {
            List(results) { article in
                Button {
                    dismiss()
                    onSelect(article)
                } label: { StoryRow(article: article) }
            }
            .listStyle(.plain)
            .overlay {
                if query.trimmingCharacters(in: .whitespaces).isEmpty {
                    ContentUnavailableView("Search stories", systemImage: "magnifyingglass",
                                           description: Text("Find a story by headline, summary or outlet.").foregroundStyle(Theme.metaText))
                } else if results.isEmpty {
                    ContentUnavailableView.search(text: query)
                }
            }
            .searchable(text: $query, isPresented: $searching, placement: .navigationBarDrawer(displayMode: .always),
                        prompt: "Headline, topic or outlet")
            .navigationTitle("Search")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
        }
    }
}

/// Every outlet reporting a story.
struct CoverageSheet: View {
    @Environment(\.dismiss) private var dismiss
    @Environment(\.dynamicTypeSize) private var typeSize
    let article: Article
    let onRead: (Article) -> Void

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Button {
                        dismiss()
                        onRead(article)
                    } label: {
                        HStack(spacing: 12) {
                            SourceMonogram(name: article.source, size: 32)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(article.source).font(.body.weight(.semibold)).foregroundStyle(.primary)
                                Text(article.title).font(.footnote).foregroundStyle(Theme.metaText)
                            }
                            Spacer(minLength: 0)
                            Image(systemName: "chevron.right").imageScale(.small).foregroundStyle(Theme.metaText)
                        }
                    }
                    ForEach(article.alsoCoveredBy ?? [], id: \.self) { name in
                        HStack(spacing: 12) {
                            SourceMonogram(name: name, size: 32)
                            Text(name)
                        }
                    }
                } header: {
                    Text("Covered by \(article.coverage) outlets · first reported \(RelativeTime.spoken(article.publishedAt)) by \(article.source)")
                        .textCase(nil)
                        .foregroundStyle(Theme.metaText)
                }
                if let points = article.points {
                    Section {
                        Label("\(points) points on Hacker News", systemImage: "arrow.up.circle")
                    }
                }
            }
            .navigationTitle("Full coverage")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
        }
        .presentationDetents(typeSize.isAccessibilitySize ? [.large] : [.medium, .large])
        // Opaque: on a see-through sheet the story behind shows through the list.
        .presentationBackground(Color(.systemGroupedBackground))
    }
}

struct SettingsView: View {
    @EnvironmentObject private var store: NewsStore
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL
    @AppStorage(Keys.digestEnabled) private var digestEnabled = false
    @AppStorage(Keys.digestMinute) private var digestMinute = 8 * 60
    @AppStorage(Keys.breakingNews) private var breakingEnabled = false
    @AppStorage(Keys.readerMode) private var readerMode = true
    @AppStorage(AlertSound.key) private var alertSound = AlertSound.flash.rawValue
    @State private var showDeniedAlert = false
    @State private var sampleSent = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Toggle("Morning gist", isOn: digestBinding)
                    if digestEnabled {
                        DatePicker("Time", selection: timeBinding, displayedComponents: .hourAndMinute)
                    }
                    Toggle("Big story alerts", isOn: breakingBinding)
                    if digestEnabled || breakingEnabled {
                        Picker("Alert sound", selection: $alertSound) {
                            ForEach(AlertSound.allCases) { Text($0.title).tag($0.rawValue) }
                        }
                        .onChange(of: alertSound) { _, new in
                            // Let them hear it, and tell the server which one this phone wants.
                            AlertSound(rawValue: new)?.preview()
                            Task { await PushRegistration.sync() }
                        }
                    }
                    if digestEnabled || breakingEnabled, let story = store.topStory {
                        Button(sampleSent ? "Preview on its way" : "Preview an alert") {
                            sampleSent = true
                            Task { await StoryNotification.sendSample(story) }
                        }
                        .disabled(sampleSent)
                    }
                } header: {
                    Text("Notifications").foregroundStyle(Theme.metaText)
                } footer: {
                    Text((PushConfig.registerURL == nil
                          ? "One notification each morning with the top story, and an alert when a story is covered by several outlets at once, at most 3 a day. iOS decides when the app checks in the background, so alerts can arrive a few hours after a story breaks."
                          : "One notification each morning with the top story, and an alert from Techgist when a big story breaks, at most a few a day. Turning alerts off removes this phone from Techgist's alert list.")
                         + (digestEnabled || breakingEnabled ? " The preview arrives 5 seconds after you ask for it." : ""))
                        .foregroundStyle(Theme.metaText)
                }

                Section {
                    Toggle("Open stories in Reader view", isOn: $readerMode)
                } header: {
                    Text("Reading").foregroundStyle(Theme.metaText)
                } footer: {
                    Text("Shows the publisher's story as clean text when the page supports it.").foregroundStyle(Theme.metaText)
                }

                Section {
                    NavigationLink {
                        SourcesView()
                    } label: {
                        LabeledContent("Sources", value: store.disabledSources.isEmpty ? "All on" : "\(store.disabledSources.count) muted")
                    }
                } footer: {
                    Text("Muted sources don't appear in any tab. You can also mute a source from the More button on a story.")
                        .foregroundStyle(Theme.metaText)
                }

                Section {
                    LabeledContent("Version", value: Self.version)
                    switch store.origin {
                    case .edition(let publishedAt, _):
                        storiesRow("Techgist edition, \(RelativeTime.spoken(publishedAt))")
                    case .direct:
                        storiesRow("Direct from publishers")
                    case nil:
                        EmptyView()
                    }
                    Link("Privacy Policy", destination: Self.privacyURL)
                    Link("Support", destination: Self.supportURL)
                } header: {
                    Text("About").foregroundStyle(Theme.metaText)
                } footer: {
                    Text("Summaries marked AI are written by Techgist's model from the publisher's story and checked against it. Every card links to the original. Your reading, saves and settings stay on this device."
                         + (PushConfig.registerURL == nil ? "" : " With Big story alerts on, Techgist's server holds this phone's notification address, time zone and language so it can send them, and nothing else."))
                        .foregroundStyle(Theme.metaText)
                }
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
            .alert("Notifications are off", isPresented: $showDeniedAlert) {
                Button("Open Settings") {
                    if let url = URL(string: UIApplication.openNotificationSettingsURLString) { openURL(url) }
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("Allow notifications for Techgist in Settings to get the morning gist and big story alerts.")
            }
        }
    }

    /// Where the stories came from. Stacked, so the long value never squeezes its label at any text size.
    private func storiesRow(_ value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("Stories")
            Text(value).font(.subheadline).foregroundStyle(Theme.metaText)
        }
        .accessibilityElement(children: .combine)
    }

    private var digestBinding: Binding<Bool> {
        Binding(get: { digestEnabled }, set: { on in
            Task {
                if on {
                    let granted = await DailyDigest.enable()
                    digestEnabled = granted
                    showDeniedAlert = !granted
                } else {
                    digestEnabled = false
                }
                await DailyDigest.reschedule(with: store.topStory)
            }
        })
    }

    private var breakingBinding: Binding<Bool> {
        Binding(get: { breakingEnabled }, set: { on in
            Task {
                if on {
                    let granted = await BreakingNews.enable()
                    breakingEnabled = granted
                    showDeniedAlert = !granted
                } else {
                    breakingEnabled = false
                    await BreakingNews.disable()
                }
            }
        })
    }

    private var timeBinding: Binding<Date> {
        Binding(get: {
            Calendar.current.date(bySettingHour: digestMinute / 60, minute: digestMinute % 60, second: 0, of: Date()) ?? Date()
        }, set: { date in
            let c = Calendar.current.dateComponents([.hour, .minute], from: date)
            digestMinute = (c.hour ?? 8) * 60 + (c.minute ?? 0)
            Task { await DailyDigest.reschedule(with: store.topStory) }
        })
    }

    static let privacyURL = URL(string: "https://pranjaldoshi96.github.io/apps/techgist/privacy/")!
    static let supportURL = URL(string: "https://pranjaldoshi96.github.io/apps/techgist/support/")!

    private static var version: String {
        let info = Bundle.main.infoDictionary
        return "\(info?["CFBundleShortVersionString"] as? String ?? "1.0") (\(info?["CFBundleVersion"] as? String ?? "1"))"
    }
}

/// Every source in the current edition, with a switch to mute it.
struct SourcesView: View {
    @EnvironmentObject private var store: NewsStore

    private var names: [String] {
        Set(store.articles.map(\.source)).union(FeedSource.all.map(\.name)).union(store.disabledSources)
            .sorted { $0.localizedCaseInsensitiveCompare($1) == .orderedAscending }
    }

    var body: some View {
        List {
            if !store.disabledSources.isEmpty {
                Section {
                    Button("Unmute all") { store.disabledSources = [] }
                }
            }
            Section {
                ForEach(names, id: \.self) { name in
                    Toggle(isOn: Binding(get: { !store.disabledSources.contains(name) }, set: { on in
                        if on { store.disabledSources.remove(name) } else { store.disabledSources.insert(name) }
                    })) {
                        HStack(spacing: 10) {
                            SourceMonogram(name: name, size: 26)
                            Text(name)
                        }
                    }
                }
            }
        }
        .navigationTitle("Sources")
        .navigationBarTitleDisplayMode(.inline)
    }
}
