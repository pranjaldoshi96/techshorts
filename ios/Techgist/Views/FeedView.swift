import SwiftUI

/// One tab's stories, one card per screen, paged vertically.
/// At the accessibility text sizes a story is taller than a screen, so the feed becomes an ordinary scrolling list
/// of full-length cards instead: nothing is cut off and nothing needs a scroll view inside a scroll view.
struct FeedView: View {
    @EnvironmentObject private var store: NewsStore
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.dynamicTypeSize) private var typeSize
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
    /// Where the feed is heading, kept across gestures so a flick made while the last one is still moving counts
    /// from the card the feed was already going to.
    @State private var paging = FeedPaging.Memory()

    static let endID = "end-of-tab"

    var body: some View {
        if items.isEmpty {
            empty
        } else {
            // The feed runs to the bottom edge of the screen and each page is exactly one screen of it. Cards get
            // their size and the home-indicator inset as numbers, so nothing inside the scroll view depends on the
            // safe area (which SwiftUI applies unevenly to pages that are partly off screen).
            GeometryReader { geo in
                let inset = geo.safeAreaInsets.bottom
                pages(CGSize(width: geo.size.width, height: geo.size.height + inset), inset: inset)
            }
        }
    }

    private func pages(_ page: CGSize, inset: CGFloat) -> some View {
        let paged = !typeSize.isAccessibilitySize
        return ScrollView {
            LazyVStack(spacing: 0) {
                ForEach(items) { article in
                    CardView(article: article, width: page.width, bottomInset: paged ? inset : 0, flows: !paged,
                             highlighted: highlighted == article.id,
                             onRead: { onRead(article) }, onCoverage: { onCoverage(article) })
                        .frame(width: page.width, height: paged ? page.height : nil)
                        .scrollTransition(.interactive, axis: .vertical) { [reduceMotion] content, phase in
                            // Depth: the card leaving at the top shrinks and dims, the next one rises into place.
                            // With Reduce Motion only the dimming remains; a scrolling list of tall cards has neither.
                            let above = paged ? max(0, -phase.value) : 0, below = paged ? max(0, phase.value) : 0
                            return content
                                .scaleEffect(reduceMotion ? 1 : 1 - 0.06 * above)
                                .opacity(1 - 0.5 * above)
                                .offset(y: reduceMotion ? 0 : 24 * below)
                        }
                        .id(article.id)
                    if !paged {
                        Color(.secondarySystemBackground).frame(height: 10)
                    }
                }
                EndOfTabCard(category: category, count: items.count,
                             onTop: { withAnimation(reduceMotion ? nil : .default) { position = items.first?.id } },
                             onSaved: onSaved)
                    .padding(.bottom, inset)
                    .frame(width: page.width, height: page.height)
                    .id(Self.endID)
            }
            .scrollTargetLayout()
        }
            .scrollTargetBehavior(FeedPaging(paged: paged, memory: paging))
            .scrollPosition(id: $position)
            .scrollIndicators(paged ? .hidden : .automatic)
            .ignoresSafeArea(.container, edges: .bottom)
            .refreshable { await store.refresh(atLeast: .seconds(1.2)) }
            .sensoryFeedback(.success, trigger: store.refreshesCompleted)
            .task(id: currentID) { await settled() }
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
                    .buttonStyle(PrimaryButtonStyle())
            }
            .frame(maxHeight: .infinity)
        }
    }
}

/// One card per swipe when the feed is paged; free scrolling when it is a list of tall cards.
///
/// The system's view-aligned paging settles on whichever card is nearest to where the scroll would coast to. A drag
/// that ends without speed (a mouse or trackpad drag in the simulator, a slow or hesitant finger) therefore sprang
/// back to the same card unless it had crossed half the screen. Here the direction of the gesture decides: once the
/// feed has moved a thumb's width from where the gesture began, it goes to the neighbouring card, and never further.
struct FeedPaging: ScrollTargetBehavior {
    let paged: Bool
    var memory = Memory()
    /// How far the feed must move (or be about to coast) before the gesture counts as "next card".
    static let commitDistance: CGFloat = 36

    /// What the feed remembers between gestures. A reference, because the behaviour itself is a value that
    /// SwiftUI rebuilds with the view.
    final class Memory {
        /// The offset the feed was last sent to, and when.
        private(set) var heading: CGFloat?
        private var headedAt: TimeInterval = 0
        /// The gesture last answered (by where it began) and the heading it was counted from. SwiftUI can ask
        /// more than once about one gesture; every answer must count from the same card.
        private var start: CGFloat?
        private var base: CGFloat?
        /// Longer than any settle; after it the feed is somewhere for another reason (a jump to the top, a new tab).
        static let lifetime: TimeInterval = 1.5

        /// The heading to count a gesture that began at `start` from; nil when the last one is too old to mean anything.
        func heading(forGestureAt start: CGFloat, now: TimeInterval = ProcessInfo.processInfo.systemUptime) -> CGFloat? {
            if self.start != start {
                self.start = start
                base = now - headedAt < Self.lifetime ? heading : nil
            }
            return base
        }

        func head(to offset: CGFloat, now: TimeInterval = ProcessInfo.processInfo.systemUptime) {
            heading = offset
            headedAt = now
        }
    }

    func updateTarget(_ target: inout ScrollTarget, context: TargetContext) {
        guard paged else { return }
        let start = context.originalTarget.rect.minY
        let rest = Self.restingOffset(start: start, projected: target.rect.minY,
                                      page: context.containerSize.height, content: context.contentSize.height,
                                      heading: memory.heading(forGestureAt: start))
        memory.head(to: rest)
        target.rect.origin.y = rest
    }

    /// Where the feed comes to rest. `start` is the offset when the gesture began, `projected` where it would
    /// coast to if left alone, `heading` where the previous gesture sent the feed. Pure, so the rule is unit-tested.
    static func restingOffset(start: CGFloat, projected: CGFloat, page: CGFloat, content: CGFloat,
                              heading: CGFloat? = nil) -> CGFloat {
        guard page > 0 else { return projected }
        let last = max(0, (content / page).rounded() - 1)
        let moved = projected - start
        var index = (start / page).rounded()
        // A gesture that begins while the feed is still on its way to a card counts from that card: the second
        // of two quick flicks goes one past it, not to it again. (Counting from the nearest card swallowed any
        // flick made before the feed was half way.) Without a heading in reach, the nearest card it is.
        // Only a gesture that moves the feed counts this way. SwiftUI also asks when nothing was swiped (the
        // text size or the layout changed); the answer then is the nearest card, wherever the feed was heading.
        let swiped = abs(moved) > commitDistance
        let atRest = abs(start - index * page) < 1
        // (Several quick flicks put the heading more than a card ahead of the feed, hence three.)
        if swiped, !atRest, let heading, abs(heading - start) < 3 * page { index = (heading / page).rounded() }
        if moved > commitDistance { index += 1 } else if moved < -commitDistance { index -= 1 }
        return min(max(index, 0), last) * page
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
                    Text("Back to top").frame(maxWidth: 220)
                }
                .buttonStyle(PrimaryButtonStyle())
                .padding(.top, 8)
                Button(action: onSaved) {
                    Text("Saved stories").font(.body.weight(.semibold)).frame(maxWidth: 220).frame(minHeight: 44)
                }
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
