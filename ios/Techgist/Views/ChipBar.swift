import SwiftUI

extension Category {
    /// The tab's name on screen. The `Blogs` category holds long reads and analysis.
    var title: String { self == .blogs ? "Reads" : rawValue }
}

/// The tab chips. The selected capsule slides between chips; with Reduce Motion it simply moves.
struct ChipBar: View {
    @Binding var selection: Category
    /// Tabs with stories that haven't been read yet.
    let unread: Set<Category>
    @Environment(\.colorScheme) private var scheme
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Namespace private var capsule

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView(.horizontal) {
                HStack(spacing: 8) {
                    ForEach(Category.allCases) { category in
                        chip(category).id(category)
                    }
                }
                .padding(.horizontal, 16)
            }
            .scrollIndicators(.hidden)
            // Like a tab bar, the chips stop growing part-way up the accessibility sizes so the stories keep the
            // screen; pressing and holding a chip shows its name large.
            .dynamicTypeSize(...DynamicTypeSize.accessibility2)
            .onChange(of: selection) { _, new in
                withAnimation(reduceMotion ? nil : Theme.chipSpring) { proxy.scrollTo(new, anchor: .center) }
            }
        }
        .sensoryFeedback(.selection, trigger: selection)
    }

    private func chip(_ category: Category) -> some View {
        let selected = selection == category
        return Button {
            withAnimation(reduceMotion ? nil : Theme.chipSpring) { selection = category }
        } label: {
            HStack(spacing: 6) {
                Text(category.title)
                if unread.contains(category), !selected {
                    Circle().fill(Theme.accent).frame(width: 6, height: 6)
                }
            }
            .font(.subheadline.weight(.semibold))
            .foregroundStyle(selected ? Theme.onAccent(scheme) : Color.primary)
            .padding(.horizontal, 14)
            .padding(.vertical, 7)
            .background {
                if selected {
                    Capsule().fill(Theme.accent).matchedGeometryEffect(id: "selected", in: capsule)
                } else {
                    Capsule().fill(Color(.secondarySystemBackground))
                }
            }
            // The capsule is about 34 pt tall; the tap area is 44.
            .padding(.vertical, 5)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityShowsLargeContentViewer { Text(category.title) }
        .accessibilityLabel(category.title)
        .accessibilityValue(unread.contains(category) ? "Unread stories" : "")
        .accessibilityAddTraits(selected ? [.isSelected] : [])
    }
}

/// A thin line showing how far through the tab the reader is.
struct ProgressLine: View {
    let fraction: Double
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        GeometryReader { geo in
            Theme.accent
                .frame(width: geo.size.width * min(max(fraction, 0), 1))
                .animation(reduceMotion ? nil : .snappy(duration: 0.25), value: fraction)
        }
        .frame(height: 2)
        .background(Color.primary.opacity(0.08))
        .accessibilityHidden(true)
    }
}

/// One line under the chips when the stories are old, offline or without AI text.
struct StateBanner: View {
    let text: String
    let symbol: String

    var body: some View {
        Label(text, systemImage: symbol)
            .font(.footnote.weight(.medium))
            .foregroundStyle(.primary)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 6)
            .padding(.horizontal, Theme.horizontal)
            .background(Color(.secondarySystemBackground))
            .accessibilityAddTraits(.isStaticText)
    }
}

/// "3 new stories": appears when a newer edition is waiting, and brings it in when tapped.
struct NewStoriesPill: View {
    let count: Int
    let action: () -> Void
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        Button(action: action) {
            Label("\(count) new \(count == 1 ? "story" : "stories")", systemImage: "arrow.up")
                .font(.subheadline.bold())
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
                .frame(minHeight: 44)
                .background(Theme.accent, in: Capsule())
                .foregroundStyle(Theme.onAccent(scheme))
                .shadow(color: .black.opacity(0.25), radius: 8, y: 3)
        }
        .buttonStyle(.plain)
        .accessibilityHint("Shows the newest stories")
    }
}
