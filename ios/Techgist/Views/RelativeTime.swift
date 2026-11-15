import Foundation

/// Short, honest ages for cards: `now`, `12m`, `3h`, `Yesterday`, `Nov 4`. A date in the future reads as `now`.
enum RelativeTime {
    static func short(_ date: Date, now: Date = Date(), calendar: Calendar = .current) -> String {
        let seconds = max(0, now.timeIntervalSince(date))
        if seconds < 60 { return "now" }
        if seconds < 3600 { return "\(Int(seconds / 60))m" }
        if seconds < 24 * 3600 { return "\(Int(seconds / 3600))h" }
        let days = calendar.dateComponents([.day], from: calendar.startOfDay(for: date), to: calendar.startOfDay(for: now)).day ?? 2
        if days <= 1 { return "Yesterday" }
        return date.formatted(.dateTime.month(.abbreviated).day().locale(calendar.locale ?? .current))
    }

    /// The same, in words VoiceOver reads well: "12 minutes ago".
    static func spoken(_ date: Date, now: Date = Date(), calendar: Calendar = .current) -> String {
        let seconds = max(0, now.timeIntervalSince(date))
        if seconds < 60 { return "just now" }
        if seconds < 3600 {
            let m = Int(seconds / 60)
            return m == 1 ? "1 minute ago" : "\(m) minutes ago"
        }
        if seconds < 24 * 3600 {
            let h = Int(seconds / 3600)
            return h == 1 ? "1 hour ago" : "\(h) hours ago"
        }
        let text = short(date, now: now, calendar: calendar)
        return text == "Yesterday" ? "yesterday" : text
    }

    /// The time line for a card. A story updated more than an hour after it was first reported says so.
    static func label(for article: Article, now: Date = Date()) -> String {
        if let updated = article.updatedAt, updated.timeIntervalSince(article.publishedAt) > 3600 {
            return "Updated \(short(updated, now: now))"
        }
        return short(article.publishedAt, now: now)
    }

    static func spokenLabel(for article: Article, now: Date = Date()) -> String {
        if let updated = article.updatedAt, updated.timeIntervalSince(article.publishedAt) > 3600 {
            return "updated \(spoken(updated, now: now))"
        }
        return spoken(article.publishedAt, now: now)
    }
}
