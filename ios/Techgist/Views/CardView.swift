import SwiftUI

/// One story, one screen. The text never shrinks and never truncates: the card picks the richest layout that fits.
/// 1. Hero picture + full summary.
/// 2. Hero picture + short summary (the first two sentences).
/// 3. Thumbnail + full summary, for small phones and long headlines.
/// 4. The same, scrollable, for the largest text sizes.
struct CardView: View {
    @EnvironmentObject private var store: NewsStore
    @Environment(\.dynamicTypeSize) private var typeSize
    @Environment(\.colorScheme) private var scheme
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let article: Article
    var highlighted = false
    let onRead: () -> Void
    let onCoverage: () -> Void
    @State private var preparingShare = false

    var body: some View {
        GeometryReader { geo in
            let width = geo.size.width
            VStack(spacing: 0) {
                content(width: width)
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
                    .contentShape(Rectangle())
                    .onTapGesture(perform: onRead)
                    .contextMenu { menu }
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(spokenLabel)
                    .accessibilityHint("Opens the full story")
                    .accessibilityAddTraits(.isButton)
                    .accessibilityAction(.default, onRead)
                    .accessibilityAction(named: isSaved ? "Remove from saved" : "Save") { store.toggleSaved(article) }
                    .accessibilityAction(named: "Share") { share() }
                    .accessibilityActions {
                        if article.coverage >= 2 { Button("Full coverage", action: onCoverage) }
                        Button("Mute \(article.source)") { store.disabledSources.insert(article.source) }
                    }
                footer
            }
        }
        .background(Theme.cardBackground)
        .overlay {
            Rectangle()
                .strokeBorder(Color.accentColor, lineWidth: 4)
                .opacity(highlighted ? 1 : 0)
                .animation(.easeInOut(duration: 0.35), value: highlighted)
                .allowsHitTesting(false)
        }
        // Swipe left to read, as in Inshorts. Simultaneous, so vertical paging is never held up by it.
        .simultaneousGesture(
            DragGesture(minimumDistance: 30).onEnded { value in
                let dx = value.translation.width, dy = value.translation.height
                if dx < -80, abs(dx) > 2 * abs(dy) { onRead() }
            }
        )
    }

    private var isSaved: Bool { store.isSaved(article) }

    // MARK: Layouts

    @ViewBuilder
    private func content(width: CGFloat) -> some View {
        let full = article.displaySummary
        let short = article.shortSummary
        if typeSize.isAccessibilitySize {
            ViewThatFits(in: .vertical) {
                compact(summary: full)
                ScrollView { compact(summary: full) }
                    .scrollBounceBehavior(.basedOnSize)
            }
        } else if short != full {
            ViewThatFits(in: .vertical) {
                hero(summary: full, width: width)
                hero(summary: short, width: width)
                compact(summary: full)
                compact(summary: short)
                ScrollView { compact(summary: full) }
                    .scrollBounceBehavior(.basedOnSize)
            }
        } else {
            ViewThatFits(in: .vertical) {
                hero(summary: full, width: width)
                compact(summary: full)
                ScrollView { compact(summary: full) }
                    .scrollBounceBehavior(.basedOnSize)
            }
        }
    }

    private func hero(summary: String, width: CGFloat) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            StoryImage(article: article, url: article.cardImage(wide: width >= 390), maxPixel: ImageLoader.heroPixels)
                .overlay { Theme.scrim }
                .overlay(alignment: .bottomLeading) {
                    sourceLabel
                        .foregroundStyle(.white)
                        .shadow(color: .black.opacity(0.6), radius: 3, y: 1)
                        .padding(.horizontal, Theme.horizontal)
                        .padding(.bottom, 10)
                }
                .clipShape(UnevenRoundedRectangle(bottomLeadingRadius: Theme.imageCorner, bottomTrailingRadius: Theme.imageCorner))
                .frame(minHeight: Theme.minHeroHeight, idealHeight: Theme.minHeroHeight, maxHeight: max(Theme.minHeroHeight, width * Theme.heroAspect))
            textBlock(summary: summary)
                .padding(.top, Theme.imageToMeta)
        }
    }

    private func compact(summary: String) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .center, spacing: 12) {
                StoryImage(article: article, url: article.thumbImage, maxPixel: ImageLoader.thumbPixels)
                    .frame(width: Theme.thumbSize, height: Theme.thumbSize)
                    .clipShape(.rect(cornerRadius: 10))
                sourceLabel
                    .foregroundStyle(.primary)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, Theme.horizontal)
            textBlock(summary: summary)
                .padding(.top, 12)
        }
        .padding(.top, 14)
    }

    private var sourceLabel: some View {
        HStack(spacing: 7) {
            SourceMonogram(name: article.source)
            Text(article.source)
                .font(.caption.weight(.semibold))
        }
    }

    private func textBlock(summary: String) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            metaRow
            Text(article.displayTitle)
                .font(Theme.headline)
                .lineSpacing(2)
                .foregroundStyle(.primary)
                .padding(.top, Theme.metaToHeadline)
            if !summary.isEmpty {
                Text(summary)
                    .font(Theme.body)
                    .lineSpacing(5)
                    .foregroundStyle(Theme.bodyText)
                    .padding(.top, Theme.headlineToBody)
            }
            coverageRow
        }
        .fixedSize(horizontal: false, vertical: true)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, Theme.horizontal)
        .padding(.bottom, 8)
    }

    // MARK: Meta

    private var metaRow: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 8) { metaItems }
            VStack(alignment: .leading, spacing: 4) { metaItems }
        }
        .font(Theme.meta)
        .foregroundStyle(Theme.metaText)
    }

    @ViewBuilder
    private var metaItems: some View {
        if article.isBreaking {
            BadgePill(text: "BREAKING", color: Theme.badgeBreaking)
                .modifier(PulseTwice(enabled: !reduceMotion))
        } else if article.isDeveloping {
            BadgePill(text: "DEVELOPING", color: Theme.badgeDeveloping)
        } else if article.isHot {
            BadgePill(text: "HOT", color: Theme.badgeHot, symbol: "flame.fill")
        }
        if let kind = article.kindLabel {
            Text(kind).font(Theme.badge).tracking(0.6)
        }
        Text(RelativeTime.label(for: article))
        if article.isAISummary {
            Label("AI summary", systemImage: "sparkles")
                .labelStyle(.titleAndIcon)
        } else if article.aiText == false {
            Text("Summary pending")
        }
    }

    @ViewBuilder
    private var coverageRow: some View {
        let others = article.alsoCoveredBy ?? []
        if !others.isEmpty {
            Button(action: onCoverage) {
                HStack(spacing: 8) {
                    HStack(spacing: -6) {
                        ForEach(Array(([article.source] + others).prefix(4).enumerated()), id: \.offset) { _, name in
                            SourceMonogram(name: name, size: 22)
                                .overlay(Circle().stroke(Theme.cardBackground, lineWidth: 1.5))
                        }
                    }
                    Text(coverageText)
                        .multilineTextAlignment(.leading)
                    Image(systemName: "chevron.right").imageScale(.small)
                }
                .font(Theme.meta.weight(.medium))
                .foregroundStyle(Theme.metaText)
                .frame(minHeight: 44)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .padding(.top, 2)
        } else if let points = article.points {
            Text("\(points) points on Hacker News")
                .font(Theme.meta)
                .foregroundStyle(Theme.metaText)
                .padding(.top, Theme.bodyToCoverage)
        }
    }

    private var coverageText: String {
        var text = "\(article.source) + \(article.coverage - 1) more"
        if let points = article.points { text += " · \(points) pts on HN" }
        return text
    }

    // MARK: Footer

    private var footer: some View {
        HStack(spacing: 4) {
            Button(action: onRead) {
                ViewThatFits(in: .horizontal) {
                    readLabel("Read more at \(article.source)")
                    readLabel("Read more")
                }
                .frame(minHeight: 44)
                .contentShape(Rectangle())
            }
            .accessibilityLabel("Read more at \(article.source)")
            Spacer(minLength: 8)
            Button { store.toggleSaved(article) } label: {
                Image(systemName: isSaved ? "bookmark.fill" : "bookmark")
                    .contentTransition(.symbolEffect(.replace))
                    .symbolEffect(.bounce, value: reduceMotion ? false : isSaved)
                    .frame(minWidth: 44, minHeight: 44)
                    .contentShape(Rectangle())
            }
            .accessibilityLabel(isSaved ? "Remove from saved" : "Save story")
            .sensoryFeedback(.success, trigger: isSaved) { _, saved in saved }
            Button(action: share) {
                Group {
                    if preparingShare { ProgressView() } else { Image(systemName: "square.and.arrow.up") }
                }
                .frame(minWidth: 44, minHeight: 44)
                .contentShape(Rectangle())
            }
            .disabled(preparingShare)
            .accessibilityLabel("Share")
        }
        .font(.body.weight(.semibold))
        .padding(.leading, Theme.horizontal)
        .padding(.trailing, Theme.horizontal - 10)
        .frame(minHeight: Theme.footerHeight)
        .background(Theme.cardBackground)
        .overlay(alignment: .top) { Theme.hairline(scheme).frame(height: 1) }
    }

    private func readLabel(_ text: String) -> some View {
        HStack(spacing: 4) {
            Text(text).lineLimit(1)
            Image(systemName: "chevron.right").imageScale(.small)
        }
    }

    @ViewBuilder
    private var menu: some View {
        Button { store.toggleSaved(article) } label: {
            Label(isSaved ? "Remove from saved" : "Save", systemImage: isSaved ? "bookmark.slash" : "bookmark")
        }
        Button(action: share) { Label("Share", systemImage: "square.and.arrow.up") }
        if article.coverage >= 2 {
            Button(action: onCoverage) { Label("Full coverage", systemImage: "newspaper") }
        }
        Button { UIPasteboard.general.url = article.url } label: { Label("Copy link", systemImage: "link") }
        Button(role: .destructive) { store.disabledSources.insert(article.source) } label: {
            Label("Mute \(article.source)", systemImage: "speaker.slash")
        }
    }

    private func share() {
        guard !preparingShare else { return }
        preparingShare = true
        Task {
            if let payload = await StoryShare.payload(for: article, summary: article.displaySummary) {
                StoryShare.present(payload)
            }
            preparingShare = false
        }
    }

    /// What VoiceOver reads for the whole card, in reading order.
    private var spokenLabel: String {
        var parts: [String] = []
        if article.isBreaking { parts.append("Breaking") } else if article.isDeveloping { parts.append("Developing") }
        if let kind = article.kindLabel { parts.append(kind.capitalized) }
        parts.append(article.displayTitle + ".")
        if !article.displaySummary.isEmpty { parts.append(article.displaySummary) }
        var source = article.source
        let others = article.coverage - 1
        if others > 0 { source += " and \(others) other \(others == 1 ? "outlet" : "outlets")" }
        parts.append(source + ", " + RelativeTime.spokenLabel(for: article) + ".")
        if article.isAISummary { parts.append("Summarized by AI.") }
        return parts.joined(separator: " ")
    }
}

/// Two gentle pulses when a BREAKING badge first appears, then still.
private struct PulseTwice: ViewModifier {
    let enabled: Bool
    @State private var big = false

    func body(content: Content) -> some View {
        content
            .scaleEffect(big ? 1.12 : 1)
            .onAppear {
                guard enabled else { return }
                withAnimation(.easeInOut(duration: 0.35).repeatCount(3, autoreverses: true)) { big = true }
                // Up, down, up; then settle back down for the second pulse.
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.1) {
                    withAnimation(.easeOut(duration: 0.2)) { big = false }
                }
            }
    }
}
