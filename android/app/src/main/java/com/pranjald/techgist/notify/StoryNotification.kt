package com.pranjald.techgist.notify

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.ContentResolver
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.media.AudioAttributes
import android.media.MediaPlayer
import android.net.Uri
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import com.pranjald.techgist.MainActivity
import com.pranjald.techgist.R
import com.pranjald.techgist.TechgistApp
import com.pranjald.techgist.data.KeyValue
import com.pranjald.techgist.data.Keys
import com.pranjald.techgist.model.Article
import kotlinx.coroutines.withTimeoutOrNull

/**
 * The sound an alert makes. The reader picks one in Settings; the push server is told the choice with the
 * registration, and the app's own notifications use it too. The files are the iOS app's, converted to WAV.
 */
enum class AlertSound(val key: String, val title: String, val raw: Int?) {
    FLASH("flash", "News flash", R.raw.alert_flash),
    WIRE("wire", "News wire", R.raw.alert_wire),
    CHIME("chime", "Chime", R.raw.alert_chime),
    SOFT("soft", "Soft", R.raw.alert_soft),
    SYSTEM("default", "Phone default", null);

    /** Android fixes a channel's sound when the channel is made, so each sound has a channel of its own. */
    val channelId: String get() = "stories_$key"

    fun uri(context: Context): Uri? =
        raw?.let { Uri.parse("${ContentResolver.SCHEME_ANDROID_RESOURCE}://${context.packageName}/$it") }

    /** Plays the sound once, so the reader hears what they picked. The phone's default sound has no file to play. */
    fun preview(context: Context) {
        val res = raw ?: return
        runCatching {
            player?.release()
            player = MediaPlayer.create(context, res,
                AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_NOTIFICATION).build(), 0)
            player?.setOnCompletionListener { it.release(); if (player === it) player = null }
            player?.start()
        }
    }

    companion object {
        private var player: MediaPlayer? = null

        fun of(key: String?): AlertSound = entries.firstOrNull { it.key == key } ?: FLASH
        fun current(settings: KeyValue): AlertSound = of(settings.string(Keys.ALERT_SOUND))
    }
}

/**
 * How a story looks as a notification: the alert title, one line of new information, the story's picture, the
 * reader's sound, and Read and Save buttons. The app's own alerts and the server's pushes both come through here.
 */
object StoryNotification {
    const val ACTION_SHOW = "com.pranjald.techgist.SHOW"
    const val ACTION_READ = "com.pranjald.techgist.READ"
    const val ACTION_SAVE = "com.pranjald.techgist.SAVE"
    const val EXTRA_URL = "url"
    const val EXTRA_EVENT = "eventId"
    const val EXTRA_ARTICLE = "article"
    const val EXTRA_NOTIFICATION_ID = "notificationId"
    private const val OTHER_CHANNEL = "service"

    enum class Kind { BREAKING, BRIEF }

    fun allowed(context: Context): Boolean = NotificationManagerCompat.from(context).areNotificationsEnabled()

    /** Makes the channel for the reader's sound and removes the ones for the other sounds. Returns its id. */
    fun ensureChannel(context: Context, sound: AlertSound): String {
        val manager = context.getSystemService(NotificationManager::class.java)
        for (other in AlertSound.entries) if (other != sound) manager.deleteNotificationChannel(other.channelId)
        if (manager.getNotificationChannel(sound.channelId) == null) {
            val channel = NotificationChannel(sound.channelId, "Story alerts", NotificationManager.IMPORTANCE_HIGH)
            channel.description = "The morning gist and big story alerts"
            sound.uri(context)?.let {
                channel.setSound(it, AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_NOTIFICATION)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build())
            }
            manager.createNotificationChannel(channel)
        }
        return sound.channelId
    }

    /** `url` finds the story in the feed; the article lets Save work even when the story has left the edition. */
    private fun extras(intent: Intent, url: String?, eventId: String?, article: Article?, id: Int): Intent {
        url?.let { intent.putExtra(EXTRA_URL, it) }
        eventId?.let { intent.putExtra(EXTRA_EVENT, it) }
        article?.slim()?.toJson()?.toString()?.takeIf { it.length < 6000 }?.let { intent.putExtra(EXTRA_ARTICLE, it) }
        intent.putExtra(EXTRA_NOTIFICATION_ID, id)
        // Two notifications' intents must not be taken for one another.
        intent.data = Uri.parse("techgist://story/$id")
        return intent
    }

    private const val FLAGS = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE

    /** A stable number for a story, so a second alert about it replaces the first. */
    fun idFor(key: String): Int = key.hashCode() and 0x7fffffff

    /**
     * Shows the notification for a story. `article` is the card when the app has it. The picture is downloaded
     * (8 s at most) and left out when it doesn't arrive.
     */
    suspend fun post(context: Context, title: String, body: String, url: String?, eventId: String?, image: String?,
                     article: Article?, kind: Kind, subtitle: String? = null) {
        if (!allowed(context)) return
        val graph = TechgistApp.graph(context)
        val channel = ensureChannel(context, AlertSound.current(graph.settings))
        val id = idFor(eventId ?: url ?: title)
        val show = PendingIntent.getActivity(context, id,
            extras(Intent(context, MainActivity::class.java).setAction(ACTION_SHOW), url, eventId, article, id), FLAGS)
        val builder = NotificationCompat.Builder(context, channel)
            .setSmallIcon(R.drawable.ic_stat_bolt)
            .setColor(context.getColor(R.color.accent))
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setCategory(NotificationCompat.CATEGORY_RECOMMENDATION)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setGroup(kind.name)
            .setAutoCancel(true)
            .setContentIntent(show)
        subtitle?.let { builder.setSubText(it) }
        if (url != null) {
            val read = PendingIntent.getActivity(context, id + 1,
                extras(Intent(context, MainActivity::class.java).setAction(ACTION_READ), url, eventId, article, id), FLAGS)
            builder.addAction(0, "Read", read)
            val save = PendingIntent.getBroadcast(context, id + 2,
                extras(Intent(context, SaveStoryReceiver::class.java).setAction(ACTION_SAVE), url, eventId, article, id), FLAGS)
            builder.addAction(0, "Save", save)
        }
        picture(context, image)?.let {
            builder.setLargeIcon(it)
            builder.setStyle(NotificationCompat.BigPictureStyle().bigPicture(it).bigLargeIcon(null as Bitmap?)
                .setSummaryText(body))
        }
        runCatching { NotificationManagerCompat.from(context).notify(id, builder.build()) }
    }

    suspend fun post(context: Context, article: Article, kind: Kind, subtitle: String? = null) =
        post(context, article.alertTitle, article.alertBody, article.url, article.eventId,
            article.images?.std ?: article.image, article, kind, subtitle)

    /** A message that is not a story: the morning gist without one, a test or a server notice. */
    fun postPlain(context: Context, title: String, body: String, tag: String) {
        if (!allowed(context)) return
        val graph = TechgistApp.graph(context)
        val channel = if (tag == "digest") ensureChannel(context, AlertSound.current(graph.settings)) else {
            val manager = context.getSystemService(NotificationManager::class.java)
            if (manager.getNotificationChannel(OTHER_CHANNEL) == null) {
                manager.createNotificationChannel(
                    NotificationChannel(OTHER_CHANNEL, "Service messages", NotificationManager.IMPORTANCE_DEFAULT))
            }
            OTHER_CHANNEL
        }
        val open = PendingIntent.getActivity(context, 0, Intent(context, MainActivity::class.java), FLAGS)
        val n = NotificationCompat.Builder(context, channel)
            .setSmallIcon(R.drawable.ic_stat_bolt).setColor(context.getColor(R.color.accent))
            .setContentTitle(title).setContentText(body).setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setAutoCancel(true).setContentIntent(open).build()
        runCatching { NotificationManagerCompat.from(context).notify(idFor(tag), n) }
    }

    private suspend fun picture(context: Context, link: String?): Bitmap? {
        if (link == null || !link.startsWith("https://")) return null
        val bytes = withTimeoutOrNull(8000) { TechgistApp.graph(context).http.get(link, 8) } ?: return null
        return runCatching {
            val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
            BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
            var sample = 1
            while (bounds.outWidth / (sample * 2) >= 720) sample *= 2
            BitmapFactory.decodeByteArray(bytes, 0, bytes.size, BitmapFactory.Options().apply { inSampleSize = sample })
        }.getOrNull()
    }

    /** The story an alert is about: matched by its link first, then by the event it reports. */
    fun story(url: String?, eventId: String?, articles: List<Article>): Article? =
        url?.let { u -> articles.firstOrNull { it.url == u } }
            ?: eventId?.let { e -> articles.firstOrNull { it.eventId == e } }

    fun cancel(context: Context, id: Int) {
        if (id != 0) NotificationManagerCompat.from(context).cancel(id)
    }
}

/** The Save button on a notification: keeps the story without opening the app. */
class SaveStoryReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val graph = TechgistApp.graph(context)
        val url = intent.getStringExtra(StoryNotification.EXTRA_URL)
        val eventId = intent.getStringExtra(StoryNotification.EXTRA_EVENT)?.takeIf { it.isNotEmpty() }
        val article = intent.getStringExtra(StoryNotification.EXTRA_ARTICLE)?.let(Article::fromJson)
            ?: StoryNotification.story(url, eventId, graph.disk.loadCache()?.articles ?: emptyList())
        if (article != null) {
            graph.disk.addSaved(article)
            graph.store.reloadSaved()
        }
        StoryNotification.cancel(context, intent.getIntExtra(StoryNotification.EXTRA_NOTIFICATION_ID, 0))
    }
}
