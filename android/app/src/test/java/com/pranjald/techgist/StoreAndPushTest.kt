package com.pranjald.techgist

import com.pranjald.techgist.data.Disk
import com.pranjald.techgist.data.FeedResult
import com.pranjald.techgist.data.Keys
import com.pranjald.techgist.data.MemoryKeyValue
import com.pranjald.techgist.data.NewsStore
import com.pranjald.techgist.data.Progress
import com.pranjald.techgist.data.ReadState
import com.pranjald.techgist.model.Article
import com.pranjald.techgist.model.ArticleCache
import com.pranjald.techgist.model.Category
import com.pranjald.techgist.model.NewsOrigin
import com.pranjald.techgist.push.DeviceInfo
import com.pranjald.techgist.push.PushRegistration
import com.pranjald.techgist.push.PushRequest
import com.pranjald.techgist.push.PushTokenSource
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

@OptIn(ExperimentalCoroutinesApi::class)
class NewsStoreTest {
    private val edition = NewsOrigin.FromEdition(NOW, false)
    private val first = (0 until 12).map { story("s$it", topRank = it, eventId = "ev_$it") }

    private class Feed(var answer: List<Article>, var delayMs: Long = 100) {
        var calls = 0
        val service: suspend (Progress?) -> FeedResult = {
            calls++
            delay(delayMs)
            FeedResult(answer, emptyList(), NewsOrigin.FromEdition(NOW, false))
        }
    }

    private fun TestScope.store(feed: Feed, cached: List<Article>? = null, disk: Disk = Disk.none(),
                                read: ReadState = ReadState(null, emptyMap())) =
        NewsStore(backgroundScope, read, disk, MemoryKeyValue(), feed.service, now = { NOW },
            cache = cached?.let { ArticleCache.of(it, NOW, edition) })

    @Test fun aFirstLoadShowsItIsLoadingThenTheStories() = runTest {
        val feed = Feed(first)
        val store = store(feed)
        val job = launch { store.refresh() }
        runCurrent()
        assertTrue(store.isRefreshing)
        job.join()
        assertFalse(store.isRefreshing)
        assertEquals(12, store.visibleArticles.size)
        assertEquals(1, store.refreshesCompleted)
        assertEquals(0, store.scrollToTopRequests)
    }

    @Test fun aRefreshWhoseCallerIsCancelledStillLoads() = runTest {
        val feed = Feed(first)
        val store = store(feed)
        val job = launch { store.refresh() }
        runCurrent()
        job.cancelAndJoin()
        advanceTimeBy(1000)
        assertEquals(12, store.articles.size)
        assertFalse(store.isOffline)
    }

    @Test fun aRefreshWithStoriesOnScreenGoesBackToTheTopAndPublishesNothingMeanwhile() = runTest {
        val feed = Feed(first.reversed())
        val store = store(feed, cached = first)
        val job = launch { store.refresh() }
        runCurrent()
        assertFalse(store.isRefreshing)
        assertEquals(first, store.articles)
        job.join()
        assertEquals(1, store.scrollToTopRequests)
    }

    @Test fun aPullHoldsAFastResultUntilItsMinimum() = runTest {
        val feed = Feed(first.take(3), delayMs = 0)
        val store = store(feed, cached = first)
        val job = launch { store.refresh(atLeastMs = 600) }
        advanceTimeBy(500)
        assertEquals(12, store.articles.size)
        job.join()
        assertEquals(3, store.articles.size)
    }

    @Test fun twoRefreshesAtOnceFetchOnce() = runTest {
        val feed = Feed(first)
        val store = store(feed)
        val a = launch { store.refresh() }
        val b = launch { store.refresh() }
        a.join(); b.join()
        assertEquals(1, feed.calls)
    }

    @Test fun aRefreshThatGetsNothingIsOfflineAndTheNextGoodOneClearsIt() = runTest {
        val feed = Feed(emptyList())
        val store = store(feed, cached = first)
        store.refresh()
        assertTrue(store.isOffline)
        assertEquals(first, store.articles)
        feed.answer = first
        store.refresh()
        assertFalse(store.isOffline)
    }

    @Test fun newerStoriesWaitBehindThePillUntilAsked() = runTest {
        val newer = listOf(story("new1", topRank = -2), story("new2", topRank = -1)) + first
        val feed = Feed(newer)
        val store = store(feed, cached = first)
        store.checkForNewStories()
        assertEquals(first, store.articles)
        assertEquals(2, store.waitingCount)
        store.category = Category.AI
        assertEquals(0, store.waitingCount)
        store.category = Category.TOP
        store.showWaitingStories()
        assertEquals("new1", store.visibleArticles.first().title)
        assertEquals(0, store.waitingCount)
        assertEquals(1, store.scrollToTopRequests)
    }

    @Test fun anUnchangedEditionChangesNothingOnScreen() = runTest {
        val store = store(Feed(first), cached = first)
        store.checkForNewStories()
        assertEquals(0, store.waitingCount)
        store.showWaitingStories()
        assertEquals(0, store.scrollToTopRequests)
    }

    @Test fun mutingASourceRemovesItFromEveryTabAndIsRemembered() = runTest {
        val settings = MemoryKeyValue()
        val list = first + story("muted", topRank = 20, source = "Loud")
        val store = NewsStore(backgroundScope, ReadState(null, emptyMap()), Disk.none(), settings, Feed(list).service,
            cache = ArticleCache.of(list, NOW, edition))
        assertEquals(13, store.visibleArticles.size)
        store.disabledSources = setOf("Loud")
        assertEquals(12, store.visibleArticles.size)
        assertEquals(setOf("Loud"), settings.strings(Keys.DISABLED_SOURCES))
    }

    @Test fun readStoriesSinkOnlyWhenANewEditionIsApplied() = runTest {
        val read = ReadState(null, emptyMap())
        val feed = Feed(first + story("extra", topRank = 50))
        val store = store(feed, cached = first, read = read)
        read.markRead(first[0].id)
        assertEquals("s0", store.visibleArticles.first().title)   // not re-ordered under a finger
        store.refresh()
        assertEquals("s1", store.visibleArticles.first().title)
        assertEquals("s0", store.visibleArticles.last().title)
    }

    @Test fun theStoreFocusesOnAPushedEventByIdOrLink() = runTest {
        val list = first + story("ai", Category.AI, eventId = "ev_ai")
        val store = store(Feed(list), cached = list)
        assertTrue(store.focus(null, "ev_ai"))
        assertEquals(Category.AI, store.category)
        assertEquals(list.last().id, store.focus!!.id)
        assertTrue(store.focus(first[3].url, null))
        assertEquals(Category.TOP, store.category)
        assertFalse(store.focus("https://example.com/gone", "ev_gone"))
    }

    @Test fun aTappedAlertWaitsForTheNewEditionAndItsCardStaysOnScreen() = runTest {
        val pushed = story("pushed", topRank = -5, eventId = "ev_new")
        val feed = Feed(listOf(pushed) + first)
        val store = store(feed, cached = first)
        assertTrue(store.showStory(null, "ev_new"))
        assertEquals(pushed.id, store.focus!!.id)
        // The refresh that lands as the app comes to the front must not send the feed back to its first story.
        assertEquals(0, store.scrollToTopRequests)
        assertFalse(store.showStory("https://example.com/gone", null))
    }

    @Test fun savedStoriesAndTheCacheSurviveARestart() = runTest {
        val dir = Files.createTempDirectory("techgist").toFile()
        val disk = Disk(File(dir, "cache"), File(dir, "files"))
        val store = store(Feed(first), disk = disk)
        store.refresh()
        store.toggleSaved(first[2])
        assertTrue(store.isSaved(first[2]))
        assertTrue(disk.addSaved(first[4]))
        assertFalse(disk.addSaved(first[4]))
        store.reloadSaved()
        assertEquals(listOf("s4", "s2"), store.saved.map { it.title })
        val again = NewsStore(backgroundScope, ReadState(disk), disk, MemoryKeyValue(), Feed(first).service)
        assertEquals(12, again.articles.size)
        assertEquals(2, again.saved.size)
        assertNotNull(again.lastUpdated)
        again.toggleSaved(first[2]); again.clearSaved()
        assertTrue(disk.loadSaved().isEmpty())
        dir.deleteRecursively()
    }

    @Test fun whatWasReadIsKeptForSevenDays() {
        val read = ReadState(null, mapOf("old" to NOW.minusSeconds(8 * 86_400), "recent" to NOW.minusSeconds(86_400)), NOW)
        assertFalse(read.isRead("old"))
        assertTrue(read.isRead("recent"))
    }
}

class PushRegistrationTest {
    private val base = "https://push.example.com"
    private val token = "fMEP0vJqS0:APA91b" + "Aa1_-".repeat(30)
    private val device = DeviceInfo("Asia/Kolkata", "en_IN", "2.0", "wire")

    private class Tokens(var value: String?, override val isAvailable: Boolean = true) : PushTokenSource {
        var deleted = 0
        override suspend fun token(): String? = value
        override suspend fun delete() { deleted++ }
    }

    private class Server(var status: Int? = 200) {
        val requests = ArrayList<PushRequest>()
        val send: suspend (PushRequest) -> Int? = { requests.add(it); status }
    }

    private fun registration(server: Server, tokens: Tokens, url: String? = base, settings: MemoryKeyValue = MemoryKeyValue()) =
        PushRegistration(settings, url, tokens, server.send) { device }

    @Test fun registerPostsTheContractBody() = runTest {
        val server = Server()
        val push = registration(server, Tokens(token))
        push.sync(alertsOn = true, notificationsAllowed = true)
        val request = server.requests.single()
        assertEquals("POST", request.method)
        assertEquals("$base/v1/devices", request.url)
        val body = Json.parseToJsonElement(request.body!!).jsonObject
        assertEquals(setOf("token", "platform", "env", "tz", "locale", "appVersion", "sound"), body.keys)
        assertEquals(token, body["token"]!!.jsonPrimitive.content)
        assertEquals("android", body["platform"]!!.jsonPrimitive.content)
        assertEquals("Asia/Kolkata", body["tz"]!!.jsonPrimitive.content)
        assertEquals("wire", body["sound"]!!.jsonPrimitive.content)
        assertTrue(push.isRegistered)
    }

    @Test fun pushIsOffWithoutARegistryOrWithoutFirebase() = runTest {
        val server = Server()
        registration(server, Tokens(token), url = null).sync(true, true)
        val unavailable = registration(server, Tokens(token, isAvailable = false))
        unavailable.sync(true, true)
        assertTrue(server.requests.isEmpty())
        assertFalse(unavailable.isRegistered)
        assertFalse(unavailable.isAvailable)
    }

    @Test fun pushNeedsBothTheSwitchAndThePermission() = runTest {
        assertTrue(PushRegistration.wantsPush(true, true))
        assertFalse(PushRegistration.wantsPush(true, false))
        assertFalse(PushRegistration.wantsPush(false, true))
        val server = Server()
        registration(server, Tokens(token)).sync(alertsOn = true, notificationsAllowed = false)
        assertTrue(server.requests.isEmpty())
    }

    @Test fun everyLaunchRegistersAgainAndAFailedRegistrationIsNotRemembered() = runTest {
        val server = Server()
        val push = registration(server, Tokens(token))
        push.sync(true, true); push.sync(true, true)
        assertEquals(2, server.requests.size)
        val failing = Server(status = 500)
        val other = registration(failing, Tokens(token))
        other.sync(true, true)
        assertNull(other.registeredToken)
        failing.status = null
        other.sync(true, true)
        assertFalse(other.isRegistered)
    }

    @Test fun aNewTokenReplacesTheOldOne() = runTest {
        val server = Server()
        val tokens = Tokens(token)
        val push = registration(server, tokens)
        push.sync(true, true)
        tokens.value = token.reversed()
        push.sync(true, true)
        assertEquals(listOf("POST", "DELETE", "POST"), server.requests.map { it.method })
        assertTrue(server.requests[1].url.startsWith("$base/v1/devices/fMEP0vJqS0%3AAPA91b"))
        assertEquals(token.reversed(), push.registeredToken)
    }

    @Test fun optingOutDeletesTheDeviceAndGivesTheTokenUp() = runTest {
        val server = Server()
        val tokens = Tokens(token)
        val push = registration(server, tokens)
        push.sync(true, true)
        push.sync(alertsOn = false, notificationsAllowed = true)
        assertEquals("DELETE", server.requests.last().method)
        assertNull(push.registeredToken)
        assertEquals(1, tokens.deleted)
        push.sync(false, true)
        assertEquals(2, server.requests.size)      // nothing to remove, nothing sent
    }

    @Test fun anOptOutThatCannotReachTheServerIsTriedAgain() = runTest {
        val server = Server()
        val tokens = Tokens(token)
        val push = registration(server, tokens)
        push.sync(true, true)
        server.status = null
        push.sync(false, true)
        assertEquals(token, push.registeredToken)
        server.status = 404
        push.sync(false, true)
        assertNull(push.registeredToken)
    }

    @Test fun tokensOnlyTravelOverHttpsAndMustLookLikeTokens() {
        assertNull(PushRegistration.registerRequest("http://push.example.com", token, device))
        assertNull(PushRegistration.registerRequest(base, "has spaces and / slashes", device))
        assertNull(PushRegistration.unregisterRequest(base, ""))
        assertNotNull(PushRegistration.registerRequest(base, token, device))
        assertEquals("en_IN", PushRegistration.localeTag("en", "IN"))
        assertEquals("en", PushRegistration.localeTag("en", ""))
    }
}
