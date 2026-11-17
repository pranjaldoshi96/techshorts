package com.pranjald.techgist.data

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import com.pranjald.techgist.model.Article
import com.pranjald.techgist.model.ArticleCache
import com.pranjald.techgist.model.Category
import com.pranjald.techgist.model.NewsOrigin
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.async
import kotlinx.coroutines.delay
import java.time.Duration
import java.time.Instant

typealias Progress = suspend (List<Article>) -> Unit

/** The stories on screen and everything that changes them. A port of the iOS app's NewsStore, rule for rule. */
class NewsStore(
    /** Refreshes run here, so one that was asked for runs to its end whatever happens to the screen that asked. */
    private val scope: CoroutineScope,
    val readState: ReadState,
    private val disk: Disk,
    private val settings: KeyValue,
    private val service: suspend (Progress?) -> FeedResult,
    /** Called when a new set of stories is on screen (for alerts that should not repeat them, and the digest). */
    private val onApplied: (NewsStore) -> Unit = {},
    private val now: () -> Instant = Instant::now,
    cache: ArticleCache? = disk.loadCache(),
) {
    /** Everything in the current edition (or the direct-feed fallback). */
    var articles: List<Article> by mutableStateOf((cache?.articles ?: emptyList()).map { it.slim() }); private set
    /** What each tab shows, in order. Rebuilt only when a new edition is applied, never while someone is reading. */
    var feeds: Map<Category, List<Article>> by mutableStateOf(emptyMap()); private set
    var lastUpdated: Instant? by mutableStateOf(cache?.updatedAt); private set
    /** True while the first stories are loading (nothing on screen yet). */
    var isRefreshing by mutableStateOf(false); private set
    /** Bumped when a refresh the reader asked for brings stories back; the feed answers with a tap of haptics. */
    var refreshesCompleted by mutableIntStateOf(0); private set
    var failedSources: List<String> by mutableStateOf(emptyList()); private set
    var origin: NewsOrigin? by mutableStateOf(cache?.origin); private set
    /** True when the last attempt to load stories got nothing back (usually no connection). */
    var isOffline by mutableStateOf(false); private set
    var saved: List<Article> by mutableStateOf(disk.loadSaved()); private set
    private var categoryState by mutableStateOf(Category.TOP)
    var category: Category
        get() = categoryState
        set(value) { if (value != categoryState) { categoryState = value; updateWaitingCount() } }
    /** How many unseen stories the waiting edition has near the top of the current tab; the pill shows this. */
    var waitingCount by mutableIntStateOf(0); private set
    /** Bumped when the feed should jump back to the first story. */
    var scrollToTopRequests by mutableIntStateOf(0); private set
    /** A story to bring on screen and highlight, set when an alert or a search result is tapped. */
    var focus: Focus? by mutableStateOf(null)
    private var disabledState by mutableStateOf(settings.strings(Keys.DISABLED_SOURCES))
    var disabledSources: Set<String>
        get() = disabledState
        set(value) {
            if (value == disabledState) return
            disabledState = value
            settings.put(Keys.DISABLED_SOURCES, value)
            rebuildFeeds()
        }

    data class Focus(val id: String, val category: Category, val token: Long)

    private class Waiting(val articles: List<Article>, val origin: NewsOrigin?)

    /** A newer set of stories found while reading; shown when the reader taps the "new stories" pill. */
    private var waiting: Waiting? = null
    /** Set by the feed: true while the reader is still on the first card of the current tab. */
    var readerIsAtTop = true
    private var backgroundedAt: Instant? = null
    private var isChecking = false
    private var refreshInFlight = false
    /** Until then the feed is not sent back to its first story: a tapped alert's card stays on screen. */
    private var holdPositionUntil: Instant = Instant.MIN
    private var focusTokens = 0L

    init {
        rebuildFeeds()
    }

    val visibleArticles: List<Article> get() = feeds[category] ?: emptyList()

    fun articles(category: Category): List<Article> = feeds[category] ?: emptyList()

    /** The biggest story right now (for the daily digest and the alert preview), not just the newest. */
    val topStory: Article? get() = FeedOrdering.topStory(articles, disabledSources)

    /** Re-applies the ordering rules: rank, muted sources, unread first. */
    private fun rebuildFeeds() {
        val read = readState.ids
        feeds = Category.entries.associateWith { FeedOrdering.stories(it, articles, disabledSources, read) }
    }

    // Loading

    /** Called on launch and whenever the app comes to the front. */
    suspend fun becameActive() {
        // A background refresh may have written a newer cache while the app was not running.
        val cache = disk.loadCache()
        if (cache != null && cache.updatedAt > (lastUpdated ?: Instant.MIN) && articles.isNotEmpty()) {
            waiting = Waiting(cache.articles.map { it.slim() }, cache.origin ?: origin)
            lastUpdated = cache.updatedAt
        }
        val awayLong = backgroundedAt?.let { Duration.between(it, now()) >= STALE_AFTER } ?: false
        backgroundedAt = null

        if (articles.isEmpty()) {
            refresh()
        } else if (Duration.between(lastUpdated ?: Instant.MIN, now()) > STALE_AFTER || waiting != null) {
            checkForNewStories()
            // Nobody is mid-read right after a launch or a long break, so the newer stories can simply appear.
            if (waiting != null && (awayLong || readerIsAtTop)) showWaitingStories()
        } else if (awayLong) {
            rebuildFeeds()
            sendToTop()
        }
    }

    private fun sendToTop() {
        if (now() >= holdPositionUntil) scrollToTopRequests++
    }

    fun enteredBackground() {
        backgroundedAt = now()
        persist()
        readState.persist()
    }

    /**
     * Pull to refresh, the "Try again" button and first launch: fetch and show the result straight away.
     * The fetch runs in the store's own scope: a refresh, once asked for, always runs to its end; the caller only
     * waits. `atLeastMs` holds the result back until that long after the pull began, so the feed is not re-ordered
     * under a finger that is still holding the pull.
     */
    suspend fun refresh(atLeastMs: Long = 0) {
        scope.async { performRefresh(atLeastMs) }.await()
    }

    private suspend fun performRefresh(atLeastMs: Long) {
        if (refreshInFlight) return
        refreshInFlight = true
        try {
            // The skeleton card needs to know a first load is running. With stories on screen nothing is published
            // until the fetch ends.
            if (articles.isEmpty()) isRefreshing = true
            // With nothing to show yet (first launch, and the edition unavailable), stories appear as each feed
            // answers instead of after the slowest one.
            val onProgress: Progress? = if (articles.isEmpty()) ({ showPartial(it) }) else null
            val began = System.nanoTime()
            val result = service(onProgress)
            val spentMs = (System.nanoTime() - began) / 1_000_000
            if (spentMs < atLeastMs) delay(atLeastMs - spentMs)
            failedSources = result.failed
            if (result.articles.isEmpty()) {
                isOffline = true
                return
            }
            isOffline = false
            val hadStories = lastUpdated != null
            apply(result.articles.map { it.slim() }, result.origin)
            // Stories already read sink in the new order. Someone who asked for fresh stories wants the top of it.
            if (hadStories) sendToTop()
            refreshesCompleted++
        } finally {
            refreshInFlight = false
            if (isRefreshing) isRefreshing = false
        }
    }

    /** Looks for newer stories without disturbing the reader; `showWaitingStories()` brings them in. */
    suspend fun checkForNewStories() {
        if (refreshInFlight || isChecking || articles.isEmpty()) return
        isChecking = true
        try {
            val result = scope.async { service(null) }.await()
            if (result.articles.isEmpty()) {
                isOffline = true
                return
            }
            isOffline = false
            val fresh = result.articles.map { it.slim() }
            if (fresh == articles) {
                origin = result.origin
                lastUpdated = now()
                return
            }
            waiting = Waiting(fresh, result.origin)
            updateWaitingCount()
        } finally {
            isChecking = false
        }
    }

    fun showWaitingStories() {
        val w = waiting ?: return
        apply(w.articles, w.origin)
        sendToTop()
    }

    private fun apply(fresh: List<Article>, newOrigin: NewsOrigin?) {
        articles = fresh
        origin = newOrigin
        lastUpdated = now()
        waiting = null
        waitingCount = 0
        rebuildFeeds()
        persist()
        onApplied(this)
    }

    private fun showPartial(partial: List<Article>) {
        // Only while the first load is still running: later results must not shuffle cards someone is reading.
        if (!isRefreshing || lastUpdated != null || !readerIsAtTop) return
        articles = partial.map { it.slim() }
        origin = NewsOrigin.Direct
        rebuildFeeds()
    }

    private fun updateWaitingCount() {
        val w = waiting
        if (w == null) { waitingCount = 0; return }
        val fresh = FeedOrdering.stories(category, w.articles, disabledSources, readState.ids)
        waitingCount = FeedOrdering.newCount(fresh, feeds[category] ?: emptyList())
    }

    fun persist() {
        lastUpdated?.let { disk.saveCache(ArticleCache.of(articles, it, origin)) }
    }

    // Finding a story

    fun article(url: String): Article? =
        articles.firstOrNull { it.id == url } ?: waiting?.articles?.firstOrNull { it.id == url }
            ?: saved.firstOrNull { it.id == url }

    /** Brings a story on screen and highlights it. Returns false when the story isn't in any tab. */
    fun focusOn(url: String): Boolean {
        if (articles.none { it.id == url } && waiting?.articles?.any { it.id == url } == true) showWaitingStories()
        val tabs = listOf(category) + Category.entries
        val tab = tabs.firstOrNull { t -> feeds[t]?.any { it.id == url } == true } ?: return false
        category = tab
        focus = Focus(url, tab, ++focusTokens)
        return true
    }

    /** Brings the story an alert is about on screen: by its link, or by the event it reports. */
    fun focus(url: String?, eventId: String?): Boolean {
        val held = holdPositionUntil
        holdPositionUntil = now().plusSeconds(5)
        if (url != null && focusOn(url)) return true
        val match = eventId?.let { id ->
            articles.firstOrNull { it.eventId == id } ?: waiting?.articles?.firstOrNull { it.eventId == id }
        }
        if (match != null && focusOn(match.url)) return true
        holdPositionUntil = held
        return false
    }

    /**
     * A tapped alert: brings its story's card on screen, fetching the newest edition first when the story isn't
     * here yet. Returns false when the story is in no edition any more.
     */
    suspend fun showStory(url: String?, eventId: String?): Boolean {
        if (focus(url, eventId)) return true
        // Nothing sends the feed to its first story while the card is being looked for.
        holdPositionUntil = Instant.MAX
        try {
            loadsSettle()
            if (focus(url, eventId)) return true
            if (articles.isEmpty()) refresh() else checkForNewStories()
            return focus(url, eventId)
        } finally {
            if (holdPositionUntil == Instant.MAX) holdPositionUntil = Instant.MIN
        }
    }

    /** Waits (15 s at most) for a refresh or a check that is already running. */
    private suspend fun loadsSettle() {
        repeat(300) {
            if (!refreshInFlight && !isChecking) return
            delay(50)
        }
    }

    // Saved

    fun isSaved(article: Article): Boolean = saved.any { it.id == article.id }

    fun toggleSaved(article: Article) {
        saved = if (isSaved(article)) saved.filter { it.id != article.id } else listOf(article.slim()) + saved
        disk.saveSaved(saved)
    }

    fun clearSaved() {
        saved = emptyList()
        disk.saveSaved(saved)
    }

    /** A story was saved from a notification's Save button while the app was running. */
    fun reloadSaved() {
        saved = disk.loadSaved()
    }

    companion object {
        val STALE_AFTER: Duration = Duration.ofMinutes(10)
    }
}
