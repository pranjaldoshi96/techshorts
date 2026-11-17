package com.pranjald.techgist

import android.app.Application
import android.content.Context
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.ProcessLifecycleOwner
import coil.ImageLoader
import coil.ImageLoaderFactory
import coil.disk.DiskCache
import coil.memory.MemoryCache
import com.pranjald.techgist.data.Disk
import com.pranjald.techgist.data.FeedService
import com.pranjald.techgist.data.Http
import com.pranjald.techgist.data.KeyValue
import com.pranjald.techgist.data.Keys
import com.pranjald.techgist.data.NewsStore
import com.pranjald.techgist.data.OkHttpGet
import com.pranjald.techgist.data.PrefsKeyValue
import com.pranjald.techgist.data.ReadState
import com.pranjald.techgist.notify.AlertSound
import com.pranjald.techgist.notify.BackgroundRefresh
import com.pranjald.techgist.notify.BreakingNews
import com.pranjald.techgist.notify.DailyDigest
import com.pranjald.techgist.notify.StoryNotification
import com.pranjald.techgist.push.DeviceInfo
import com.pranjald.techgist.push.FirebaseTokens
import com.pranjald.techgist.push.PushRegistration
import com.pranjald.techgist.push.PushRequest
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.MainScope
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.File
import java.time.ZoneId
import java.util.Locale
import java.util.concurrent.TimeUnit

/** What to do with a tapped alert: show its card in the feed, or open the full story. */
sealed interface AlertRequest {
    data class Show(val url: String?, val eventId: String?) : AlertRequest
    data class Read(val url: String) : AlertRequest
}

/** The app's long-lived objects, made once per process. */
class Graph(private val context: Context) {
    val scope: CoroutineScope = MainScope()
    val settings: KeyValue = PrefsKeyValue(context.getSharedPreferences("techgist", Context.MODE_PRIVATE))
    val disk = Disk(context.cacheDir, context.filesDir)
    val http: Http = OkHttpGet(context.cacheDir)
    val feed = FeedService(http, BuildConfig.EDITION_URL)
    val readState = ReadState(disk)
    val breaking = BreakingNews(settings)
    val push = PushRegistration(
        // The device's registration is kept apart from the settings, so it is never restored onto another phone.
        PrefsKeyValue(context.getSharedPreferences("techgist_device", Context.MODE_PRIVATE)),
        BuildConfig.PUSH_URL.takeIf { it.isNotEmpty() },
        FirebaseTokens(context),
        ::sendPush,
    ) {
        val locale = Locale.getDefault()
        DeviceInfo(ZoneId.systemDefault().id, PushRegistration.localeTag(locale.language, locale.country),
            BuildConfig.VERSION_NAME, AlertSound.current(settings).key)
    }
    val store = NewsStore(scope, readState, disk, settings, { feed.latest(it) }, onApplied = { store ->
        // These stories are on screen now, so they shouldn't alert later.
        breaking.markSeen(store.articles)
    })

    /** A tapped alert waiting for the feed to be on screen. */
    var pendingRequest: AlertRequest? by mutableStateOf(null)

    private val pushClient = OkHttpClient.Builder().callTimeout(15, TimeUnit.SECONDS).build()

    private suspend fun sendPush(request: PushRequest): Int? = withContext(Dispatchers.IO) {
        runCatching {
            val body = request.body?.toRequestBody("application/json".toMediaType())
            pushClient.newCall(Request.Builder().url(request.url).method(request.method, body).build())
                .execute().use { it.code }
        }.getOrNull()
    }

    /** Brings the push registry in line with the switch and the permission. */
    fun syncPush() {
        scope.launch { push.sync(settings.bool(Keys.BREAKING_NEWS), StoryNotification.allowed(context)) }
    }
}

class TechgistApp : Application(), ImageLoaderFactory {
    lateinit var graph: Graph
        private set

    override fun onCreate() {
        super.onCreate()
        graph = Graph(this)
        ProcessLifecycleOwner.get().lifecycle.addObserver(object : DefaultLifecycleObserver {
            override fun onStart(owner: LifecycleOwner) {
                graph.scope.launch { graph.store.becameActive() }
            }

            override fun onStop(owner: LifecycleOwner) {
                graph.store.enteredBackground()
            }
        })
        BackgroundRefresh.schedule(this)
        DailyDigest.reschedule(this)
        graph.syncPush()
    }

    /** Story pictures: decoded off the main thread at the size shown, 80 MB in memory at most, 7 days' worth on disk. */
    override fun newImageLoader(): ImageLoader = ImageLoader.Builder(this)
        .memoryCache { MemoryCache.Builder(this).maxSizeBytes(80_000_000).build() }
        .diskCache { DiskCache.Builder().directory(File(cacheDir, "images")).maxSizeBytes(150L * 1024 * 1024).build() }
        .respectCacheHeaders(false)
        .crossfade(250)
        .build()

    companion object {
        fun graph(context: Context): Graph = (context.applicationContext as TechgistApp).graph
    }
}
