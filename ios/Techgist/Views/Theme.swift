import SwiftUI

/// Design tokens for app 2.0 (spec 5.2). Fonts are text styles, so everything scales with Dynamic Type.
enum Theme {
    // Spacing
    static let horizontal: CGFloat = 20
    static let imageToMeta: CGFloat = 14
    static let metaToHeadline: CGFloat = 8
    static let headlineToBody: CGFloat = 10
    static let bodyToCoverage: CGFloat = 12
    static let footerHeight: CGFloat = 48
    static let imageCorner: CGFloat = 12
    /// The smallest hero picture worth showing; below this the card switches to a thumbnail.
    static let minHeroHeight: CGFloat = 170
    /// Hero height is at most this share of the card's width (16:10).
    static let heroAspect: CGFloat = 0.625
    static let thumbSize: CGFloat = 72

    // Type
    static let headline = Font.system(.title2, design: .serif).weight(.bold)
    static let body = Font.body
    static let meta = Font.footnote
    static let badge = Font.caption2.weight(.heavy)

    // Colors
    static let cardBackground = Color(.systemBackground)
    static let bodyText = Color.primary.opacity(0.85)
    /// Meta text. `secondary` is under 4.5:1 on white at footnote size; this is about 6:1 in light and 8:1 in dark.
    static let metaText = Color.primary.opacity(0.62)
    // Badge fills are darker than systemRed and systemOrange so the white caption on them passes AA (4.5:1 or more).
    static let badgeBreaking = Color(red: 0.80, green: 0.11, blue: 0.11)
    static let badgeDeveloping = Color(red: 0.70, green: 0.33, blue: 0.0)
    static let badgeHot = Color(red: 0.80, green: 0.22, blue: 0.10)
    /// Text on the accent colour: white on the light-mode blue, black on the lighter dark-mode blue.
    static func onAccent(_ scheme: ColorScheme) -> Color { scheme == .dark ? .black : .white }
    static func hairline(_ scheme: ColorScheme) -> Color {
        scheme == .dark ? .white.opacity(0.10) : .black.opacity(0.08)
    }
    /// Darkens the bottom of a picture so the white source label stays readable on any photo.
    static let scrim = LinearGradient(colors: [.black.opacity(0), .black.opacity(0.62)], startPoint: .center, endPoint: .bottom)

    // Motion
    static let chipSpring = Animation.spring(response: 0.35, dampingFraction: 0.85)
    static let fade = Animation.easeOut(duration: 0.25)
}

/// A soft sweep of light across a placeholder while content loads. Static when Reduce Motion is on.
struct Shimmer: ViewModifier {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var phase: CGFloat = -1

    func body(content: Content) -> some View {
        content
            .overlay {
                if !reduceMotion {
                    GeometryReader { geo in
                        LinearGradient(colors: [.clear, .white.opacity(0.28), .clear], startPoint: .leading, endPoint: .trailing)
                            .frame(width: geo.size.width * 0.6)
                            .offset(x: phase * geo.size.width * 1.3)
                            .onAppear {
                                withAnimation(.linear(duration: 1.3).repeatForever(autoreverses: false)) { phase = 1 }
                            }
                    }
                    .allowsHitTesting(false)
                }
            }
            .clipped()
    }
}

extension View {
    func shimmer() -> some View { modifier(Shimmer()) }
}

/// A round stand-in for a publisher's logo: its initial on a colour that is always the same for that name.
struct SourceMonogram: View {
    let name: String
    var size: CGFloat = 20

    private static let palette: [Color] = [
        Color(red: 0.12, green: 0.34, blue: 0.80), Color(red: 0.55, green: 0.20, blue: 0.75),
        Color(red: 0.78, green: 0.20, blue: 0.35), Color(red: 0.72, green: 0.33, blue: 0.05),
        Color(red: 0.05, green: 0.47, blue: 0.43), Color(red: 0.25, green: 0.30, blue: 0.38),
        Color(red: 0.10, green: 0.45, blue: 0.20), Color(red: 0.60, green: 0.15, blue: 0.55),
    ]

    static func color(for name: String) -> Color {
        // A fixed hash: Swift's own differs on every launch.
        let sum = name.unicodeScalars.reduce(UInt32(7)) { ($0 &* 31) &+ $1.value }
        return palette[Int(sum % UInt32(palette.count))]
    }

    var body: some View {
        Text(String(name.first(where: { $0.isLetter || $0.isNumber }) ?? "T").uppercased())
            .font(.system(size: size * 0.55, weight: .bold, design: .rounded))
            .foregroundStyle(.white)
            .frame(width: size, height: size)
            .background(Self.color(for: name), in: Circle())
            .accessibilityHidden(true)
    }
}

/// A small filled capsule: BREAKING, DEVELOPING, HOT.
struct BadgePill: View {
    let text: String
    let color: Color
    var symbol: String?

    var body: some View {
        HStack(spacing: 3) {
            if let symbol { Image(systemName: symbol).imageScale(.small) }
            Text(text).tracking(0.6)
        }
        .font(Theme.badge)
        .foregroundStyle(.white)
        .padding(.horizontal, 7)
        .padding(.vertical, 3)
        .background(color, in: Capsule())
    }
}
