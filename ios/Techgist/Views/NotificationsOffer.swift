import SwiftUI

/// Asked once, after a few stories or the first save: turns on the morning gist and big story alerts together.
/// It shows what an alert will look like, using today's top story.
struct NotificationsOffer: View {
    @Environment(\.dismiss) private var dismiss
    @EnvironmentObject private var store: NewsStore
    @AppStorage(Keys.notificationsOffered) private var notificationsOffered = false
    @AppStorage(Keys.digestEnabled) private var digestEnabled = false
    @AppStorage(Keys.breakingNews) private var breakingEnabled = false
    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        ScrollView {
            VStack(spacing: 18) {
                if let story = store.topStory {
                    AlertPreview(article: story)
                        .padding(.top, 8)
                } else {
                    Image(systemName: "bell.badge.fill")
                        .font(.system(size: 48))
                        .foregroundStyle(.tint)
                        .accessibilityHidden(true)
                }
                Text("Get the big stories first")
                    .font(.system(.title2, design: .serif).weight(.bold))
                    .multilineTextAlignment(.center)
                Text("One morning gist with the day's top story, and an alert when a big story breaks. At most a few a day, never marketing.")
                    .multilineTextAlignment(.center)
                    .foregroundStyle(Theme.metaText)
                Button {
                    Task {
                        // One permission prompt covers both; record both before the sheet goes away.
                        let granted = await StoryNotification.requestPermission()
                        digestEnabled = granted
                        breakingEnabled = granted
                        notificationsOffered = true
                        if granted { await DailyDigest.reschedule(with: store.topStory) }
                        await PushRegistration.sync()
                        dismiss()
                    }
                } label: {
                    Text("Turn on notifications").frame(maxWidth: .infinity)
                }
                .buttonStyle(PrimaryButtonStyle())
                Button {
                    notificationsOffered = true
                    dismiss()
                } label: {
                    // The whole 44-point row is the button, not just the two words.
                    Text("Not now").frame(maxWidth: .infinity, minHeight: 44).contentShape(Rectangle())
                }
            }
            .padding(24)
        }
        .scrollBounceBehavior(.basedOnSize)
        // Half height normally; full height at the accessibility text sizes, where half a screen shows too little.
        .presentationDetents(typeSize.isAccessibilitySize ? [.large] : [.medium, .large])
        // Opaque: on a see-through sheet the story behind shows through the text.
        .presentationBackground(Theme.cardBackground)
        // Swiping the sheet away is not an answer; the offer comes back in a later session.
    }
}

/// A notification banner drawn in the app, so people see what they are agreeing to.
struct AlertPreview: View {
    let article: Article
    @Environment(\.dynamicTypeSize) private var typeSize
    /// A real banner cuts its text; the preview does too, except at the accessibility sizes where two lines hold
    /// only a few words.
    private var cuts: Bool { !typeSize.isAccessibilitySize }

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image("Logo")
                .resizable()
                .frame(width: 38, height: 38)
                .clipShape(.rect(cornerRadius: 9))
            VStack(alignment: .leading, spacing: 2) {
                Text(article.alertTitle)
                    .font(.subheadline.weight(.semibold))
                    .lineLimit(cuts ? 2 : nil)
                Text(article.alertBody)
                    .font(.subheadline)
                    .lineLimit(cuts ? 3 : nil)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            StoryImage(article: article, url: article.thumbImage, maxPixel: ImageLoader.thumbPixels)
                .frame(width: 44, height: 44)
                .clipShape(.rect(cornerRadius: 8))
        }
        .padding(12)
        .background(Color(.secondarySystemBackground), in: .rect(cornerRadius: 20))
        .overlay(RoundedRectangle(cornerRadius: 20).strokeBorder(Color.primary.opacity(0.08)))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Example alert: \(article.alertTitle). \(article.alertBody)")
    }
}
