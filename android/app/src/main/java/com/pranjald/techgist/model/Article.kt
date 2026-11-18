package com.pranjald.techgist.model

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.put
import java.time.Instant
import java.time.OffsetDateTime
import kotlin.math.roundToInt

enum class Category(val raw: String) {
    TOP("Top"), AI("AI"), STARTUPS("Startups"), DEV("Dev"), BLOGS("Blogs");

    /** The tab's name on screen. The `Blogs` category holds long reads and analysis. */
    val title: String get() = if (this == BLOGS) "Reads" else raw

    companion object {
        fun of(raw: String?): Category? = entries.firstOrNull { it.raw == raw }
    }
}

/** Sized copies of a story's picture. Every field is optional, so an edition may send any subset. */
data class ImageVariants(val hero: String? = null, val std: String? = null, val thumb: String? = null,
                         val generated: Boolean? = null)

/** The text of a story's alert as the server wrote it. */
data class AlertText(val title: String? = null, val body: String? = null)

/**
 * One story. The fields and their JSON names are the edition's (the same file the iOS app reads).
 * The v1 fields are required as iOS requires them; the fields added for 2.0 are optional and never make an
 * article fail: a missing or mistyped value is null.
 */
data class Article(
    val url: String,
    val title: String,
    val excerpt: String = "",
    val content: String = "",
    val image: String? = null,
    val source: String,
    val category: Category,
    val publishedAt: Instant,
    val points: Int? = null,
    val alsoCoveredBy: List<String>? = null,
    /** The other outlets' own reports, by outlet name, so each name in the coverage list opens its story. */
    val coverageLinks: Map<String, String>? = null,
    val topRank: Int? = null,
    val headline: String? = null,
    val summary: String? = null,
    val summaryShort: String? = null,
    val alertLine: String? = null,
    /** How tap-worthy the story is, 1 to 10. */
    val spice: Int? = null,
    val rank: Int? = null,
    val images: ImageVariants? = null,
    val badge: String? = null,
    val kind: String? = null,
    val updatedAt: Instant? = null,
    val aiText: Boolean? = null,
    val eventId: String? = null,
    val notification: AlertText? = null,
) {
    val id: String get() = url

    /** The headline to show: Techgist's when there is one, otherwise the publisher's. Never altered. */
    val displayTitle: String get() = headline ?: title

    /** Techgist's summary when there is one, otherwise the publisher's excerpt. */
    val displaySummary: String get() = summary ?: excerpt

    val isAISummary: Boolean get() = summary != null && aiText != false

    /** The summary cut to its first two sentences, for cards where the whole summary doesn't fit. */
    val shortSummary: String
        get() = summaryShort?.takeIf { it.isNotEmpty() } ?: firstSentences(2, displaySummary)

    val alertTitle: String get() = notification?.title?.takeIf { it.isNotEmpty() } ?: displayTitle

    val alertBody: String
        get() {
            notification?.body?.takeIf { it.isNotEmpty() }?.let { return it }
            alertLine?.takeIf { it.isNotEmpty() }?.let { return it }
            val first = firstSentences(1, displaySummary)
            if (first.isNotEmpty()) return first
            val others = alsoCoveredBy?.size ?: 0
            return if (others > 0) "$source + $others more" else source
        }

    /** Outlets reporting the story, the lead included. */
    val coverage: Int get() = 1 + (alsoCoveredBy?.size ?: 0)
    val spiceLevel: Double get() = (spice ?: 0) / 10.0
    val isBreaking: Boolean get() = badge == "breaking"
    val isDeveloping: Boolean get() = badge == "developing"
    val isHot: Boolean get() = (spice ?: 0) >= 8

    val kindLabel: String?
        get() = when (kind) {
            "analysis" -> "ANALYSIS"
            "opinion" -> "OPINION"
            "interview" -> "INTERVIEW"
            "release_notes" -> "RELEASE"
            "tutorial" -> "TUTORIAL"
            else -> null
        }

    /** The picture for the card: the large copy on wide phones, the standard one otherwise. */
    fun cardImage(wide: Boolean): String? =
        if (wide) images?.hero ?: images?.std ?: image else images?.std ?: image ?: images?.hero

    /** The picture for lists and the compact card. */
    val thumbImage: String? get() = images?.thumb ?: images?.std ?: image

    /** The card needs the summary, not the article body. */
    fun slim(): Article = if (content.isEmpty()) this else copy(content = "")

    fun toJson(): JsonObject = buildJsonObject {
        put("url", url); put("title", title); put("excerpt", excerpt); put("content", content)
        image?.let { put("image", it) }
        put("source", source); put("category", category.raw); put("publishedAt", publishedAt.toString())
        points?.let { put("points", it) }
        alsoCoveredBy?.let { names -> put("alsoCoveredBy", buildJsonArray { names.forEach { add(JsonPrimitive(it)) } }) }
        coverageLinks?.let { links -> put("coverageLinks", buildJsonObject { links.forEach { (k, v) -> put(k, v) } }) }
        topRank?.let { put("topRank", it) }
        headline?.let { put("headline", it) }
        summary?.let { put("summary", it) }
        summaryShort?.let { put("summaryShort", it) }
        alertLine?.let { put("alertLine", it) }
        spice?.let { put("spice", it) }
        rank?.let { put("rank", it) }
        images?.let { v ->
            put("images", buildJsonObject {
                v.hero?.let { put("hero", it) }; v.std?.let { put("std", it) }; v.thumb?.let { put("thumb", it) }
                v.generated?.let { put("generated", it) }
            })
        }
        badge?.let { put("badge", it) }
        kind?.let { put("type", it) }
        updatedAt?.let { put("updatedAt", it.toString()) }
        aiText?.let { put("aiText", it) }
        eventId?.let { put("eventId", it) }
        notification?.let { n ->
            put("notification", buildJsonObject { n.title?.let { put("title", it) }; n.body?.let { put("body", it) } })
        }
    }

    companion object {
        /** The article in `o`, or null when a required field is missing or of the wrong type. */
        fun fromJson(o: JsonObject): Article? {
            val url = o.str("url")?.takeIf { it.isNotBlank() } ?: return null
            val title = o.str("title") ?: return null
            val excerpt = o.str("excerpt") ?: return null
            val content = o.str("content") ?: return null
            val source = o.str("source") ?: return null
            val category = Category.of(o.str("category")) ?: return null
            val publishedAt = parseDate(o.str("publishedAt")) ?: return null
            if (o.wrongType("points") { it.int() == null } || o.wrongType("headline") { !it.isString } ||
                o.wrongType("summary") { !it.isString } || o.wrongType("topRank") { it.int() == null }) return null
            val others = o["alsoCoveredBy"]?.let { el ->
                if (el is JsonNull) null
                else (el as? JsonArray)?.map { (it as? JsonPrimitive)?.takeIf { p -> p.isString }?.content ?: return null }
                    ?: return null
            }
            val links = (o["coverageLinks"] as? JsonObject)
                ?.mapNotNull { (name, v) -> (v as? JsonPrimitive)?.takeIf { it.isString }?.content?.let { name to it } }
                ?.filter { it.second.startsWith("http://") || it.second.startsWith("https://") }
                ?.toMap()?.takeIf { it.isNotEmpty() }
            val images = (o["images"] as? JsonObject)?.let {
                ImageVariants(it.str("hero"), it.str("std"), it.str("thumb"), it.bool("generated"))
            }
            val note = (o["notification"] as? JsonObject)?.let { AlertText(it.str("title"), it.str("body")) }
            return Article(
                url = url, title = title, excerpt = excerpt, content = content, image = o.str("image"), source = source,
                category = category, publishedAt = publishedAt, points = o.int("points"), alsoCoveredBy = others,
                coverageLinks = links, topRank = o.int("topRank"), headline = o.str("headline"),
                summary = o.str("summary"), summaryShort = o.str("summaryShort"),
                alertLine = o.str("alertLine") ?: o.str("notifLine"), spice = spice(o["spice"]), rank = o.int("rank"),
                images = images, badge = o.str("badge"), kind = o.str("type"), updatedAt = parseDate(o.str("updatedAt")),
                aiText = o.bool("aiText"), eventId = o.str("eventId"), notification = note,
            )
        }

        fun fromJson(text: String): Article? =
            runCatching { Json.parseToJsonElement(text) as? JsonObject }.getOrNull()?.let(::fromJson)

        /** 1 to 10. An older 0-to-1 fraction is read as tenths; 0, negatives and non-numbers are "none". */
        internal fun spice(el: JsonElement?): Int? {
            val p = (el as? JsonPrimitive)?.takeIf { !it.isString } ?: return null
            p.int()?.let { return if (it > 0) minOf(it, 10) else null }
            val n = p.doubleOrNull?.takeIf { it > 0 } ?: return null
            return (if (n <= 1) n * 10 else n).roundToInt().coerceIn(1, 10)
        }

        fun parseDate(s: String?): Instant? = s?.let {
            runCatching { Instant.parse(it) }.getOrNull() ?: runCatching { OffsetDateTime.parse(it).toInstant() }.getOrNull()
        }

        /** The first `count` sentences of `text`. A full stop inside a number or an abbreviation doesn't end one. */
        fun firstSentences(count: Int, text: String): String {
            val chars = text.toCharArray()
            var ends = 0
            var i = 0
            while (i < chars.size) {
                val ch = chars[i]
                if (ch == '.' || ch == '!' || ch == '?') {
                    var j = i + 1
                    while (j < chars.size && chars[j] in "\"”’')") j++
                    val atEnd = j >= chars.size
                    val nextIsSpace = !atEnd && chars[j] == ' '
                    val nextStartsSentence = j + 1 < chars.size &&
                        (chars[j + 1].isUpperCase() || chars[j + 1].isDigit() || chars[j + 1] in "\"“‘")
                    // "U.S. regulators", "Inc. said", "v2.5": a short token before the stop is an abbreviation.
                    var k = i - 1
                    var token = 0
                    var dotted = false
                    while (k >= 0 && chars[k] != ' ') {
                        if (chars[k] == '.') dotted = true
                        token++
                        k--
                    }
                    val abbreviation = ch == '.' && (token <= 2 || dotted) && !atEnd
                    if (atEnd || (nextIsSpace && nextStartsSentence && !abbreviation)) {
                        ends++
                        if (ends == count) return String(chars, 0, j)
                    }
                }
                i++
            }
            return text
        }
    }
}

internal fun JsonPrimitive.int(): Int? {
    if (isString) return null
    content.toIntOrNull()?.let { return it }
    val d = doubleOrNull ?: return null
    return if (d == Math.floor(d) && Math.abs(d) < 1e9) d.toInt() else null
}

internal fun JsonObject.str(key: String): String? = (this[key] as? JsonPrimitive)?.takeIf { it.isString }?.content
internal fun JsonObject.int(key: String): Int? = (this[key] as? JsonPrimitive)?.int()
internal fun JsonObject.bool(key: String): Boolean? = (this[key] as? JsonPrimitive)?.takeIf { !it.isString }?.booleanOrNull

/** True when `key` is present, not null, and `bad` says its value is not what the field holds. */
private inline fun JsonObject.wrongType(key: String, bad: (JsonPrimitive) -> Boolean): Boolean {
    val el = this[key] ?: return false
    if (el is JsonNull) return false
    return el !is JsonPrimitive || bad(el)
}
