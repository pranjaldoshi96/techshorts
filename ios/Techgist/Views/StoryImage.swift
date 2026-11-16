import SwiftUI

/// A story's picture, filling whatever frame it is given. The picture is decoded off the main thread by
/// `ImageLoader`; a cached one shows on the first frame, a new one fades in over a shimmering placeholder.
/// Stories without a usable picture get `StoryCover`.
struct StoryImage: View {
    let article: Article
    let url: URL?
    let maxPixel: Int
    @State private var image: UIImage?
    @State private var failed = false

    init(article: Article, url: URL?, maxPixel: Int) {
        self.article = article
        self.url = url
        self.maxPixel = maxPixel
        _image = State(initialValue: url.flatMap { ImageLoader.shared.cached($0, maxPixel: maxPixel) })
    }

    var body: some View {
        Color(.secondarySystemBackground)
            .overlay {
                if let image {
                    Image(uiImage: image)
                        .resizable()
                        .scaledToFill()
                        .transition(.opacity)
                } else if url == nil || failed {
                    StoryCover(article: article)
                } else {
                    Color(.tertiarySystemFill).shimmer()
                }
            }
            .clipped()
            .task(id: url) {
                guard let url, image == nil else { return }
                let asked = Date()
                let loaded = await ImageLoader.shared.image(for: url, maxPixel: maxPixel)
                guard !Task.isCancelled else { return }
                if let loaded {
                    // A picture that was on disk is there within a frame or two: show it at once. Only one that had
                    // to be downloaded fades in.
                    withAnimation(Date().timeIntervalSince(asked) < 0.12 ? nil : Theme.fade) { image = loaded }
                } else {
                    failed = true
                }
            }
            .accessibilityHidden(true)
    }
}

/// A drawn cover for stories without a photo: a gradient in the tab's colours with its symbol. No text, so it
/// never clips at any size.
struct StoryCover: View {
    let article: Article

    private var style: (colors: [Color], symbol: String) {
        switch article.category {
        case .top: ([Color(red: 0.14, green: 0.33, blue: 0.85), Color(red: 0.36, green: 0.20, blue: 0.78)], "bolt.fill")
        case .ai: ([Color(red: 0.45, green: 0.17, blue: 0.80), Color(red: 0.78, green: 0.22, blue: 0.50)], "sparkles")
        case .startups: ([Color(red: 0.82, green: 0.35, blue: 0.14), Color(red: 0.76, green: 0.16, blue: 0.33)], "paperplane.fill")
        case .dev: ([Color(red: 0.04, green: 0.47, blue: 0.44), Color(red: 0.09, green: 0.28, blue: 0.62)], "chevron.left.forwardslash.chevron.right")
        case .blogs: ([Color(red: 0.76, green: 0.46, blue: 0.08), Color(red: 0.68, green: 0.24, blue: 0.16)], "text.quote")
        }
    }

    var body: some View {
        GeometryReader { geo in
            let side = min(geo.size.width, geo.size.height)
            LinearGradient(colors: style.colors, startPoint: .topLeading, endPoint: .bottomTrailing)
                .overlay(alignment: .topTrailing) {
                    Image(systemName: style.symbol)
                        .font(.system(size: side * 0.62, weight: .bold))
                        .foregroundStyle(.white.opacity(0.16))
                        .offset(x: side * 0.08, y: -side * 0.04)
                }
        }
        .accessibilityHidden(true)
    }
}
