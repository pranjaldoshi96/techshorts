package com.pranjald.techgist.model

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.time.Duration
import java.time.Instant

/**
 * A ranked set of stories built and published by the Techgist server. The app shows it while it's under 8 hours
 * old, and fetches the publishers' feeds itself when it isn't.
 */
data class Edition(val publishedAt: Instant, val articles: List<Article>, val degraded: Boolean? = null) {
    fun isFresh(now: Instant = Instant.now()): Boolean =
        Duration.between(publishedAt, now) < MAX_AGE && articles.isNotEmpty()

    companion object {
        val MAX_AGE: Duration = Duration.ofHours(8)

        /** An article that doesn't decode (an unknown category, say) is dropped and the rest kept. */
        fun decode(text: String): Edition? {
            val root = runCatching { Json.parseToJsonElement(text) as? JsonObject }.getOrNull() ?: return null
            val publishedAt = Article.parseDate(root.str("publishedAt")) ?: return null
            val list = root["articles"] as? JsonArray ?: return null
            return Edition(publishedAt, list.mapNotNull { (it as? JsonObject)?.let(Article::fromJson) }, root.bool("degraded"))
        }
    }
}

/** Where the stories on screen came from. */
sealed interface NewsOrigin {
    data class FromEdition(val publishedAt: Instant, val degraded: Boolean) : NewsOrigin
    data object Direct : NewsOrigin
}

/** The stories as last shown, kept in the cache directory so a launch has something on its first frame. */
data class ArticleCache(val updatedAt: Instant, val articles: List<Article>, val editionPublishedAt: Instant? = null,
                        val degraded: Boolean? = null) {
    val origin: NewsOrigin? get() = editionPublishedAt?.let { NewsOrigin.FromEdition(it, degraded ?: false) }

    fun encode(): String = buildJsonObject {
        put("updatedAt", updatedAt.toString())
        put("articles", buildJsonArray { articles.forEach { add(it.toJson()) } })
        editionPublishedAt?.let { put("editionPublishedAt", it.toString()) }
        degraded?.let { put("degraded", it) }
    }.toString()

    companion object {
        const val FILE = "articles.json"

        fun of(articles: List<Article>, at: Instant, origin: NewsOrigin?): ArticleCache {
            val edition = origin as? NewsOrigin.FromEdition
            return ArticleCache(at, articles, edition?.publishedAt, edition?.degraded)
        }

        fun decode(text: String): ArticleCache? {
            val root = runCatching { Json.parseToJsonElement(text) as? JsonObject }.getOrNull() ?: return null
            val updatedAt = Article.parseDate(root.str("updatedAt")) ?: return null
            val list = root["articles"] as? JsonArray ?: return null
            return ArticleCache(updatedAt, list.mapNotNull { (it as? JsonObject)?.let(Article::fromJson) },
                Article.parseDate(root.str("editionPublishedAt")), root.bool("degraded"))
        }
    }
}

fun encodeArticles(articles: List<Article>): String = buildJsonArray { articles.forEach { add(it.toJson()) } }.toString()

fun decodeArticles(text: String): List<Article> =
    (runCatching { Json.parseToJsonElement(text) as? JsonArray }.getOrNull() ?: JsonArray(emptyList()))
        .mapNotNull { (it as? JsonObject)?.let(Article::fromJson) }
