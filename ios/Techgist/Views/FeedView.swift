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
    /// A sideways swipe: +1 for the tab to the right, -1 for the tab to the left.
    let onSwipeTab: (Int) -> Void
    /// How far the finger has dragged the page, and which way the gesture turned out to be going.
    @State private var drag: CGFloat = 0
    @State private var dragAxis: Axis?
    /// The card the three-card window is built around. It follows the card on screen a moment after the snap
    /// ends, so the next neighbour is built while the feed is still, not in the middle of the animation.
    @State private var windowCenter: Int?

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
                let page = CGSize(width: geo.size.width, height: geo.size.height + inset)
                if typeSize.isAccessibilitySize {
                    list(page, inset: inset)
                } else {
                    pager(page, inset: inset)
                }
            }
        }
    }

    // MARK: Paged feed

    /// One card per screen. The page follows the finger and, on release, snaps to the next card in a quarter of a
    /// second whatever the speed of the swipe (a scroll view's own settle took over a second after a slow drag).
    /// Only the card on screen and its two neighbours exist at any time.
    private func pager(_ page: CGSize, inset: CGFloat) -> some View {
        let ids = items.map(\.id) + [Self.endID]
        let index = position.flatMap { ids.firstIndex(of: $0) } ?? 0
        // Two quick flicks can outrun the window; then it moves at once.
        let center = windowCenter.flatMap { abs($0 - index) <= 1 && $0 < ids.count ? $0 : nil } ?? index
        let window = Array(ids[max(0, center - 1)...min(ids.count - 1, center + 1)])
        return ZStack(alignment: .top) {
            // The cards are their own view, compared by what they show: a drag moves them as one layer
            // without drawing any of them again.
            PageWindow(ids: window, first: max(0, center - 1), index: index, items: items, category: category,
                       page: page, inset: inset, highlighted: highlighted,
                       onRead: onRead, onCoverage: onCoverage, onTop: { go(to: 0, ids: ids) }, onSaved: onSaved)
                .equatable()
                .offset(y: drag)
            if index == 0, drag > 24 || store.isRefreshing {
                ProgressView().padding(.top, 14).allowsHitTesting(false)
            }
        }
        .frame(width: page.width, height: page.height, alignment: .top)
        .clipped()
        .contentShape(Rectangle())
        .simultaneousGesture(swipes(index: index, ids: ids, page: page))
        .accessibilityScrollAction { edge in
            switch edge {
            case .bottom, .trailing: go(to: index + 1, ids: ids)
            case .top, .leading: go(to: index - 1, ids: ids)
            }
        }
        .ignoresSafeArea(.container, edges: .bottom)
        .sensoryFeedback(.success, trigger: store.refreshesCompleted)
        .task(id: currentID) { await settled() }
    }

    private static let snap = Animation.snappy(duration: 0.24, extraBounce: 0)

    private func go(to index: Int, ids: [String]) {
        let target = min(max(index, 0), ids.count - 1)
        withAnimation(reduceMotion ? .easeOut(duration: 0.12) : Self.snap) {
            position = ids[target]
            drag = 0
        }
    }

    /// Up and down move between stories (pulling down on the first one refreshes); left and right change tabs.
    private func swipes(index: Int, ids: [String], page: CGSize) -> some Gesture {
        DragGesture(minimumDistance: 10)
            .onChanged { value in
                let t = value.translation
                if dragAxis == nil { dragAxis = abs(t.width) > abs(t.height) ? .horizontal : .vertical }
                guard dragAxis == .vertical else { return }
                // Past either end the page follows the finger at a third of its speed.
                let atEnd = (index == 0 && t.height > 0) || (index == ids.count - 1 && t.height < 0)
                drag = atEnd ? t.height / 3 : t.height
            }
            .onEnded { value in
                let axis = dragAxis
                dragAxis = nil
                let t = value.translation
                if axis == .horizontal {
                    if let step = FeedPaging.tabStep(dx: t.width, dy: t.height) { onSwipeTab(step) }
                    return
                }
                let step = FeedPaging.step(moved: t.height, projected: value.predictedEndTranslation.height)
                let refresh = index == 0 && t.height > FeedPaging.refreshPull
                go(to: index + step, ids: ids)
                if refresh { Task { await store.refresh(atLeast: .seconds(0.6)) } }
            }
    }

    // MARK: List at the accessibility text sizes

    private func list(_ page: CGSize, inset: CGFloat) -> some View {
        ScrollView {
            LazyVStack(spacing: 0) {
                ForEach(items) { article in
                    CardView(article: article, width: page.width, bottomInset: 0, flows: true,
                             highlighted: highlighted == article.id,
                             onRead: { onRead(article) }, onCoverage: { onCoverage(article) })
                        .frame(width: page.width)
                        .id(article.id)
                    Color(.secondarySystemBackground).frame(height: 10)
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
            .scrollPosition(id: $position)
            .ignoresSafeArea(.container, edges: .bottom)
            .refreshable { await store.refresh(atLeast: .seconds(1.2)) }
            .sensoryFeedback(.success, trigger: store.refreshesCompleted)
            .simultaneousGesture(DragGesture(minimumDistance: 30).onEnded { value in
                if let step = FeedPaging.tabStep(dx: value.translation.width, dy: value.translation.height) { onSwipeTab(step) }
            })
            .task(id: currentID) { await settled() }
    }

    private var currentID: String? { position ?? items.first?.id }

    /// Runs each time a card comes to rest: warm the next pictures, then count the card as read after 1.5 s.
    private func settled() async {
        try? await Task.sleep(for: .seconds(0.3))
        guard !Task.isCancelled else { return }
        if let id = currentID {
            windowCenter = id == Self.endID ? items.count : items.firstIndex(where: { $0.id == id })
        }
        guard let id = currentID, let index = items.firstIndex(where: { $0.id == id }) else { return }
        store.readerIsAtTop = index == 0
        let wide = UIScreen.main.bounds.width >= 390
        let upcoming = items.dropFirst(index + 1).prefix(3).compactMap { $0.cardImage(wide: wide) }
        ImageLoader.shared.prefetch(upcoming, maxPixel: ImageLoader.heroPixels)
        try? await Task.sleep(for: .seconds(1.2))
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

/// The card on screen and its neighbours, each a page apart. Equal when it would draw the same thing, so the
/// feed can move it with the finger at no cost.
private struct PageWindow: View, Equatable {
    let ids: [String]
    /// The index of `ids.first` in the tab, and of the card on screen.
    let first: Int
    let index: Int
    let items: [Article]
    let category: Category
    let page: CGSize
    let inset: CGFloat
    let highlighted: String?
    let onRead: (Article) -> Void
    let onCoverage: (Article) -> Void
    let onTop: () -> Void
    let onSaved: () -> Void

    static func == (a: PageWindow, b: PageWindow) -> Bool {
        a.ids == b.ids && a.index == b.index && a.page == b.page && a.inset == b.inset
            && a.highlighted == b.highlighted && a.category == b.category
            && a.items.count == b.items.count && a.visible == b.visible
    }

    /// The articles this window shows (a story's text can change with a new edition while its id stays).
    private var visible: [Article] { Array(items[min(first, items.count)..<min(first + ids.count, items.count)]) }

    var body: some View {
        ZStack(alignment: .top) {
            ForEach(Array(ids.enumerated()), id: \.element) { offset, _ in
                let i = first + offset
                Group {
                    if i < items.count {
                        let article = items[i]
                        CardView(article: article, width: page.width, bottomInset: inset, flows: false,
                                 highlighted: highlighted == article.id,
                                 onRead: { onRead(article) }, onCoverage: { onCoverage(article) })
                    } else {
                        EndOfTabCard(category: category, count: items.count, onTop: onTop, onSaved: onSaved)
                            .padding(.bottom, inset)
                    }
                }
                .frame(width: page.width, height: page.height)
                .offset(y: CGFloat(i - index) * page.height)
                .accessibilityHidden(i != index)
                .transition(.identity)
            }
        }
        .frame(width: page.width, height: page.height, alignment: .top)
    }
}

/// What a swipe on the paged feed means. Pure, so the rules are unit-tested.
enum FeedPaging {
    /// How far the finger must move before the swipe counts as "next card"; shorter drags spring back.
    static let commitDistance: CGFloat = 36
    /// How far the first card must be pulled down to refresh.
    static let refreshPull: CGFloat = 90
    /// A flick shorter than `commitDistance` still counts when it would have coasted this far.
    static let flickDistance: CGFloat = 120

    /// +1 for the next card, -1 for the previous, 0 to stay. `moved` is the finger's vertical travel (up is
    /// negative), `projected` where it would have coasted to. Never more than one card per swipe.
    static func step(moved: CGFloat, projected: CGFloat) -> Int {
        if moved < -commitDistance || (moved < 0 && projected < -flickDistance) { return 1 }
        if moved > commitDistance || (moved > 0 && projected > flickDistance) { return -1 }
        return 0
    }

    /// +1 for the tab to the right (a swipe to the left), -1 for the tab to the left, nil when the swipe was
    /// too short or not clearly sideways.
    static func tabStep(dx: CGFloat, dy: CGFloat) -> Int? {
        guard abs(dx) > 50, abs(dx) > 1.5 * abs(dy) else { return nil }
        return dx < 0 ? 1 : -1
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
