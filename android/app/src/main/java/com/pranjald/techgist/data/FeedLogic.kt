package com.pranjald.techgist.data

import com.pranjald.techgist.model.Article
import com.pranjald.techgist.model.Category
import java.time.Duration
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.time.temporal.ChronoUnit
import java.util.Locale
import kotlin.math.abs

/** Decides which stories a tab shows and in what order. Pure functions, so the rules are unit-tested. */
object FeedOrdering {
    /**
     * The stories for one tab.
     * 1. Muted sources are removed.
     * 2. Top holds every story with a `topRank`, in that order. Other tabs hold their own category, ordered by the
     *    edition's `rank` when it sends one; without a rank a tab is newest first.
     * 3. Stories already read sink below unread ones, keeping their order.
     */
    fun stories(category: Category, articles: List<Article>, muted: Set<String> = emptySet(),
                read: Set<String> = emptySet()): List<Article> {
        val enabled = if (muted.isEmpty()) articles else articles.filter { it.source !in muted }
        val ordered = if (category == Category.TOP && enabled.any { it.topRank != null }) {
            sorted(enabled.filter { it.topRank != null }) { it.topRank }
        } else {
            sorted(enabled.filter { it.category == category }) { it.rank }
        }
        if (read.isEmpty()) return ordered
        return ordered.filter { it.id !in read } + ordered.filter { it.id in read }
    }

    /** Ranked stories first, in rank order; unranked ones after them, newest first. Ties go to the newer story. */
    private fun sorted(articles: List<Article>, rank: (Article) -> Int?): List<Article> =
        articles.sortedWith { a, b ->
            val x = rank(a)
            val y = rank(b)
            when {
                x != null && y != null && x != y -> x.compareTo(y)
                x != null && y == null -> -1
                x == null && y != null -> 1
                else -> b.publishedAt.compareTo(a.publishedAt)
            }
        }

    /** The single biggest story right now, for the morning digest. */
    fun topStory(articles: List<Article>, muted: Set<String> = emptySet()): Article? =
        stories(Category.TOP, articles, muted).firstOrNull()

    /** How many of the first `limit` stories in `fresh` are not in `current`. */
    fun newCount(fresh: List<Article>, current: List<Article>, limit: Int = 10): Int {
        val known = current.mapTo(HashSet()) { it.id }
        return fresh.take(limit).count { it.id !in known }
    }
}

/** What a swipe on the paged feed means. Distances are in dp. Pure, so the rules are unit-tested. */
object FeedPaging {
    /** How far the finger must move before the swipe counts as "next card"; shorter drags spring back. */
    const val COMMIT_DISTANCE = 36f
    /** How far the first card must be pulled down to refresh. */
    const val REFRESH_PULL = 90f
    /** A flick shorter than `COMMIT_DISTANCE` still counts when it would have coasted this far. */
    const val FLICK_DISTANCE = 120f

    /**
     * +1 for the next card, -1 for the previous, 0 to stay. `moved` is the finger's vertical travel (up is
     * negative), `projected` where it would have coasted to. Never more than one card per swipe.
     */
    fun step(moved: Float, projected: Float): Int {
        if (moved < -COMMIT_DISTANCE || (moved < 0 && projected < -FLICK_DISTANCE)) return 1
        if (moved > COMMIT_DISTANCE || (moved > 0 && projected > FLICK_DISTANCE)) return -1
        return 0
    }

    /** +1 for the tab to the right (a swipe to the left), -1 for the tab to the left, null when too short or diagonal. */
    fun tabStep(dx: Float, dy: Float): Int? {
        if (abs(dx) <= 50 || abs(dx) <= 1.5f * abs(dy)) return null
        return if (dx < 0) 1 else -1
    }
}

/** Short, honest ages for cards: `now`, `12m`, `3h`, `Yesterday`, `Nov 4`. A date in the future reads as `now`. */
object RelativeTime {
    fun short(date: Instant, now: Instant = Instant.now(), zone: ZoneId = ZoneId.systemDefault(),
              locale: Locale = Locale.getDefault()): String {
        val seconds = maxOf(0, Duration.between(date, now).seconds)
        if (seconds < 60) return "now"
        if (seconds < 3600) return "${seconds / 60}m"
        if (seconds < 24 * 3600) return "${seconds / 3600}h"
        val days = ChronoUnit.DAYS.between(date.atZone(zone).toLocalDate(), now.atZone(zone).toLocalDate())
        if (days <= 1) return "Yesterday"
        return DateTimeFormatter.ofPattern("MMM d", locale).format(date.atZone(zone))
    }

    /** The same, in words a screen reader reads well: "12 minutes ago". */
    fun spoken(date: Instant, now: Instant = Instant.now(), zone: ZoneId = ZoneId.systemDefault(),
               locale: Locale = Locale.getDefault()): String {
        val seconds = maxOf(0, Duration.between(date, now).seconds)
        if (seconds < 60) return "just now"
        if (seconds < 3600) {
            val m = seconds / 60
            return if (m == 1L) "1 minute ago" else "$m minutes ago"
        }
        if (seconds < 24 * 3600) {
            val h = seconds / 3600
            return if (h == 1L) "1 hour ago" else "$h hours ago"
        }
        val text = short(date, now, zone, locale)
        return if (text == "Yesterday") "yesterday" else text
    }

    private fun updated(article: Article): Instant? =
        article.updatedAt?.takeIf { Duration.between(article.publishedAt, it).seconds > 3600 }

    /** The time line for a card. A story updated more than an hour after it was first reported says so. */
    fun label(article: Article, now: Instant = Instant.now(), zone: ZoneId = ZoneId.systemDefault()): String =
        updated(article)?.let { "Updated ${short(it, now, zone)}" } ?: short(article.publishedAt, now, zone)

    fun spokenLabel(article: Article, now: Instant = Instant.now()): String =
        updated(article)?.let { "updated ${spoken(it, now)}" } ?: spoken(article.publishedAt, now)
}

/** Matches a story against typed words: every word must appear in its headline, summary or outlets. */
object StorySearch {
    fun matches(article: Article, query: String): Boolean {
        val haystack = (listOf(article.displayTitle, article.title, article.displaySummary, article.source) +
            (article.alsoCoveredBy ?: emptyList())).joinToString(" ")
        return query.split(" ").filter { it.isNotEmpty() }.all { haystack.contains(it, ignoreCase = true) }
    }

    /** Stories in the edition and in Saved that match, edition first, no duplicates. */
    fun results(query: String, articles: List<Article>, saved: List<Article>): List<Article> {
        val words = query.trim()
        if (words.isEmpty()) return emptyList()
        val seen = HashSet<String>()
        return (articles + saved).filter { matches(it, words) && seen.add(it.id) }
    }
}
