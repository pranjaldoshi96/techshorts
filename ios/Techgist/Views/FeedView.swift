import SwiftUI

/// One tab's stories, one card per screen, paged vertically.
struct FeedView: View {
    @EnvironmentObject private var store: NewsStore
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let category: Category
    let items: [Article]
    /// The id of the card on screen.
    @Binding var position: String?
    let highlighted: String?
    let onRead: (Article) -> Void
    let onCoverage: (Article) -> Void
    /// Called when a card has been on screen long enough to count as read.
    let onViewed: (Article) -> Void
    let onSaved: () -> Void

    static let endID = "end-of-tab"

    var body: some View {
        if items.isEmpty {
            empty
        } else {
            ScrollView {
                LazyVStack(spacing: 0) {
                    ForEach(items) { article in
                        CardView(article: article, highlighted: highlighted == article.id,
                                 onRead: { onRead(article) }, onCoverage: { onCoverage(article) })
                            .containerRelativeFrame(.vertical)
                            .scrollTransition(.interactive, axis: .vertical) { [reduceMotion] content, phase in
                                // Depth: the card leaving at the top shrinks and dims, the next one rises into place.
                                let above = max(0, -phase.value), below = max(0, phase.value)
                                return content
                                    .scaleEffect(reduceMotion ? 1 : 1 - 0.06 * above)
                                    .opacity(1 - 0.5 * above)
                                    .offset(y: reduceMotion ? 0 : 24 * below)
                            }
                            .id(article.id)
                    }
                    EndOfTabCard(category: category, count: items.count,
                                 onTop: { withAnimation(reduceMotion ? nil : .default) { position = items.first?.id } },
                                 onSaved: onSaved)
                        .containerRelativeFrame(.vertical)
                        .id(Self.endID)
                }
                .scrollTargetLayout()
            }
            .scrollTargetBehavior(.paging)
            .scrollPosition(id: $position)
            .scrollIndicators(.hidden)
            .refreshable { await store.refresh() }
            .sensoryFeedback(.success, trigger: store.lastUpdated) { _, _ in store.isRefreshing }
            .task(id: currentID) { await settled() }
        }
    }

    private var currentID: String? { position ?? items.first?.id }

    /// Runs each time a card comes to rest: warm the next pictures, then count the card as read after 1.5 s.
    private func settled() async {
        guard let id = currentID, let index = items.firstIndex(where: { $0.id == id }) else { return }
        store.readerIsAtTop = index == 0
        let wide = UIScreen.main.bounds.width >= 390
        let upcoming = items.dropFirst(index + 1).prefix(3).compactMap { $0.cardImage(wide: wide) }
        ImageLoader.shared.prefetch(upcoming, maxPixel: ImageLoader.heroPixels)
        try? await Task.sleep(for: .seconds(1.5))
        guard !Task.isCancelled else { return }
        onViewed(items[index])
    }

    @ViewBuilder
    private var empty: some View {
        if store.isRefreshing || (store.lastUpdated == nil && !store.isOffline) {
            SkeletonCard()
        } else {
            ContentUnavailableView {
                Label("No stories", systemImage: "newspaper")
            } description: {
                Text(store.articles.isEmpty ? "Couldn't reach Techgist. Check your connection." : "Nothing here right now.")
            } actions: {
                Button("Try again") { Task { await store.refresh() } }
                    .buttonStyle(.borderedProminent)
            }
            .frame(maxHeight: .infinity)
        }
    }
}

/// The shape of a card while the first edition loads. No sample content is bundled.
struct SkeletonCard: View {
    var body: some View {
        GeometryReader { geo in
            VStack(alignment: .leading, spacing: 0) {
                Color(.tertiarySystemFill)
                    .frame(height: geo.size.width * Theme.heroAspect)
                    .clipShape(UnevenRoundedRectangle(bottomLeadingRadius: Theme.imageCorner, bottomTrailingRadius: Theme.imageCorner))
                VStack(alignment: .leading, spacing: 10) {
                    Text("Source · 2h").font(Theme.meta)
                    Text("A headline for a story that is still on its way to this phone").font(Theme.headline)
                    Text("The summary appears here once the edition has loaded. It is about sixty words long and says what happened, gives the context, and explains why it matters to people who follow technology.")
                        .font(Theme.body)
                        .lineSpacing(5)
                }
                .redacted(reason: .placeholder)
                .padding(.horizontal, Theme.horizontal)
                .padding(.top, Theme.imageToMeta)
                Spacer(minLength: 0)
            }
            .shimmer()
        }
        .background(Theme.cardBackground)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Loading today's stories")
    }
}

/// The last page of a tab.
struct EndOfTabCard: View {
    @EnvironmentObject private var store: NewsStore
    let category: Category
    let count: Int
    let onTop: () -> Void
    let onSaved: () -> Void

    var body: some View {
        GeometryReader { geo in
        ScrollView {
            VStack(spacing: 12) {
                Image(systemName: "checkmark.circle.fill")
                    .font(.system(size: 52))
                    .foregroundStyle(.tint)
                    .accessibilityHidden(true)
                Text("You're all caught up")
                    .font(.system(.title2, design: .serif).weight(.bold))
                    .multilineTextAlignment(.center)
                Text(detail)
                    .font(.subheadline)
                    .foregroundStyle(Theme.metaText)
                    .multilineTextAlignment(.center)
                Button(action: onTop) {
                    Text("Back to top").font(.body.weight(.semibold)).frame(maxWidth: 260).padding(.vertical, 4)
                }
                .buttonStyle(.borderedProminent)
                .padding(.top, 8)
                Button(action: onSaved) {
                    Text("Saved stories").font(.body.weight(.semibold)).frame(maxWidth: 260).padding(.vertical, 4)
                }
                .buttonStyle(.bordered)
            }
            .padding(24)
            // Centred when it fits, scrollable when the text size makes it taller than the page.
            .frame(maxWidth: .infinity, minHeight: geo.size.height)
        }
        .scrollBounceBehavior(.basedOnSize)
        }
        .background(Theme.cardBackground)
    }

    private var detail: String {
        var text = "\(count) \(count == 1 ? "story" : "stories") in \(category.title)"
        if let updated = store.lastUpdated { text += " · updated \(RelativeTime.spoken(updated))" }
        return text
    }
}
