package com.pranjald.techgist.push

import com.pranjald.techgist.data.KeyValue
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.net.URI
import java.net.URLEncoder

/**
 * Where this device's push token comes from. The app is written against this, not against Firebase: without a
 * Firebase project (`NoPush`) everything else works and alerts are made on the phone.
 */
interface PushTokenSource {
    /** False when push is not set up in this build; nothing is then asked for or sent. */
    val isAvailable: Boolean
    suspend fun token(): String?
    /** Gives the token up, so nothing can be delivered to it any more. */
    suspend fun delete()
}

object NoPush : PushTokenSource {
    override val isAvailable = false
    override suspend fun token(): String? = null
    override suspend fun delete() {}
}

data class PushRequest(val method: String, val url: String, val body: String? = null)

/** What the registry learns about a device besides its token. Nothing else leaves the phone. */
data class DeviceInfo(val tz: String, val locale: String, val appVersion: String, val sound: String)

/**
 * Server push for big stories. The device registers once the reader allows notifications and has "Big story
 * alerts" on: at that moment, on every launch after it, and whenever a new token is issued. Turning the switch
 * off (or taking the permission away) removes the device from the registry.
 *
 * The wire format is the iOS app's, plus `"platform": "android"`:
 *   POST   <base>/v1/devices            {"token", "platform", "env", "tz", "locale", "appVersion", "sound"}
 *   DELETE <base>/v1/devices/<token>
 */
class PushRegistration(
    private val settings: KeyValue,
    /** The registry's base URL (HTTPS, no trailing path); null turns server push off. */
    private val baseUrl: String?,
    private val tokens: PushTokenSource,
    /** Sends a request and returns the HTTP status, or null when there was no answer. */
    private val send: suspend (PushRequest) -> Int?,
    private val device: () -> DeviceInfo,
) {
    /** The token the registry currently holds for this device; null when it holds none. */
    val registeredToken: String? get() = settings.string(REGISTERED)

    /** True when this build can register for server push at all. */
    val isAvailable: Boolean get() = baseUrl != null && tokens.isAvailable

    /** True while the server is the one sending big-story alerts to this device, so the app must not send its own. */
    val isRegistered: Boolean get() = isAvailable && registeredToken != null

    /** Brings the registry in line with the reader's choices. Never shows a prompt itself. */
    suspend fun sync(alertsOn: Boolean, notificationsAllowed: Boolean) {
        if (!isAvailable) return
        if (wantsPush(alertsOn, notificationsAllowed)) {
            tokens.token()?.let { register(it) }
        } else {
            unregister()
        }
    }

    /** Sends the token. When a different token than the one the registry holds was issued, the old one is removed. */
    suspend fun register(token: String) {
        val base = baseUrl ?: return
        val request = registerRequest(base, token, device()) ?: return
        val old = registeredToken
        if (old != null && old != token) unregisterRequest(base, old)?.let { send(it) }
        val status = send(request) ?: return
        if (status in 200..299) settings.put(REGISTERED, token)
    }

    /**
     * Removes this device from the registry and gives the token up. If the server can't be reached the token is
     * kept, so the next launch tries again.
     */
    suspend fun unregister() {
        val base = baseUrl ?: return
        val token = registeredToken ?: return
        val request = unregisterRequest(base, token) ?: return
        val status = send(request) ?: return
        if (status in 200..299 || status == 404 || status == 410) {
            settings.put(REGISTERED, null)
            tokens.delete()
        }
    }

    companion object {
        private const val REGISTERED = "pushRegisteredToken"
        private val TOKEN = Regex("^[A-Za-z0-9_:\\-]{20,1024}$")

        fun wantsPush(alertsOn: Boolean, notificationsAllowed: Boolean): Boolean = alertsOn && notificationsAllowed

        /** Language and region only, e.g. `en_IN`. */
        fun localeTag(language: String, country: String): String =
            (if (country.isEmpty()) language else "${language}_$country").filter { it.isLetterOrDigit() || it == '_' }.take(35)

        fun registration(token: String, device: DeviceInfo): String = buildJsonObject {
            put("appVersion", device.appVersion)
            // FCM has one environment; the field is the registry's, kept so both platforms send the same shape.
            put("env", "production")
            put("locale", device.locale)
            put("platform", "android")
            put("sound", device.sound)
            put("token", token)
            put("tz", device.tz)
        }.toString()

        fun registerRequest(base: String, token: String, device: DeviceInfo): PushRequest? {
            if (!isUsable(base) || !isToken(token)) return null
            return PushRequest("POST", base.trimEnd('/') + "/v1/devices", registration(token, device))
        }

        fun unregisterRequest(base: String, token: String): PushRequest? {
            if (!isUsable(base) || !isToken(token)) return null
            return PushRequest("DELETE", base.trimEnd('/') + "/v1/devices/" + URLEncoder.encode(token, "UTF-8"))
        }

        /** Tokens only ever travel over HTTPS. */
        private fun isUsable(base: String): Boolean =
            runCatching { URI(base) }.getOrNull()?.let { it.scheme == "https" && !it.host.isNullOrEmpty() } ?: false

        fun isToken(token: String): Boolean = TOKEN.matches(token)
    }
}
