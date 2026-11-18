package com.pranjald.techgist.data

import com.pranjald.techgist.model.Article
import com.pranjald.techgist.model.Category
import org.xml.sax.Attributes
import org.xml.sax.InputSource
import org.xml.sax.helpers.DefaultHandler
import java.io.ByteArrayInputStream
import java.net.URI
import java.time.Instant
import java.time.OffsetDateTime
import java.time.ZoneOffset
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import java.util.Locale
import javax.xml.parsers.SAXParserFactory
import kotlin.math.abs
import kotlin.math.ln
import kotlin.math.log2
import kotlin.math.max
import kotlin.math.min
import kotlin.math.pow

// The fallback for when the edition is unavailable or over 8 hours old: the publishers' own feeds, fetched,
// de-duplicated and ranked on the phone. A port of the iOS app's FeedSource, FeedParser, HTMLText and Ranker.

data class FeedSource(val name: String, val category: Category, val url: String, val weight: Double = 1.0) {
    companion object {
        val all: List<FeedSource> = listOf(
            FeedSource("TechCrunch", Category.TOP, "https://techcrunch.com/feed/"),
            FeedSource("The Verge", Category.TOP, "https://www.theverge.com/rss/index.xml"),
            FeedSource("Ars Technica", Category.TOP, "https://feeds.arstechnica.com/arstechnica/index"),
            FeedSource("Wired", Category.TOP, "https://www.wired.com/feed/rss"),
            FeedSource("Hacker News", Category.TOP, "https://hnrss.org/frontpage?points=150"),
            FeedSource("BBC Technology", Category.TOP, "https://feeds.bbci.co.uk/news/technology/rss.xml"),
            FeedSource("The Guardian", Category.TOP, "https://www.theguardian.com/uk/technology/rss"),
            FeedSource("New York Times", Category.TOP, "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml"),
            FeedSource("CNBC Tech", Category.TOP, "https://www.cnbc.com/id/19854910/device/rss/rss.html"),
            FeedSource("Engadget", Category.TOP, "https://www.engadget.com/rss.xml"),

            FeedSource("MIT Tech Review", Category.AI, "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
            FeedSource("The Decoder", Category.AI, "https://the-decoder.com/feed/"),
            FeedSource("Google AI", Category.AI, "https://blog.google/technology/ai/rss/"),
            FeedSource("OpenAI", Category.AI, "https://openai.com/news/rss.xml"),
            FeedSource("Latent Space", Category.AI, "https://www.latent.space/feed"),

            FeedSource("TechCrunch Startups", Category.STARTUPS, "https://techcrunch.com/category/startups/feed/"),
            FeedSource("Product Hunt", Category.STARTUPS, "https://www.producthunt.com/feed", 0.7),
            FeedSource("Lenny's Newsletter", Category.STARTUPS, "https://www.lennysnewsletter.com/feed"),

            FeedSource("GitHub Blog", Category.DEV, "https://github.blog/feed/"),
            FeedSource("Cloudflare", Category.DEV, "https://blog.cloudflare.com/rss/"),
            FeedSource("Changelog", Category.DEV, "https://changelog.com/feed"),
            FeedSource("DEV Community", Category.DEV, "https://dev.to/feed", 0.4),

            FeedSource("Simon Willison", Category.BLOGS, "https://simonwillison.net/atom/everything/"),
            FeedSource("Pragmatic Engineer", Category.BLOGS, "https://newsletter.pragmaticengineer.com/feed"),
            FeedSource("Stratechery", Category.BLOGS, "https://stratechery.com/feed/"),
            FeedSource("Martin Fowler", Category.BLOGS, "https://martinfowler.com/feed.atom"),
            FeedSource("Julia Evans", Category.BLOGS, "https://jvns.ca/atom.xml"),
        )

        private val byName = all.associateBy { it.name }

        fun weight(name: String): Double = byName[name]?.weight ?: 1.0

        /** Feeds from the same site (TechCrunch and TechCrunch Startups) count as one publisher. */
        fun publisher(name: String): String {
            val host = byName[name]?.let { runCatching { URI(it.url).host }.getOrNull() } ?: return name
            val parts = host.split(".")
            return if (parts.size >= 2) parts[parts.size - 2] else host
        }
    }
}

data class FeedItem(var title: String = "", var link: String = "", var html: String = "", var date: String = "",
                    var image: String? = null)

/** Parses RSS 2.0 and Atom feeds into `FeedItem`s. */
object FeedParser {
    fun parse(data: ByteArray): List<FeedItem> {
        val items = ArrayList<FeedItem>()
        val handler = object : DefaultHandler() {
            var item: FeedItem? = null
            val buffer = StringBuilder()
            val bodies = HashMap<String, String>()
            val dates = HashMap<String, String>()

            override fun startElement(uri: String?, localName: String?, qName: String?, a: Attributes) {
                val name = (qName ?: localName ?: "").lowercase()
                if (name == "item" || name == "entry") {
                    item = FeedItem(); bodies.clear(); dates.clear()
                    return
                }
                val it = item ?: return
                buffer.setLength(0)
                when (name) {
                    "link" -> {
                        val href = a.getValue("href")
                        if (href != null && (a.getValue("rel") ?: "alternate") == "alternate" && it.link.isEmpty()) it.link = href
                    }
                    "media:content", "media:thumbnail" -> {
                        val isImage = a.getValue("medium")?.let { m -> m == "image" }
                            ?: a.getValue("type")?.startsWith("image") ?: true
                        if (it.image == null && isImage) a.getValue("url")?.let { u -> it.image = u }
                    }
                    "enclosure" ->
                        if (it.image == null && a.getValue("type")?.startsWith("image") == true) {
                            a.getValue("url")?.let { u -> it.image = u }
                        }
                }
            }

            override fun characters(ch: CharArray, start: Int, length: Int) {
                if (item != null) buffer.appendRange(ch, start, start + length)
            }

            override fun endElement(uri: String?, localName: String?, qName: String?) {
                val name = (qName ?: localName ?: "").lowercase()
                val it = item ?: return
                if (name == "item" || name == "entry") {
                    it.html = bodies["content:encoded"] ?: bodies["content"] ?: bodies["description"] ?: bodies["summary"] ?: ""
                    it.date = dates["pubdate"] ?: dates["published"] ?: dates["dc:date"] ?: dates["updated"] ?: ""
                    items.add(it)
                    item = null
                    return
                }
                val text = buffer.toString().trim()
                when (name) {
                    "title" -> if (it.title.isEmpty()) it.title = text
                    "link" -> if (it.link.isEmpty() && text.isNotEmpty()) it.link = text
                    "content:encoded", "content", "description", "summary" ->
                        if (name !in bodies && text.isNotEmpty()) bodies[name] = text
                    "pubdate", "published", "updated", "dc:date" ->
                        if (name !in dates && text.isNotEmpty()) dates[name] = text
                }
            }
        }
        runCatching {
            val factory = SAXParserFactory.newInstance()
            factory.isNamespaceAware = false
            factory.newSAXParser().parse(InputSource(ByteArrayInputStream(data)), handler)
        }
        return items
    }
}

object FeedDate {
    private val rfc822 = listOf(
        "EEE, dd MMM yyyy HH:mm:ss Z", "EEE, dd MMM yyyy HH:mm:ss zzz", "EEE, d MMM yyyy HH:mm:ss Z",
        "EEE, dd MMM yyyy HH:mm Z", "dd MMM yyyy HH:mm:ss Z",
    ).map { DateTimeFormatter.ofPattern(it, Locale.US) }
    private val noZone = DateTimeFormatter.ofPattern("EEE, dd MMM yyyy HH:mm:ss", Locale.US).withZone(ZoneOffset.UTC)

    fun parse(string: String): Instant? {
        val s = string.trim()
        runCatching { return Instant.parse(s) }
        runCatching { return OffsetDateTime.parse(s).toInstant() }
        for (f in rfc822) runCatching { return ZonedDateTime.parse(s, f).toInstant() }
        runCatching { return ZonedDateTime.parse(s, noZone).toInstant() }
        return null
    }
}

/** Lightweight HTML-to-text helpers. Feed bodies are small, so regex is good enough. */
object HtmlText {
    private val blocks = Regex("<(script|style|figcaption)[^>]*>[\\s\\S]*?</\\1>", RegexOption.IGNORE_CASE)
    private val tags = Regex("<[^>]+>")
    private val spaces = Regex("\\s+")

    fun plain(html: String): String =
        decodeEntities(html.replace(blocks, " ").replace(tags, " ")).replace(spaces, " ").trim()

    private val boilerplate = listOf(
        "The post .* appeared first on .*$", "Continue reading.*$", "Read more.*$", "Read the full .*$",
        "\\[(…|\\.\\.\\.)\\]\\s*$", "\\s*Discussion \\| Link\\s*$",
        "^This (story|article) originally appeared in .*?sign up here\\s*\\.\\s*",
        "\\s*(Via \\S.{0,60}?)?\\s*Tags: [^.!?]*$",
    ).map { Regex(it, RegexOption.IGNORE_CASE) }

    /** Strips publisher boilerplate such as "The post … appeared first on …". */
    fun clean(text: String): String = boilerplate.fold(text) { t, p -> t.replace(p, "") }.trim()

    /** Cuts to `maxWords`, preferring to end on a full sentence. */
    fun truncate(text: String, maxWords: Int): String {
        val words = text.split(" ").filter { it.isNotEmpty() }
        if (words.size <= maxWords) return text
        val cut = words.take(maxWords).joinToString(" ")
        val last = listOf(". ", "? ", "! ").maxOf { cut.lastIndexOf(it) }
        if (last > cut.length / 2) return cut.substring(0, last + 1)
        return cut.trim(',', ';', ':', '-', '–', ' ') + "…"
    }

    fun firstImage(html: String): String? =
        allMatches("<img[^>]+src=[\"']([^\"']+)[\"']", html).map(::decodeEntities).firstOrNull {
            it.startsWith("https://") && "feedburner" !in it && "pixel" !in it && "gravatar" !in it
        }

    fun firstMatch(pattern: String, s: String): String? =
        Regex(pattern, RegexOption.IGNORE_CASE).find(s)?.let(::group)

    fun allMatches(pattern: String, s: String): List<String> =
        Regex(pattern, RegexOption.IGNORE_CASE).findAll(s).mapNotNull(::group).toList()

    private fun group(m: MatchResult): String? = m.groups.drop(1).firstOrNull { it != null }?.value

    private val named = mapOf(
        "amp" to "&", "lt" to "<", "gt" to ">", "quot" to "\"", "apos" to "'", "nbsp" to " ", "hellip" to "…",
        "mdash" to "—", "ndash" to "–", "rsquo" to "’", "lsquo" to "‘", "rdquo" to "”", "ldquo" to "“",
        "copy" to "©", "trade" to "™",
    )

    fun decodeEntities(s: String): String {
        if ('&' !in s) return s
        val out = StringBuilder(s.length)
        var i = 0
        while (i < s.length) {
            if (s[i] == '&') {
                val semi = s.indexOf(';', i)
                if (semi in (i + 1)..(i + 11)) {
                    val decoded = decodeOne(s.substring(i + 1, semi))
                    if (decoded != null) {
                        out.append(decoded)
                        i = semi + 1
                        continue
                    }
                }
            }
            out.append(s[i])
            i++
        }
        return out.toString()
    }

    private fun decodeOne(name: String): String? {
        val scalar = when {
            name.startsWith("#x") || name.startsWith("#X") -> name.drop(2).toIntOrNull(16)
            name.startsWith("#") -> name.drop(1).toIntOrNull()
            else -> return named[name.lowercase()]
        }
        return scalar?.takeIf { Character.isValidCodePoint(it) }?.let { String(Character.toChars(it)) }
    }
}

/** Drops shopping deals, buying guides and sponsored posts, which aren't news. */
object ContentFilter {
    private val title = Regex(listOf(
        "\\d+% off", "\\$\\d+ off", "\\bon sale\\b", "\\b(best|lowest) price", "all-time low", "black friday", "cyber monday",
        "prime day", "gift guide", "coupon", "promo code", "\\bbest\\b.{0,60}\\bdeals?\\b", "\\bdeals? of the (day|week)\\b",
        "\\bsponsored\\b", "^(the )?\\d+ best\\b", "\\bbest\\b.*\\(20\\d\\d\\)",
        "\\bfounder summit\\b", "\\btechcrunch disrupt\\b", "\\bstrictlyvc\\b", "\\btickets?\\b.{0,40}\\b(sale|save|discount|price)",
    ).joinToString("|"), RegexOption.IGNORE_CASE)

    private val path = Regex(listOf("/deals?/", "deal-sale", "-deals$", "coupon", "gift-guide", "/sponsored")
        .joinToString("|"), RegexOption.IGNORE_CASE)

    fun isLowValue(article: Article): Boolean =
        title.containsMatchIn(article.title) ||
            path.containsMatchIn(runCatching { URI(article.url).path }.getOrNull() ?: "")
}

private val youtubeSuffix = Regex("\\s+[-–|]\\s+YouTube$")
private val hnPoints = "Points: (\\d+)"

/** A feed item as a story, or null when it has no title, link or date. */
fun articleOf(item: FeedItem, source: FeedSource): Article? {
    val rawTitle = HtmlText.plain(item.title)
    val title = rawTitle.replace(youtubeSuffix, "")
    val url = item.link.trim()
    if (title.isEmpty() || !(url.startsWith("https://") || url.startsWith("http://"))) return null
    if (runCatching { URI(url).host }.getOrNull() == null) return null
    val date = FeedDate.parse(item.date) ?: return null

    var body = HtmlText.clean(HtmlText.plain(item.html))
    for (prefix in listOf(rawTitle, title)) {
        if (body.length > prefix.length && body.startsWith(prefix, ignoreCase = true)) {
            body = body.drop(prefix.length).trim()
            break
        }
    }
    // Hacker News bodies are just "Article URL: … Comments URL: …".
    val usable = !body.startsWith("Article URL:") && !body.equals(title, ignoreCase = true)
    return Article(
        url = url, title = title,
        excerpt = if (usable) HtmlText.truncate(body, 60) else "",
        content = "",
        image = item.image ?: HtmlText.firstImage(item.html),
        source = source.name, category = source.category, publishedAt = date,
        points = HtmlText.firstMatch(hnPoints, item.html)?.toIntOrNull(),
    )
}

/**
 * Orders stories by importance rather than time alone: coverage by several publishers (matched by headline),
 * Hacker News points, source weight, and age. Same-story duplicates within a category collapse into one card.
 */
object Ranker {
    private class Scored(val article: Article, val score: Double, val group: Int)

    /** How many of each tab's best stories also appear in Top. */
    private val topPicksPerTab = mapOf(Category.AI to 6, Category.STARTUPS to 4, Category.DEV to 3, Category.BLOGS to 3)

    fun rank(articles: List<Article>, now: Instant = Instant.now()): List<Article> {
        val tokens = articles.map { titleTokens(it.title) }
        val idf = inverseDocumentFrequency(tokens)
        val groups = clusters(articles, tokens, idf)

        val ranked = HashMap<Category, MutableList<Scored>>()
        groups.forEachIndexed { groupIndex, group ->
            val members = group.map { articles[it] }
            val sourceNames = members.flatMap { listOf(it.source) + (it.alsoCoveredBy ?: emptyList()) }
            val points = members.mapNotNull { it.points }.maxOrNull()
            for (category in Category.entries) {
                val inCategory = members.filter { it.category == category }
                // Blog posts on the same topic are different opinions, not duplicates, so keep them all.
                val leads = if (category == Category.BLOGS) inCategory else listOfNotNull(inCategory.maxByOrNull(::quality))
                for (lead in leads) {
                    val others = otherPublishers(sourceNames, lead.source)
                    val a = lead.copy(alsoCoveredBy = others.takeIf { it.isNotEmpty() }, points = points, topRank = null)
                    ranked.getOrPut(category) { ArrayList() }.add(Scored(a, score(a, others.size + 1, now), groupIndex))
                }
            }
        }

        // The Top tab: Top sources plus the strongest stories from every other tab, one card per story.
        val topPicks = ArrayList(ranked[Category.TOP] ?: emptyList())
        val topGroups = topPicks.mapTo(HashSet()) { it.group }
        for (category in Category.entries) {
            if (category == Category.TOP) continue
            val best = (ranked[category] ?: emptyList()).sortedByDescending { it.score }
            for (pick in best.take(topPicksPerTab[category] ?: 0)) if (topGroups.add(pick.group)) topPicks.add(pick)
        }
        val topOrder = HashMap<String, Int>()
        spreadSources(topPicks).forEachIndexed { i, a -> topOrder.merge(a.id, i, ::minOf) }

        return Category.entries.flatMap { category ->
            spreadSources(ranked[category] ?: emptyList()).map { it.copy(topRank = topOrder[it.id]) }
        }
    }

    fun score(article: Article, coverage: Int, now: Instant): Double {
        val ageHours = max(0.0, (now.epochSecond - article.publishedAt.epochSecond) / 3600.0)
        val coverageBoost = 1 + 0.8 * min(coverage - 1, 3)
        val pointsBoost = article.points?.let { 1 + min(1.0, max(0.0, log2(it / 150.0)) * 0.5) } ?: 1.0
        // Half the score after 12 hours, a third after a day.
        return FeedSource.weight(article.source) * coverageBoost * pointsBoost / (ageHours + 12)
    }

    /** Picks which copy of a story to show: trusted source, then one with an image and text, then the earliest. */
    private fun quality(a: Article): Double =
        FeedSource.weight(a.source) + (if (a.image != null) 0.2 else 0.0) + (if (a.excerpt.isEmpty()) 0.0 else 0.2) -
            a.publishedAt.epochSecond / 1e12

    private fun otherPublishers(names: List<String>, lead: String): List<String> {
        val seen = hashSetOf(FeedSource.publisher(lead))
        return names.filter { seen.add(FeedSource.publisher(it)) }
    }

    /** Orders by score, but each further story from a source already shown counts for 25% less. */
    private fun spreadSources(scored: List<Scored>): List<Article> {
        val pending = ArrayList(scored)
        val shown = HashMap<String, Int>()
        val out = ArrayList<Article>(scored.size)
        while (pending.isNotEmpty()) {
            var best = 0
            var bestScore = Double.NEGATIVE_INFINITY
            for (i in pending.indices) {
                val adjusted = pending[i].score * 0.75.pow(shown[pending[i].article.source] ?: 0)
                if (adjusted > bestScore) { bestScore = adjusted; best = i }
            }
            val article = pending.removeAt(best).article
            shown.merge(article.source, 1, Int::plus)
            out.add(article)
        }
        return out
    }

    private val stopwords = setOf(
        "the", "and", "for", "with", "from", "that", "this", "its", "are", "was", "has", "have", "how", "why", "what",
        "who", "when", "where", "which", "will", "into", "about", "after", "over", "now", "new", "more", "can", "you",
        "your", "our", "says", "say", "said", "just", "out", "not", "but", "all", "get", "gets", "here", "than", "they",
        "their", "his", "her", "report", "reportedly", "launches", "announces", "update", "week", "today", "first",
        "could", "would",
    )
    private val possessive = Regex("['’]s\\b")
    private val separators = Regex("[^\\p{L}\\p{N}]+")

    fun titleTokens(title: String): Set<String> =
        title.lowercase().replace(possessive, "").split(separators)
            .filter { (it.length >= 3 || it.any(Char::isDigit)) && it.isNotEmpty() && it !in stopwords }.toSet()

    private fun inverseDocumentFrequency(docs: List<Set<String>>): Map<String, Double> {
        val counts = HashMap<String, Int>()
        for (doc in docs) for (t in doc) counts.merge(t, 1, Int::plus)
        val n = max(docs.size, 1).toDouble()
        return counts.mapValues { ln(n / it.value) + 0.1 }
    }

    /** Two headlines are the same story when they share at least three words and the rarer ones mostly overlap. */
    fun sameStory(a: Set<String>, b: Set<String>, idf: Map<String, Double>): Boolean {
        val shared = a intersect b
        if (shared.size < 3) return false
        val weight = { s: Set<String> -> s.sumOf { idf[it] ?: 0.0 } }
        return weight(shared) / min(weight(a), weight(b)) >= 0.4
    }

    /** Groups article indices into stories with union–find. */
    private fun clusters(articles: List<Article>, tokens: List<Set<String>>, idf: Map<String, Double>): List<List<Int>> {
        val parent = IntArray(articles.size) { it }
        fun root(start: Int): Int {
            var i = start
            while (parent[i] != i) { parent[i] = parent[parent[i]]; i = parent[i] }
            return i
        }
        for (i in articles.indices) for (j in i + 1 until articles.size) {
            if (abs(articles[i].publishedAt.epochSecond - articles[j].publishedAt.epochSecond) < 3 * 86_400 &&
                sameStory(tokens[i], tokens[j], idf)) parent[root(j)] = root(i)
        }
        return articles.indices.groupBy(::root).values.toList()
    }
}
