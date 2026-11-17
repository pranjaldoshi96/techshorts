package com.pranjald.techgist.data

import com.pranjald.techgist.model.Article
import com.pranjald.techgist.model.Category
import com.pranjald.techgist.model.Edition
import com.pranjald.techgist.model.NewsOrigin
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.Cache
import okhttp3.CacheControl
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.net.URI
import java.time.Duration
import java.time.Instant
import java.util.concurrent.TimeUnit

data class FeedResult(val articles: List<Article>, val failed: List<String>, val origin: NewsOrigin)

/** A GET that returns the body of a 2xx answer, or null. */
fun interface Http {
    suspend fun get(url: String, timeoutSeconds: Long): ByteArray?
}

class OkHttpGet(cacheDir: File?) : Http {
    private val client: OkHttpClient = OkHttpClient.Builder()
        .apply { if (cacheDir != null) cache(Cache(File(cacheDir, "http"), 8L * 1024 * 1024)) }
        .build()

    override suspend fun get(url: String, timeoutSeconds: Long): ByteArray? = withContext(Dispatchers.IO) {
        runCatching {
            val request = Request.Builder().url(url)
                .header("User-Agent", "Techgist/2.0 (Android RSS reader)")
                // Always ask the server, with the cached copy's ETag: an unchanged edition costs a few hundred bytes.
                .cacheControl(CacheControl.Builder().noCache().build())
                .build()
            client.newBuilder().callTimeout(timeoutSeconds, TimeUnit.SECONDS).build().newCall(request).execute().use {
                if (it.isSuccessful) it.body?.bytes() else null
            }
        }.getOrNull()
    }
}

/** The published edition while it's fresh; otherwise the publishers' feeds, fetched directly. */
class FeedService(private val http: Http, private val editionUrl: String, private val now: () -> Instant = Instant::now) {

    suspend fun latest(onProgress: (suspend (List<Article>) -> Unit)? = null): FeedResult {
        val edition = fetchEdition()
        if (edition != null && edition.isFresh(now())) {
            return FeedResult(edition.articles, emptyList(),
                NewsOrigin.FromEdition(edition.publishedAt, edition.degraded ?: false))
        }
        val (articles, failed) = fetchAll(onProgress = onProgress)
        return FeedResult(articles, failed, NewsOrigin.Direct)
    }

    suspend fun fetchEdition(): Edition? =
        http.get(editionUrl, 10)?.let { bytes -> withContext(Dispatchers.Default) { Edition.decode(String(bytes)) } }

    /** `onProgress` receives the merged stories so far after each feed finishes. */
    suspend fun fetchAll(sources: List<FeedSource> = FeedSource.all,
                         onProgress: (suspend (List<Article>) -> Unit)? = null): Pair<List<Article>, List<String>> =
        coroutineScope {
            val done = Channel<Pair<FeedSource, List<Article>?>>(sources.size)
            for (source in sources) launch { done.send(source to fetch(source)) }
            val out = ArrayList<Pair<FeedSource, List<Article>?>>()
            repeat(sources.size) {
                val r = done.receive()
                out.add(r)
                if (onProgress != null && r.second?.isNotEmpty() == true) {
                    onProgress(withContext(Dispatchers.Default) { merge(out.mapNotNull { it.second }, now()) })
                }
            }
            val failed = out.filter { it.second == null }.map { it.first.name }.sorted()
            withContext(Dispatchers.Default) { merge(out.mapNotNull { it.second }, now()) } to failed
        }

    private suspend fun fetch(source: FeedSource): List<Article>? {
        val data = http.get(source.url, 20) ?: return null
        return withContext(Dispatchers.Default) {
            // Blogs post less often, so allow older posts there.
            val maxAgeDays = if (source.category == Category.BLOGS) 45L else 7L
            val cutoff = now().minus(Duration.ofDays(maxAgeDays))
            FeedParser.parse(data).mapNotNull { articleOf(it, source) }
                .filter { it.publishedAt > cutoff && !ContentFilter.isLowValue(it) }
                .take(PER_FEED_LIMIT)
        }
    }

    companion object {
        const val PER_FEED_LIMIT = 12

        /**
         * The same link can appear in several feeds. Keep one copy in the more specific category, but remember the
         * other sources and HN points for ranking.
         */
        fun merge(feeds: List<List<Article>>, now: Instant = Instant.now()): List<Article> {
            val byKey = LinkedHashMap<String, Article>()
            for (article in feeds.flatten()) {
                val key = dedupeKey(article.url)
                val existing = byKey[key]
                if (existing == null) { byKey[key] = article; continue }
                val preferNew = existing.category == Category.TOP && article.category != Category.TOP
                val kept = if (preferNew) article else existing
                val dropped = if (preferNew) existing else article
                byKey[key] = kept.copy(
                    points = listOfNotNull(kept.points, dropped.points).maxOrNull(),
                    alsoCoveredBy = (kept.alsoCoveredBy ?: emptyList()) + dropped.source + (dropped.alsoCoveredBy ?: emptyList()),
                )
            }
            return Ranker.rank(byKey.values.toList(), now)
        }

        fun dedupeKey(url: String): String {
            val u = runCatching { URI(url) }.getOrNull() ?: return url.lowercase()
            return ((u.host ?: "") + (u.path ?: "")).lowercase().trim('/')
        }
    }
}
