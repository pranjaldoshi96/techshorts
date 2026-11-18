package com.pranjald.techgist

import com.pranjald.techgist.data.ContentFilter
import com.pranjald.techgist.data.FeedDate
import com.pranjald.techgist.data.FeedOrdering
import com.pranjald.techgist.data.FeedPaging
import com.pranjald.techgist.data.FeedParser
import com.pranjald.techgist.data.FeedService
import com.pranjald.techgist.data.FeedSource
import com.pranjald.techgist.data.HtmlText
import com.pranjald.techgist.data.MemoryKeyValue
import com.pranjald.techgist.data.Keys
import com.pranjald.techgist.data.RelativeTime
import com.pranjald.techgist.data.StorySearch
import com.pranjald.techgist.data.articleOf
import com.pranjald.techgist.model.Article
import com.pranjald.techgist.model.Category
import com.pranjald.techgist.model.NewsOrigin
import com.pranjald.techgist.notify.BreakingNews
import com.pranjald.techgist.notify.DailyDigest
import com.pranjald.techgist.ui.END_ID
import com.pranjald.techgist.ui.banner
import com.pranjald.techgist.ui.monogramColor
import com.pranjald.techgist.ui.progress
import com.pranjald.techgist.ui.spokenLabel
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.Duration
import java.time.Instant
import java.time.ZoneId
import java.time.ZonedDateTime
import java.util.Locale

val NOW: Instant = Instant.parse("2026-11-16T12:00:00Z")

fun story(name: String, category: Category = Category.TOP, topRank: Int? = null, rank: Int? = null, ageMin: Long = 60,
          source: String = "Example", others: List<String>? = null, badge: String? = null, spice: Int? = null,
          eventId: String? = null) = Article(
    url = "https://example.com/$name", title = name, excerpt = "Excerpt of $name.", source = source, category = category,
    publishedAt = NOW.minus(Duration.ofMinutes(ageMin)), topRank = topRank, rank = rank, alsoCoveredBy = others,
    badge = badge, spice = spice, eventId = eventId)

class FeedLogicTest {
    private fun names(list: List<Article>) = list.map { it.title }

    // Ordering

    @Test fun topFollowsTheServersRankNotTheClock() {
        val list = listOf(story("old", topRank = 0, ageMin = 600), story("new", topRank = 1, ageMin = 1), story("pinned", topRank = -1))
        assertEquals(listOf("pinned", "old", "new"), names(FeedOrdering.stories(Category.TOP, list)))
    }

    @Test fun topFallsBackToNewestFirstWithoutRanks() {
        val list = listOf(story("a", ageMin = 30), story("b", ageMin = 5), story("ai", Category.AI))
        assertEquals(listOf("b", "a"), names(FeedOrdering.stories(Category.TOP, list)))
    }

    @Test fun otherTabsFollowRankThenNewestFirst() {
        val list = listOf(story("r1", Category.AI, rank = 1), story("none-old", Category.AI, ageMin = 90),
            story("r0", Category.AI, rank = 0), story("none-new", Category.AI, ageMin = 2),
            story("tie-new", Category.AI, rank = 1, ageMin = 1))
        assertEquals(listOf("r0", "tie-new", "r1", "none-new", "none-old"), names(FeedOrdering.stories(Category.AI, list)))
    }

    @Test fun mutedSourcesAreRemovedAndReadStoriesSinkKeepingTheirOrder() {
        val list = listOf(story("a", topRank = 0), story("b", topRank = 1, source = "Muted"), story("c", topRank = 2), story("d", topRank = 3))
        assertEquals(listOf("a", "c", "d"), names(FeedOrdering.stories(Category.TOP, list, muted = setOf("Muted"))))
        assertEquals(listOf("b", "d", "a", "c"),
            names(FeedOrdering.stories(Category.TOP, list, read = setOf(list[0].id, list[2].id))))
        assertEquals("a", FeedOrdering.topStory(list)!!.title)
    }

    @Test fun newCountLooksOnlyAtTheTopOfTheTab() {
        val current = (0 until 20).map { story("s$it") }
        val fresh = listOf(story("n1"), story("n2")) + current.take(8) + story("n3")
        assertEquals(2, FeedOrdering.newCount(fresh, current))
    }

    // Paging

    @Test fun swipes() {
        assertEquals(1, FeedPaging.step(-40f, -45f))          // a short slow drag goes to the next card
        assertEquals(0, FeedPaging.step(-12f, -30f))          // a twitch stays put
        assertEquals(1, FeedPaging.step(-15f, -400f))         // a quick flick counts
        assertEquals(1, FeedPaging.step(-300f, -5000f))       // a hard flick still moves only one card
        assertEquals(-1, FeedPaging.step(50f, 60f))
        assertEquals(1, FeedPaging.tabStep(-80f, 10f))
        assertEquals(-1, FeedPaging.tabStep(80f, -10f))
        assertNull(FeedPaging.tabStep(-40f, 0f))
        assertNull(FeedPaging.tabStep(-80f, 70f))
    }

    @Test fun progressLine() {
        val items = (0 until 4).map { story("s$it") }
        assertEquals(0f, progress(emptyList(), null), 0f)
        assertEquals(0.25f, progress(items, null), 0f)
        assertEquals(0.75f, progress(items, items[2].id), 0f)
        assertEquals(1f, progress(items, END_ID), 0f)
    }

    // Time

    @Test fun relativeTime() {
        val zone = ZoneId.of("UTC")
        fun short(ago: Duration) = RelativeTime.short(NOW.minus(ago), NOW, zone, Locale.US)
        assertEquals("now", short(Duration.ofSeconds(20)))
        assertEquals("now", RelativeTime.short(NOW.plusSeconds(500), NOW, zone, Locale.US))
        assertEquals("12m", short(Duration.ofMinutes(12)))
        assertEquals("3h", short(Duration.ofHours(3)))
        assertEquals("Yesterday", short(Duration.ofHours(30)))
        assertEquals("Nov 4", short(Duration.ofDays(12)))
        assertEquals("1 minute ago", RelativeTime.spoken(NOW.minusSeconds(61), NOW, zone))
        assertEquals("2 hours ago", RelativeTime.spoken(NOW.minus(Duration.ofHours(2)), NOW, zone))
        assertEquals("yesterday", RelativeTime.spoken(NOW.minus(Duration.ofHours(30)), NOW, zone))
    }

    @Test fun updatedLabel() {
        val a = story("a", ageMin = 300)
        assertEquals("5h", RelativeTime.label(a, NOW))
        assertEquals("5h", RelativeTime.label(a.copy(updatedAt = a.publishedAt.plusSeconds(1800)), NOW))
        assertEquals("Updated 2h", RelativeTime.label(a.copy(updatedAt = NOW.minus(Duration.ofHours(2))), NOW))
    }

    // Banner, search, labels

    @Test fun bannerSaysWhenStoriesAreOldOfflineOrWithoutAiText() {
        val fresh = NewsOrigin.FromEdition(NOW.minusSeconds(600), false)
        assertNull(banner(true, false, NOW, fresh, NOW))
        assertNull(banner(false, true, NOW, fresh, NOW))
        assertEquals("Offline · stories from 10 minutes ago", banner(true, true, NOW.minusSeconds(600), fresh, NOW)!!.first)
        assertEquals("Summaries catching up · showing headlines", banner(true, false, NOW, NewsOrigin.Direct, NOW)!!.first)
        assertEquals("Summaries catching up · showing headlines",
            banner(true, false, NOW, NewsOrigin.FromEdition(NOW, true), NOW)!!.first)
        assertEquals("Updated 3 hours ago",
            banner(true, false, NOW, NewsOrigin.FromEdition(NOW.minus(Duration.ofHours(3)), false), NOW)!!.first)
    }

    @Test fun searchMatchesEveryWordAcrossHeadlineSummaryAndOutlets() {
        val a = story("Nvidia chips", others = listOf("Bloomberg"))
        val b = story("Apple phone")
        assertTrue(StorySearch.matches(a, "nvidia BLOOM"))
        assertFalse(StorySearch.matches(a, "nvidia apple"))
        assertEquals(listOf(b), StorySearch.results(" apple ", listOf(a, b), listOf(b)))
        assertEquals(emptyList<Article>(), StorySearch.results("  ", listOf(a, b), emptyList()))
    }

    @Test fun theCardIsReadOutInOrder() {
        val a = story("Big news", others = listOf("A", "B"), badge = "breaking").copy(summary = "What happened.")
        val label = spokenLabel(a)
        assertTrue(label.startsWith("Breaking Big news. What happened. Example and 2 other outlets, "))
        assertTrue(label.endsWith("Summarized by AI."))
    }

    @Test fun monogramColoursAreStableAndVaried() {
        assertEquals(monogramColor("TechCrunch"), monogramColor("TechCrunch"))
        assertTrue(listOf("TechCrunch", "The Verge", "Wired", "Bloomberg", "Mint", "ET Tech").map(::monogramColor).toSet().size >= 3)
    }

    // The app's own alerts and the morning gist

    @Test fun alertCandidatesPreferBreakingThenSpiceThenRank() {
        val list = listOf(
            story("covered", topRank = 0, others = listOf("A", "B"), spice = 5),
            story("spicier", topRank = 3, others = listOf("A", "B"), spice = 9),
            story("breaking", topRank = 7, badge = "breaking"),
            story("small", topRank = 1, others = listOf("A")),
            story("old", topRank = 2, others = listOf("A", "B", "C"), ageMin = 7 * 60),
            story("not-top", others = listOf("A", "B", "C")),
        )
        assertEquals(listOf("breaking", "spicier", "covered"), names(BreakingNews.candidates(list, emptySet(), NOW)))
        assertEquals(listOf("spicier", "covered"), names(BreakingNews.candidates(list, setOf(list[2].id), NOW)))
    }

    @Test fun localAlertsStopAtThreeADayNeverRepeatAndYieldToTheServer() {
        val settings = MemoryKeyValue().apply { put(Keys.BREAKING_NEWS, true) }
        val alerts = BreakingNews(settings)
        val list = (0 until 5).map { story("s$it", topRank = it, badge = "breaking") }
        assertNull(alerts.pick(list, serverPushes = true, NOW))
        repeat(3) {
            val pick = alerts.pick(list, serverPushes = false, NOW)!!
            assertEquals("s$it", pick.title)
            alerts.sent(pick, NOW)
        }
        assertNull(alerts.pick(list, serverPushes = false, NOW))
        alerts.markSeen(list, NOW)
        assertTrue(BreakingNews.candidates(list, settings.string("breakingNewsSeen")!!.split('\n').toSet(), NOW).isEmpty())
        settings.put(Keys.BREAKING_NEWS, false)
        assertNull(BreakingNews(settings).pick(list, serverPushes = false, NOW))
    }

    @Test fun theMorningGistComesAtTheNextEightOClock() {
        val zone = ZoneId.of("Asia/Kolkata")
        val morning = ZonedDateTime.of(2026, 11, 16, 6, 0, 0, 0, zone)
        assertEquals(ZonedDateTime.of(2026, 11, 16, 8, 0, 0, 0, zone), DailyDigest.nextTrigger(480, morning))
        assertEquals(ZonedDateTime.of(2026, 11, 17, 8, 0, 0, 0, zone), DailyDigest.nextTrigger(480, morning.plusHours(2)))
    }

    // The direct-feed fallback

    private val rss = """<?xml version="1.0"?><rss xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:media="http://search.yahoo.com/mrss/"><channel>
        <item><title>Startup raises &amp;#36;5M &amp;amp; hires</title><link> https://example.com/raise </link>
          <pubDate>Mon, 16 Nov 2026 10:00:00 +0000</pubDate>
          <description><![CDATA[<p>The company plans to grow. <img src="https://img.example.com/a.jpg"></p> The post X appeared first on Y.]]></description>
          <media:content url="https://img.example.com/m.jpg" medium="image"/></item>
        <item><title>Best laptop deals of the week</title><link>https://example.com/deals/laptops</link><pubDate>Mon, 16 Nov 2026 09:00:00 GMT</pubDate></item>
        <item><title>No date</title><link>https://example.com/x</link></item></channel></rss>"""

    @Test fun rssItemsBecomeStories() {
        val items = FeedParser.parse(rss.toByteArray())
        assertEquals(3, items.size)
        val source = FeedSource("Example", Category.STARTUPS, "https://example.com/feed")
        val a = articleOf(items[0], source)!!
        assertEquals("Startup raises \$5M & hires", a.title)
        assertEquals("https://example.com/raise", a.url)
        assertEquals("The company plans to grow.", a.excerpt)
        assertEquals("https://img.example.com/m.jpg", a.image)
        assertEquals(Instant.parse("2026-11-16T10:00:00Z"), a.publishedAt)
        assertTrue(ContentFilter.isLowValue(articleOf(items[1], source)!!))
        assertFalse(ContentFilter.isLowValue(a))
        assertNull(articleOf(items[2], source))
    }

    @Test fun atomEntriesAndDates() {
        val atom = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Post</title>
            <link rel="alternate" href="https://blog.example.com/post"/><link rel="self" href="https://blog.example.com/self"/>
            <published>2026-11-15T08:00:00+05:30</published><summary>Hello &lt;b&gt;world&lt;/b&gt;</summary></entry></feed>"""
        val item = FeedParser.parse(atom.toByteArray()).single()
        assertEquals("https://blog.example.com/post", item.link)
        assertEquals(Instant.parse("2026-11-15T02:30:00Z"), FeedDate.parse(item.date))
        assertEquals("Hello world", HtmlText.plain(item.html))
        assertEquals(Instant.parse("2026-11-16T09:00:00Z"), FeedDate.parse("Mon, 16 Nov 2026 09:00:00 GMT"))
        assertNull(FeedDate.parse("soon"))
        assertEquals("One two three.", HtmlText.truncate("One two three. Four five six seven", 5))
    }

    @Test fun theSameStoryFromSeveralOutletsBecomesOneCardThatNamesThem() {
        fun s(title: String, source: String, category: Category, path: String) = Article(url = "https://$path", title = title,
            excerpt = "x", source = source, category = category, publishedAt = NOW.minusSeconds(3600))
        val merged = FeedService.merge(listOf(
            listOf(s("OpenAI launches GPT-6 model for enterprise customers", "TechCrunch", Category.TOP, "techcrunch.com/gpt6")),
            listOf(s("OpenAI launches GPT-6 for enterprise customers worldwide", "The Verge", Category.TOP, "theverge.com/gpt6")),
            listOf(s("Rust 2.0 compiler ships with faster builds", "GitHub Blog", Category.DEV, "github.blog/rust")),
            listOf(s("A quiet essay on database indexes", "Julia Evans", Category.BLOGS, "jvns.ca/idx")),
        ), NOW)
        val top = FeedOrdering.stories(Category.TOP, merged)
        val gpt = top.single { "GPT-6" in it.title }
        assertEquals(1, gpt.alsoCoveredBy!!.size)
        assertEquals(0, gpt.topRank)
        assertTrue(top.any { it.category == Category.DEV })
        assertNotEquals(FeedService.dedupeKey("https://a.com/x/"), FeedService.dedupeKey("https://a.com/y"))
        assertEquals(FeedService.dedupeKey("https://A.com/x/"), FeedService.dedupeKey("https://a.com/x"))
    }
}
