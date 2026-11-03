import SwiftUI

struct CardView: View {
    @EnvironmentObject private var store: NewsStore
    let article: Article
    let onRead: () -> Void

    var body: some View {
        let summary = store.summary(for: article)
        VStack(alignment: .leading, spacing: 0) {
            ArticleImage(url: article.image)
                .containerRelativeFrame(.vertical) { height, _ in height * 0.32 }
                .frame(maxWidth: .infinity)

            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 6) {
                    Text(article.source.uppercased()).bold().foregroundStyle(.tint)
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
    let url: URL?

    var body: some View {
        Rectangle()
            .fill(LinearGradient(colors: [.accentColor, .purple], startPoint: .topLeading, endPoint: .bottomTrailing))
            .overlay {
                if let url {
                    AsyncImage(url: url, transaction: Transaction(animation: .easeIn(duration: 0.2))) { phase in
                        if let image = phase.image { image.resizable().scaledToFill() }
                    }
                } else {
                    Image(systemName: "newspaper")
                        .font(.system(size: 40))
                        .foregroundStyle(.white.opacity(0.85))
                }
            }
            .clipped()
            .accessibilityHidden(true)
    }
}
