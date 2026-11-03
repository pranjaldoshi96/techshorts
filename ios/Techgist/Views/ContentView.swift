import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var store: NewsStore
    @State private var showSaved = false
    @State private var showSettings = false
    @State private var reading: Article?

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                CategoryBar(selection: $store.category)
                // Swipe left and right between tabs; up and down between stories.
                TabView(selection: $store.category) {
                    ForEach(Category.allCases) { category in
                        CategoryFeed(category: category) { reading = $0 }
                            .tag(category)
                    }
                }
                .tabViewStyle(.page(indexDisplayMode: .never))
                .animation(.default, value: store.category)
            }
            .background(Color(.systemGroupedBackground))
            .navigationTitle("Techgist")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button { showSettings = true } label: { Image(systemName: "gearshape") }
                        .accessibilityLabel("Settings")
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button { showSaved = true } label: { Image(systemName: "bookmark") }
                        .accessibilityLabel("Saved stories")
                }
            }
            .sheet(isPresented: $showSaved) { SavedView() }
            .sheet(isPresented: $showSettings) { SettingsView() }
            .sheet(item: $reading) { SafariView(url: $0.url).ignoresSafeArea() }
        }
        .task { await store.refreshIfStale() }
    }
}

/// One tab's stories, one card per screen.
struct CategoryFeed: View {
    @EnvironmentObject private var store: NewsStore
    let category: Category
    let onRead: (Article) -> Void

    var body: some View {
        let items = store.articles(in: category)
        if items.isEmpty {
            if store.isRefreshing || (store.lastUpdated == nil && store.failedSources.isEmpty) {
                ProgressView("Fetching today's stories…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ContentUnavailableView {
                    Label("No stories", systemImage: "newspaper")
                } description: {
                    Text(store.lastUpdated == nil ? "Couldn't reach the news sources. Check your connection." : "Nothing here right now.")
                } actions: {
                    Button("Try again") { Task { await store.refresh() } }
                }
                .frame(maxHeight: .infinity)
            }
        } else {
            ScrollView {
                LazyVStack(spacing: 0) {
                    ForEach(Array(items.enumerated()), id: \.element.id) { index, article in
                        CardView(article: article) { onRead(article) }
                            .containerRelativeFrame(.vertical)
                            .task { await store.prepare(article) }
                            .onAppear {
                                // Prepare the next card too so its summary is ready by the time it's swiped to.
                                guard index + 1 < items.count else { return }
                                let next = items[index + 1]
                                Task { await store.prepare(next) }
                            }
                    }
                    CaughtUpView()
                        .containerRelativeFrame(.vertical)
                }
                .scrollTargetLayout()
            }
            .scrollTargetBehavior(.paging)
            .scrollIndicators(.hidden)
            .refreshable { await store.refresh() }
        }
    }
}

struct CategoryBar: View {
    @Binding var selection: Category

    var body: some View {
        ScrollViewReader { proxy in
        ScrollView(.horizontal) {
            HStack(spacing: 8) {
                ForEach(Category.allCases) { c in
                    Button { withAnimation { selection = c } } label: {
                        Text(c.rawValue)
                            .font(.subheadline.weight(.medium))
                            .padding(.horizontal, 14)
                            .padding(.vertical, 6)
                            .background(selection == c ? Color.accentColor : Color(.secondarySystemGroupedBackground), in: Capsule())
                            .foregroundStyle(selection == c ? Color.white : Color.primary)
                            .contentShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    .accessibilityAddTraits(selection == c ? .isSelected : [])
                    .id(c)
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 8)
        }
        .scrollIndicators(.hidden)
        // Keep the current tab's chip in view when it changes by swiping.
        .onChange(of: selection) { _, new in withAnimation { proxy.scrollTo(new, anchor: .center) } }
        }
    }
}

struct CaughtUpView: View {
    @EnvironmentObject private var store: NewsStore

    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: "checkmark.circle.fill")
                .font(.system(size: 52))
                .foregroundStyle(.tint)
            Text("You're all caught up")
                .font(.title2.bold())
            if let updated = store.lastUpdated {
                Text("Updated \(updated, format: .relative(presentation: .named))")
                    .foregroundStyle(.secondary)
            }
            Button("Check for new stories") { Task { await store.refresh() } }
                .buttonStyle(.borderedProminent)
                .disabled(store.isRefreshing)
                .padding(.top, 8)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}
