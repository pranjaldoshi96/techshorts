package com.pranjald.techgist.data

import android.content.SharedPreferences
import com.pranjald.techgist.model.Article
import com.pranjald.techgist.model.ArticleCache
import com.pranjald.techgist.model.decodeArticles
import com.pranjald.techgist.model.encodeArticles
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put
import java.io.File
import java.time.Duration
import java.time.Instant

/** The settings' names, the same as the iOS app's. */
object Keys {
    const val DISABLED_SOURCES = "disabledSources"
    const val DIGEST_ENABLED = "digestEnabled"
    const val DIGEST_MINUTE = "digestMinuteOfDay"
    const val BREAKING_NEWS = "breakingNewsEnabled"
    const val NOTIFICATIONS_OFFERED = "notificationsOffered"
    const val ALERT_SOUND = "alertSound"
}

/** Small key-value settings. SharedPreferences in the app, a map in tests. */
interface KeyValue {
    fun string(key: String): String?
    fun bool(key: String, default: Boolean = false): Boolean
    fun int(key: String, default: Int): Int
    fun strings(key: String): Set<String>
    fun put(key: String, value: Any?)
}

class PrefsKeyValue(private val prefs: SharedPreferences) : KeyValue {
    override fun string(key: String): String? = prefs.getString(key, null)
    override fun bool(key: String, default: Boolean): Boolean = prefs.getBoolean(key, default)
    override fun int(key: String, default: Int): Int = prefs.getInt(key, default)
    override fun strings(key: String): Set<String> = prefs.getStringSet(key, null)?.toSet() ?: emptySet()

    @Suppress("UNCHECKED_CAST")
    override fun put(key: String, value: Any?) {
        val e = prefs.edit()
        when (value) {
            null -> e.remove(key)
            is String -> e.putString(key, value)
            is Boolean -> e.putBoolean(key, value)
            is Int -> e.putInt(key, value)
            is Set<*> -> e.putStringSet(key, value as Set<String>)
            else -> error("unsupported setting type")
        }
        e.apply()
    }
}

class MemoryKeyValue : KeyValue {
    val map = HashMap<String, Any>()
    override fun string(key: String): String? = map[key] as? String
    override fun bool(key: String, default: Boolean): Boolean = map[key] as? Boolean ?: default
    override fun int(key: String, default: Int): Int = map[key] as? Int ?: default
    @Suppress("UNCHECKED_CAST")
    override fun strings(key: String): Set<String> = map[key] as? Set<String> ?: emptySet()
    override fun put(key: String, value: Any?) { if (value == null) map.remove(key) else map[key] = value }
}

/**
 * Small JSON-on-disk persistence. `files` is backed up with the app's data (saved stories); `cache` is not
 * (the last edition, what has been read).
 */
class Disk(private val cache: File, private val files: File) {
    private fun file(name: String, backedUp: Boolean) = File(if (backedUp) files else cache, name)

    fun read(name: String, backedUp: Boolean = false): String? =
        runCatching { file(name, backedUp).takeIf { it.exists() }?.readText() }.getOrNull()

    @Synchronized
    fun write(name: String, text: String, backedUp: Boolean = false) {
        runCatching {
            val target = file(name, backedUp)
            target.parentFile?.mkdirs()
            val tmp = File(target.path + ".tmp")
            tmp.writeText(text)
            if (!tmp.renameTo(target)) { target.delete(); tmp.renameTo(target) }
        }
    }

    fun loadCache(): ArticleCache? = read(ArticleCache.FILE)?.let(ArticleCache::decode)
    fun saveCache(cache: ArticleCache) = write(ArticleCache.FILE, cache.encode())

    fun loadSaved(): List<Article> = read(SAVED_FILE, backedUp = true)?.let(::decodeArticles) ?: emptyList()
    fun saveSaved(saved: List<Article>) = write(SAVED_FILE, encodeArticles(saved), backedUp = true)

    /** Saves a story without a running feed, for the Save button on a notification. False when it was there already. */
    @Synchronized
    fun addSaved(article: Article): Boolean {
        val saved = loadSaved()
        if (saved.any { it.id == article.id }) return false
        saveSaved(listOf(article.slim()) + saved)
        return true
    }

    companion object {
        const val SAVED_FILE = "saved.json"
        const val READ_FILE = "read.json"

        /** A disk that keeps nothing, for tests. */
        fun none(): Disk = Disk(File("/nonexistent-techgist"), File("/nonexistent-techgist"))
    }
}

/** Which stories have been read: on screen for 1.5 seconds, or opened. Kept for 7 days, on this device only. */
class ReadState(private val disk: Disk?, initial: Map<String, Instant>? = null, now: Instant = Instant.now()) {
    private val readAt = HashMap<String, Instant>()
    private var unsaved = 0
    /** Called when a story becomes read, so the tab dots can redraw. */
    var onChange: (() -> Unit)? = null

    init {
        val stored = initial ?: disk?.read(Disk.READ_FILE)?.let(::decode) ?: emptyMap()
        stored.filterTo(readAt) { Duration.between(it.value, now) < KEEP_FOR }
    }

    val ids: Set<String> get() = readAt.keys.toSet()

    fun isRead(id: String): Boolean = id in readAt

    fun markRead(id: String, now: Instant = Instant.now()) {
        if (id in readAt) return
        readAt[id] = now
        unsaved++
        persist()
        onChange?.invoke()
    }

    fun persist() {
        if (unsaved == 0) return
        unsaved = 0
        disk?.write(Disk.READ_FILE, buildJsonObject { readAt.forEach { (k, v) -> put(k, v.toEpochMilli()) } }.toString())
    }

    companion object {
        val KEEP_FOR: Duration = Duration.ofDays(7)

        private fun decode(text: String): Map<String, Instant> =
            (runCatching { Json.parseToJsonElement(text) as? JsonObject }.getOrNull() ?: JsonObject(emptyMap()))
                .mapNotNull { (k, v) -> (v as? JsonPrimitive)?.longOrNull?.let { k to Instant.ofEpochMilli(it) } }.toMap()
    }
}
