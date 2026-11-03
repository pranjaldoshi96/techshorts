import Foundation
#if canImport(FoundationXML)
import FoundationXML
#endif

struct FeedItem {
    var title = ""
    var link = ""
    var html = ""
    var date = ""
    var image: URL?
}

/// Parses RSS 2.0 and Atom feeds into `FeedItem`s.
final class FeedParser: NSObject, XMLParserDelegate {
    private var items: [FeedItem] = []
    private var item: FeedItem?
    private var buffer = ""
    private var bodies: [String: String] = [:]
    private var dates: [String: String] = [:]

    static func parse(_ data: Data) -> [FeedItem] {
        let delegate = FeedParser()
        let parser = XMLParser(data: data)
        parser.delegate = delegate
        _ = parser.parse()
        return delegate.items
    }

    func parser(_ parser: XMLParser, didStartElement elementName: String, namespaceURI: String?,
                qualifiedName qName: String?, attributes a: [String: String] = [:]) {
        let name = elementName.lowercased()
        if name == "item" || name == "entry" {
            item = FeedItem()
            bodies = [:]
            dates = [:]
            return
        }
        guard item != nil else { return }
        buffer = ""
        switch name {
        case "link":
            if let href = a["href"], (a["rel"] ?? "alternate") == "alternate", item!.link.isEmpty { item!.link = href }
        case "media:content", "media:thumbnail":
            let isImage = a["medium"].map { $0 == "image" } ?? a["type"].map { $0.hasPrefix("image") } ?? true
            if item!.image == nil, isImage, let u = a["url"] { item!.image = URL(string: u) }
        case "enclosure":
            if item!.image == nil, a["type"]?.hasPrefix("image") == true, let u = a["url"] { item!.image = URL(string: u) }
        default:
            break
        }
    }

    func parser(_ parser: XMLParser, foundCharacters string: String) {
        if item != nil { buffer += string }
    }

    func parser(_ parser: XMLParser, foundCDATA CDATABlock: Data) {
        if item != nil { buffer += String(decoding: CDATABlock, as: UTF8.self) }
    }

    func parser(_ parser: XMLParser, didEndElement elementName: String, namespaceURI: String?, qualifiedName qName: String?) {
        let name = elementName.lowercased()
        if name == "item" || name == "entry" {
            if var it = item {
                it.html = bodies["content:encoded"] ?? bodies["content"] ?? bodies["description"] ?? bodies["summary"] ?? ""
                it.date = dates["pubdate"] ?? dates["published"] ?? dates["dc:date"] ?? dates["updated"] ?? ""
                items.append(it)
            }
            item = nil
            return
        }
        guard item != nil else { return }
        let text = buffer.trimmingCharacters(in: .whitespacesAndNewlines)
        switch name {
        case "title":
            if item!.title.isEmpty { item!.title = text }
        case "link":
            if item!.link.isEmpty, !text.isEmpty { item!.link = text }
        case "content:encoded", "content", "description", "summary":
            if bodies[name] == nil, !text.isEmpty { bodies[name] = text }
        case "pubdate", "published", "updated", "dc:date":
            if dates[name] == nil, !text.isEmpty { dates[name] = text }
        default:
            break
        }
    }
}

enum FeedDate {
    private static let rfc822: [DateFormatter] = [
        "EEE, dd MMM yyyy HH:mm:ss Z", "EEE, dd MMM yyyy HH:mm:ss zzz", "EEE, d MMM yyyy HH:mm:ss Z",
        "EEE, dd MMM yyyy HH:mm Z", "dd MMM yyyy HH:mm:ss Z", "EEE, dd MMM yyyy HH:mm:ss",
    ].map {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(secondsFromGMT: 0)
        f.dateFormat = $0
        return f
    }

    private static let iso: [ISO8601DateFormatter] = ([
        [.withInternetDateTime], [.withInternetDateTime, .withFractionalSeconds],
    ] as [ISO8601DateFormatter.Options]).map {
        let f = ISO8601DateFormatter()
        f.formatOptions = $0
        return f
    }

    static func parse(_ string: String) -> Date? {
        let s = string.trimmingCharacters(in: .whitespacesAndNewlines)
        for f in iso { if let d = f.date(from: s) { return d } }
        for f in rfc822 { if let d = f.date(from: s) { return d } }
        return nil
    }
}
