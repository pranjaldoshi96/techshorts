import SafariServices
import SwiftUI

/// In-app Safari, so reading the full story keeps people in the app and shares Safari's logins and content blockers.
struct SafariView: UIViewControllerRepresentable {
    let url: URL

    func makeUIViewController(context: Context) -> SFSafariViewController {
        SFSafariViewController(url: url)
    }

    func updateUIViewController(_ controller: SFSafariViewController, context: Context) {}
}

struct SavedView: View {
    @EnvironmentObject private var store: NewsStore
    @Environment(\.dismiss) private var dismiss
    @State private var reading: Article?

    var body: some View {
        NavigationStack {
            Group {
                if store.saved.isEmpty {
                    ContentUnavailableView("Nothing saved", systemImage: "bookmark",
                                           description: Text("Tap the bookmark on a story to keep it here."))
                } else {
                    List(store.saved) { article in
                        Button { reading = article } label: {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(article.title).font(.headline).foregroundStyle(.primary)
                                Text("\(article.source) · \(article.publishedAt.formatted(date: .abbreviated, time: .omitted))")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .swipeActions {
                            Button("Remove", role: .destructive) { store.toggleSaved(article) }
                        }
                    }
                }
            }
            .navigationTitle("Saved")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
            .sheet(item: $reading) { SafariView(url: $0.url).ignoresSafeArea() }
        }
    }
}

struct SettingsView: View {
    @EnvironmentObject private var store: NewsStore
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL
    @AppStorage(Keys.digestEnabled) private var digestEnabled = false
    @AppStorage(Keys.digestMinute) private var digestMinute = 8 * 60
    @State private var showDeniedAlert = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Toggle("Daily digest", isOn: digestBinding)
                    if digestEnabled {
                        DatePicker("Time", selection: timeBinding, displayedComponents: .hourAndMinute)
                    }
                } header: {
                    Text("Notifications")
                } footer: {
                    Text("One notification a day with the top tech story.")
                }

                Section {
                    Toggle("On-device summaries", isOn: $store.aiSummariesEnabled)
                        .disabled(!Summarizer.isSupported)
                } header: {
                    Text("Apple Intelligence")
                } footer: {
                    Text(Summarizer.statusText)
                }

                ForEach(Category.allCases) { category in
                    Section(category.rawValue) {
                        ForEach(FeedSource.all.filter { $0.category == category }) { source in
                            Toggle(source.name, isOn: sourceBinding(source))
                        }
                    }
                }

                Section {
                    LabeledContent("Version", value: Self.version)
                    switch store.origin {
                    case .edition(let publishedAt):
                        LabeledContent("Stories") {
                            Text("Techgist edition, \(publishedAt, format: .relative(presentation: .named))")
                        }
                    case .direct:
                        LabeledContent("Stories", value: "Direct from publishers")
                    case nil:
                        EmptyView()
                    }
                } footer: {
                    Text("Headlines and excerpts come from each publisher's public feed and link to the original story. Your reading, saves and settings stay on this device.")
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
                Text("Allow notifications for Techgist in Settings to get the daily digest.")
            }
        }
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

    private var timeBinding: Binding<Date> {
        Binding(get: {
            Calendar.current.date(bySettingHour: digestMinute / 60, minute: digestMinute % 60, second: 0, of: Date()) ?? Date()
        }, set: { date in
            let c = Calendar.current.dateComponents([.hour, .minute], from: date)
            digestMinute = (c.hour ?? 8) * 60 + (c.minute ?? 0)
            Task { await DailyDigest.reschedule(with: store.topStory) }
        })
    }

    private func sourceBinding(_ source: FeedSource) -> Binding<Bool> {
        Binding(get: { !store.disabledSources.contains(source.name) }, set: { on in
            if on { store.disabledSources.remove(source.name) } else { store.disabledSources.insert(source.name) }
        })
    }

    private static var version: String {
        let info = Bundle.main.infoDictionary
        return "\(info?["CFBundleShortVersionString"] as? String ?? "1.0") (\(info?["CFBundleVersion"] as? String ?? "1"))"
    }
}
