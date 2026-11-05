import SwiftUI
import UIKit

/// Shares a story as a picture of its card with a Techgist footer, plus a short line linking to the app.
enum StoryShare {
    static let appURL = URL(string: "https://apps.apple.com/app/techgist-ai-startup-news/id6830218043")!

    struct Payload: Identifiable {
        let id = UUID()
        let image: UIImage
        let text: String
    }

    @MainActor
    static func payload(for article: Article, summary: String) async -> Payload? {
        var photo: UIImage?
        if let url = article.image, let (data, _) = try? await URLSession.shared.data(from: url) {
            photo = UIImage(data: data)
        }
        let renderer = ImageRenderer(content: ShareCard(article: article, summary: summary, photo: photo)
            .environment(\.colorScheme, .light))
        renderer.scale = 3
        guard let image = renderer.uiImage else { return nil }
        return Payload(image: image, text: "\(article.displayTitle)\n\nRead tech news in 60 words on Techgist. Get it on the App Store: \(appURL.absoluteString)")
    }
}

/// The shared picture: the story card as it looks in the app, with a Techgist footer as the watermark.
private struct ShareCard: View {
    let article: Article
    let summary: String
    let photo: UIImage?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Group {
                if let photo {
                    Image(uiImage: photo).resizable().scaledToFill()
                } else {
                    StoryCover(article: article)
                }
            }
            .frame(width: 390, height: 230)
            .clipped()

            VStack(alignment: .leading, spacing: 10) {
                Text(article.source.uppercased())
                    .font(.caption.bold())
                    .foregroundStyle(Color.accentColor)
                Text(article.displayTitle)
                    .font(.title3.weight(.bold))
                    .foregroundStyle(.black)
                    .fixedSize(horizontal: false, vertical: true)
                if !summary.isEmpty {
                    Text(summary)
                        .font(.body)
                        .foregroundStyle(.black.opacity(0.65))
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(20)

            HStack(spacing: 10) {
                Image("Logo")
                    .resizable()
                    .frame(width: 30, height: 30)
                    .clipShape(.rect(cornerRadius: 7))
                VStack(alignment: .leading, spacing: 1) {
                    Text("Techgist").font(.subheadline.bold())
                    Text("Tech news in 60 words").font(.caption)
                }
                Spacer()
                VStack(alignment: .trailing, spacing: 1) {
                    Text("Download free on the").font(.caption2).opacity(0.85)
                    Text("App Store").font(.subheadline.bold())
                }
            }
            .foregroundStyle(.white)
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
            .background(Color.accentColor)
        }
        .frame(width: 390)
        .background(.white)
    }
}

extension StoryShare {
    /// Shows the system share sheet from the frontmost screen. SwiftUI sheets attached to cards inside the
    /// paged feed don't present reliably, so this goes through UIKit.
    @MainActor
    static func present(_ payload: Payload) {
        let scene = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first
        guard var top = scene?.keyWindow?.rootViewController else { return }
        while let presented = top.presentedViewController { top = presented }
        top.present(UIActivityViewController(activityItems: [payload.image, payload.text], applicationActivities: nil),
                    animated: true)
    }
}
