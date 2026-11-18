package com.pranjald.techgist.notify

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.ProcessLifecycleOwner
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.pranjald.techgist.TechgistApp
import com.pranjald.techgist.data.FeedOrdering
import com.pranjald.techgist.data.KeyValue
import com.pranjald.techgist.data.Keys
import com.pranjald.techgist.model.Article
import com.pranjald.techgist.model.ArticleCache
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import java.time.Duration
import java.time.Instant
import java.time.LocalTime
import java.time.ZoneId
import java.time.ZonedDateTime
import java.util.concurrent.TimeUnit

/**
 * Alerts for big stories, shown by the app itself while the server isn't pushing to this device. After a
 * background refresh, a new Top story becomes a notification if it is marked breaking by the edition, or is
 * covered by 3+ outlets, and is under 6 hours old. At most 1 per refresh and 3 per 24 hours, never the same story
 * twice. Stories already seen in the app don't alert.
 */
class BreakingNews(private val settings: KeyValue) {
    val isEnabled: Boolean get() = settings.bool(Keys.BREAKING_NEWS)

    private val seen: Set<String> get() = settings.string(SEEN)?.split('\n')?.toSet() ?: emptySet()

    /** While the app is open, current big stories are on screen anyway, so they won't alert later. */
    fun markSeen(articles: List<Article>, now: Instant = Instant.now()) =
        remember(candidates(articles, seen, now).map { it.id })

    /** The story to alert about after a background refresh, within the daily cap; null when there is none. */
    fun pick(articles: List<Article>, serverPushes: Boolean, now: Instant = Instant.now()): Article? {
        // Once the server pushes to this device, it decides what alerts; a second, local alert would be a duplicate.
        if (!isEnabled || serverPushes) return null
        val story = candidates(articles, seen, now).firstOrNull() ?: return null
        return if (recent(now).size < MAX_PER_DAY) story else null
    }

    fun sent(story: Article, now: Instant = Instant.now()) {
        remember(listOf(story.id))
        settings.put(SENT, (recent(now) + now.toEpochMilli()).joinToString(","))
    }

    private fun recent(now: Instant): List<Long> =
        (settings.string(SENT) ?: "").split(',').mapNotNull { it.toLongOrNull() }
            .filter { now.toEpochMilli() - it < 86_400_000 }

    private fun remember(ids: List<String>) {
        if (ids.isEmpty()) return
        val all = (settings.string(SEEN)?.split('\n') ?: emptyList()) + ids
        settings.put(SEEN, all.takeLast(500).joinToString("\n"))
    }

    companion object {
        private const val SEEN = "breakingNewsSeen"
        private const val SENT = "breakingNewsSentAt"
        const val MIN_OUTLETS = 3
        const val MAX_AGE_HOURS = 6L
        const val MAX_PER_DAY = 3

        /** Big stories not in `seen`, best first: breaking ones, then the spiciest, then by Top rank. */
        fun candidates(articles: List<Article>, seen: Set<String>, now: Instant = Instant.now()): List<Article> =
            articles
                .filter { it.topRank != null && (it.isBreaking || it.coverage >= MIN_OUTLETS) }
                .filter { Duration.between(it.publishedAt, now) < Duration.ofHours(MAX_AGE_HOURS) && it.id !in seen }
                .sortedWith(compareByDescending<Article> { it.isBreaking }.thenByDescending { it.spiceLevel }
                    .thenBy { it.topRank })
    }
}

/** The morning gist: one notification a day at the reader's time, carrying the biggest story and its picture. */
object DailyDigest {
    fun isEnabled(settings: KeyValue): Boolean = settings.bool(Keys.DIGEST_ENABLED)
    fun minuteOfDay(settings: KeyValue): Int = settings.int(Keys.DIGEST_MINUTE, 8 * 60)

    /** The next time the clock shows `minuteOfDay`. */
    fun nextTrigger(minuteOfDay: Int, now: ZonedDateTime): ZonedDateTime {
        val today = now.toLocalDate().atTime(LocalTime.of(minuteOfDay / 60 % 24, minuteOfDay % 60)).atZone(now.zone)
        return if (today.isAfter(now)) today else today.plusDays(1)
    }

    private fun intent(context: Context): PendingIntent = PendingIntent.getBroadcast(
        context, 0, Intent(context, DigestReceiver::class.java),
        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)

    /** Sets (or removes) the alarm for the next gist. Android may deliver it a few minutes late to save battery. */
    fun reschedule(context: Context) {
        val settings = TechgistApp.graph(context).settings
        val alarms = context.getSystemService(AlarmManager::class.java)
        alarms.cancel(intent(context))
        if (!isEnabled(settings)) return
        val at = nextTrigger(minuteOfDay(settings), ZonedDateTime.now(ZoneId.systemDefault()))
        alarms.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at.toInstant().toEpochMilli(), intent(context))
    }

    suspend fun deliver(context: Context) {
        val graph = TechgistApp.graph(context)
        val muted = graph.settings.strings(Keys.DISABLED_SOURCES)
        val top = FeedOrdering.topStory(graph.disk.loadCache()?.articles ?: emptyList(), muted)
        if (top != null && Duration.between(top.publishedAt, Instant.now()) < Duration.ofHours(18)) {
            StoryNotification.post(context, top, StoryNotification.Kind.BRIEF, subtitle = "Your morning gist")
        } else {
            StoryNotification.postPlain(context, "Your morning gist",
                "Today's tech, AI and startup news, 60 words a story.", "digest")
        }
    }
}

class DigestReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val app = context.applicationContext
        DailyDigest.reschedule(app)
        if (!DailyDigest.isEnabled(TechgistApp.graph(app).settings)) return
        val pending = goAsync()
        CoroutineScope(Dispatchers.Default).launch {
            try { DailyDigest.deliver(app) } finally { pending.finish() }
        }
    }
}

/** Alarms do not survive a restart or an app update. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        DailyDigest.reschedule(context.applicationContext)
        BackgroundRefresh.schedule(context.applicationContext)
    }
}

/** Refreshes the story cache about once an hour while the app is not open, and makes the app's own alerts. */
class BackgroundRefresh(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        // With the app open the feed looks after itself.
        if (ProcessLifecycleOwner.get().lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)) return Result.success()
        val graph = TechgistApp.graph(applicationContext)
        val result = graph.feed.latest()
        if (result.articles.isEmpty()) return Result.success()
        graph.disk.saveCache(ArticleCache.of(result.articles.map { it.slim() }, Instant.now(), result.origin))
        val muted = graph.settings.strings(Keys.DISABLED_SOURCES)
        val enabled = result.articles.filter { it.source !in muted }
        val story = graph.breaking.pick(enabled, serverPushes = graph.push.isRegistered)
        if (story != null && StoryNotification.allowed(applicationContext)) {
            StoryNotification.post(applicationContext, story, StoryNotification.Kind.BREAKING)
            graph.breaking.sent(story)
        }
        return Result.success()
    }

    companion object {
        fun schedule(context: Context) {
            val request = PeriodicWorkRequestBuilder<BackgroundRefresh>(1, TimeUnit.HOURS)
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .build()
            WorkManager.getInstance(context)
                .enqueueUniquePeriodicWork("refresh", ExistingPeriodicWorkPolicy.KEEP, request)
        }
    }
}
