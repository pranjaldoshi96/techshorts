import SwiftUI

struct CardView: View {
    @EnvironmentObject private var store: NewsStore
    let article: Article
    let onRead: () -> Void

    var body: some View {
        let summary = store.summary(for: article)
        VStack(alignment: .leading, spacing: 0) {
            ArticleImage(article: article)
                // Bigger photo on tall phones; small phones need the room for the summary.
                .containerRelativeFrame(.vertical) { height, _ in height * (height > 700 ? 0.4 : 0.32) }
                .frame(maxWidth: .infinity)

            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 6) {
                    Text(article.source.uppercased()).bold().foregroundStyle(.tint)
                    if let others = article.alsoCoveredBy, !others.isEmpty {
                        Text("+\(others.count) more")
                            .foregroundStyle(.secondary)
                            .accessibilityLabel("Also covered by \(others.formatted())")
                    }
                    Text("·").foregroundStyle(.secondary)
                    Text(article.publishedAt, format: .relative(presentation: .named)).foregroundStyle(.secondary)
                }
                .font(.caption)
                .lineLimit(1)

                Text(article.title)
                    .font(.title3.weight(.bold))
                    .lineLimit(4)
                    .fixedSize(horizontal: false, vertical: true)

                if !summary.text.isEmpty {
                    Text(summary.text)
                        .font(.body)
                        .foregroundStyle(.secondary)
                        .lineLimit(10)
                        .minimumScaleFactor(0.85)
                }
                if summary.isAI {
                    Label("Summarized on device", systemImage: "sparkles")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }

                Spacer(minLength: 0)

                HStack(spacing: 22) {
                    Button(action: onRead) {
                        Label("Read full story", systemImage: "arrow.up.right")
                            .labelStyle(.titleAndIcon)
                            .font(.body.bold())
                    }
                    Spacer()
                    Button { store.toggleSaved(article) } label: {
                        Image(systemName: store.isSaved(article) ? "bookmark.fill" : "bookmark")
                    }
                    .accessibilityLabel(store.isSaved(article) ? "Remove from saved" : "Save story")
                    ShareLink(item: article.url, subject: Text(article.title)) {
                        Image(systemName: "square.and.arrow.up")
                    }
                    .accessibilityLabel("Share")
                }
            }
            .padding(18)
        }
        .background(Color(.secondarySystemGroupedBackground))
        .clipShape(.rect(cornerRadius: 22))
        .shadow(color: .black.opacity(0.08), radius: 12, y: 4)
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
    }
}

struct ArticleImage: View {
    let article: Article

    var body: some View {
        StoryCover(article: article)
            .overlay {
                // The cover stays visible while the photo loads, and if it never does.
                if let url = article.image {
                    AsyncImage(url: url, transaction: Transaction(animation: .easeIn(duration: 0.2))) { phase in
                        if let image = phase.image { image.resizable().scaledToFill() }
                    }
                }
            }
            .clipped()
            .accessibilityHidden(true)
    }
}

/// A designed cover for stories without a photo: a gradient in the category's colors with its symbol and the source name.
private struct StoryCover: View {
    let article: Article

    private var style: (colors: [Color], symbol: String) {
        switch article.category {
        case .top: ([Color(red: 0.18, green: 0.42, blue: 1), Color(red: 0.45, green: 0.25, blue: 0.95)], "bolt.fill")
        case .ai: ([Color(red: 0.55, green: 0.2, blue: 0.95), Color(red: 0.95, green: 0.3, blue: 0.6)], "sparkles")
        case .startups: ([Color(red: 1, green: 0.45, blue: 0.2), Color(red: 0.95, green: 0.2, blue: 0.4)], "rocket.fill")
        case .dev: ([Color(red: 0.05, green: 0.6, blue: 0.55), Color(red: 0.1, green: 0.35, blue: 0.75)], "chevron.left.forwardslash.chevron.right")
        case .blogs: ([Color(red: 0.95, green: 0.6, blue: 0.1), Color(red: 0.85, green: 0.3, blue: 0.2)], "text.quote")
        }
    }

    var body: some View {
        LinearGradient(colors: style.colors, startPoint: .topLeading, endPoint: .bottomTrailing)
            .overlay(alignment: .topTrailing) {
                Image(systemName: style.symbol)
                    .font(.system(size: 120, weight: .bold))
                    .foregroundStyle(.white.opacity(0.15))
                    .offset(x: 24, y: -12)
            }
            .overlay(alignment: .bottomLeading) {
                Text(article.source)
                    .font(.system(.largeTitle, design: .rounded, weight: .heavy))
                    .foregroundStyle(.white)
                    .lineLimit(2)
                    .minimumScaleFactor(0.6)
                    .padding(20)
            }
    }
}
