package com.pranjald.techgist.push

import android.content.Context
import com.google.firebase.FirebaseApp
import com.google.firebase.messaging.FirebaseMessaging
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import com.pranjald.techgist.BuildConfig
import com.pranjald.techgist.TechgistApp
import com.pranjald.techgist.data.Keys
import com.pranjald.techgist.notify.StoryNotification
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlin.coroutines.resume

/**
 * Firebase Cloud Messaging as the token source. It works only in a build that has the owner's
 * `app/google-services.json`; without it Firebase never starts and `isAvailable` is false.
 */
class FirebaseTokens(private val context: Context) : PushTokenSource {
    override val isAvailable: Boolean
        get() = BuildConfig.HAS_FIREBASE && runCatching { FirebaseApp.getApps(context).isNotEmpty() }.getOrDefault(false)

    override suspend fun token(): String? = suspendCancellableCoroutine { cont ->
        runCatching {
            FirebaseMessaging.getInstance().token.addOnCompleteListener { task ->
                if (cont.isActive) cont.resume(if (task.isSuccessful) task.result else null)
            }
        }.onFailure { if (cont.isActive) cont.resume(null) }
    }

    override suspend fun delete() = suspendCancellableCoroutine { cont ->
        runCatching {
            FirebaseMessaging.getInstance().deleteToken().addOnCompleteListener { if (cont.isActive) cont.resume(Unit) }
        }.onFailure { if (cont.isActive) cont.resume(Unit) }
    }
}

/**
 * Receives the server's pushes. They are data messages (`kind`, `title`, `body`, `eventId`, `url`, `image`), so
 * the app draws the notification itself: the reader's sound, the picture, Read and Save, and a tap that opens the
 * story's card. Title and body are shown exactly as sent.
 */
class TechgistMessagingService : FirebaseMessagingService() {
    override fun onNewToken(token: String) {
        val graph = TechgistApp.graph(applicationContext)
        if (PushRegistration.wantsPush(graph.settings.bool(Keys.BREAKING_NEWS), StoryNotification.allowed(applicationContext))) {
            runBlocking { graph.push.register(token) }
        }
    }

    override fun onMessageReceived(message: RemoteMessage) {
        val data = message.data
        val title = data["title"]?.takeIf { it.isNotEmpty() } ?: return
        val body = data["body"] ?: ""
        val url = data["url"]?.takeIf { it.startsWith("http") }
        val eventId = data["eventId"]?.takeIf { it.isNotEmpty() }
        if (data["kind"] != "story" && url == null && eventId == null) {
            StoryNotification.postPlain(applicationContext, title, body, data["kind"] ?: "server")
            return
        }
        val graph = TechgistApp.graph(applicationContext)
        val article = StoryNotification.story(url, eventId, graph.disk.loadCache()?.articles ?: emptyList())
        runBlocking {
            StoryNotification.post(applicationContext, title, body, url ?: article?.url, eventId, data["image"],
                article, StoryNotification.Kind.BREAKING)
        }
    }
}
