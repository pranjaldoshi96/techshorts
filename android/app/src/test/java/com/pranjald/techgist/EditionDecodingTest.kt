package com.pranjald.techgist

import com.pranjald.techgist.data.FeedOrdering
import com.pranjald.techgist.model.Article
import com.pranjald.techgist.model.ArticleCache
import com.pranjald.techgist.model.Category
import com.pranjald.techgist.model.Edition
import com.pranjald.techgist.model.NewsOrigin
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.Duration
import java.time.Instant

/** The edition as the server publishes it must read the same on Android as on iOS. */
class EditionDecodingTest {
    private fun fixture(): String =
        javaClass.classLoader!!.getResourceAsStream("edition-v2-engine.json")!!.readBytes().decodeToString()

    private fun article(extra: String = ""): String = """
        {"url":"https://example.com/a","title":"Publisher title","excerpt":"Excerpt.","content":"Body",
         "source":"Example","category":"AI","publishedAt":"2026-11-16T01:32:03Z"$extra}"""

    private fun one(extra: String = ""): Article? = Article.fromJson(article(extra))

    @Test fun engineEditionDecodesEveryArticleWithItsNewFields() {
        val text = fixture()
        val edition = Edition.decode(text)!!
        assertEquals(Regex("\"topRank\"|\"category\"").findAll(text).count { it.value == "\"category\"" }, edition.articles.size)
        assertTrue(edition.articles.size > 50)
        assertTrue(edition.articles.all { it.url.startsWith("http") && it.title.isNotEmpty() })
        assertTrue(edition.articles.any { it.spice != null })
        assertTrue(edition.articles.any { it.eventId != null })
        assertTrue(edition.articles.any { it.summary != null })
        assertTrue(edition.articles.all { (it.spice ?: 1) in 1..10 })
    }

    @Test fun engineEditionFillsEveryTabAndTopFollowsTopRank() {
        val articles = Edition.decode(fixture())!!.articles
        for (tab in Category.entries) assertTrue(tab.raw, FeedOrdering.stories(tab, articles).isNotEmpty())
        val top = FeedOrdering.stories(Category.TOP, articles)
        assertEquals(top.map { it.topRank!! }.sorted(), top.map { it.topRank!! })
    }

    @Test fun engineEditionSurvivesTheCacheRoundTrip() {
        val edition = Edition.decode(fixture())!!
        val cache = ArticleCache.of(edition.articles, Instant.parse("2026-11-16T04:00:00Z"),
            NewsOrigin.FromEdition(edition.publishedAt, false))
        val back = ArticleCache.decode(cache.encode())!!
        assertEquals(edition.articles, back.articles)
        assertEquals(NewsOrigin.FromEdition(edition.publishedAt, false), back.origin)
    }

    @Test fun headlinesAndAlertTextAreExactlyTheEditions() {
        val a = one(""","headline":"Techgist’s “spicy” headline","summary":"S.","notification":{"title":"Alert title","body":"Alert body."}""")!!
        assertEquals("Techgist’s “spicy” headline", a.displayTitle)
        assertEquals("Alert title", a.alertTitle)
        assertEquals("Alert body.", a.alertBody)
        assertEquals("Publisher title", one()!!.displayTitle)
    }

    @Test fun newOptionalFieldsDecode() {
        val a = one(""","summaryShort":"Short.","alertLine":"Line.","spice":9,"rank":3,"badge":"breaking","type":"analysis",
            "updatedAt":"2026-11-16T05:00:00Z","aiText":true,"eventId":"ev_1",
            "images":{"hero":"https://x/h.jpg","std":"https://x/s.jpg","thumb":"https://x/t.jpg","generated":false},
            "coverageLinks":{"Other":"https://other.com/story","Bad":"ftp://nope","Num":5}""")!!
        assertEquals("Short.", a.summaryShort); assertEquals("Line.", a.alertLine); assertEquals(9, a.spice)
        assertEquals(3, a.rank); assertTrue(a.isBreaking); assertTrue(a.isHot); assertEquals("ANALYSIS", a.kindLabel)
        assertEquals(Instant.parse("2026-11-16T05:00:00Z"), a.updatedAt); assertEquals("ev_1", a.eventId)
        assertEquals("https://x/h.jpg", a.cardImage(wide = true)); assertEquals("https://x/s.jpg", a.cardImage(wide = false))
        assertEquals("https://x/t.jpg", a.thumbImage)
        assertEquals(mapOf("Other" to "https://other.com/story"), a.coverageLinks)
    }

    @Test fun notifLineIsAcceptedAsTheAlertLine() {
        assertEquals("Old key.", one(""","notifLine":"Old key."""")!!.alertLine)
    }

    @Test fun mistypedNewFieldsAreIgnoredNotFatal() {
        val a = one(""","spice":"hot","rank":"1","images":"none","badge":7,"aiText":"yes","eventId":3,"notification":"x","coverageLinks":[1]""")!!
        assertNull(a.spice); assertNull(a.rank); assertNull(a.images); assertNull(a.badge); assertNull(a.aiText)
        assertNull(a.eventId); assertNull(a.notification); assertNull(a.coverageLinks)
    }

    @Test fun spiceScales() {
        assertEquals(8, one(""","spice":0.8""")!!.spice)
        assertEquals(1, one(""","spice":1""")!!.spice)
        assertEquals(10, one(""","spice":14""")!!.spice)
        assertEquals(7, one(""","spice":7.4""")!!.spice)
        assertNull(one(""","spice":0""")!!.spice)
    }

    @Test fun anArticleThatDoesNotDecodeIsDroppedAndTheRestKept() {
        val bad = article().replace("\"AI\"", "\"Gadgets\"")
        val edition = Edition.decode("""{"publishedAt":"2026-11-16T03:17:40Z","articles":[${article()},$bad,{"url":"https://x"}]}""")!!
        assertEquals(1, edition.articles.size)
    }

    @Test fun aMissingRequiredFieldFailsThatArticle() {
        assertNull(Article.fromJson(article().replace("\"title\":\"Publisher title\",", "")))
        assertNull(Article.fromJson(article().replace("2026-11-16T01:32:03Z", "yesterday")))
        assertNull(one(""","points":"many""""))
    }

    @Test fun editionIsFreshForEightHours() {
        val edition = Edition.decode("""{"publishedAt":"2026-11-16T00:00:00Z","articles":[${article()}]}""")!!
        assertTrue(edition.isFresh(edition.publishedAt.plus(Duration.ofHours(8)).minusSeconds(1)))
        assertFalse(edition.isFresh(edition.publishedAt.plus(Duration.ofHours(8))))
        assertFalse(Edition(edition.publishedAt, emptyList()).isFresh(edition.publishedAt))
        assertNull(Edition.decode("not json"))
        assertNull(Edition.decode("""{"articles":[]}"""))
    }

    @Test fun shortSummaryKeepsWholeSentences() {
        val text = "U.S. regulators fined Acme Inc. \$2.5 billion on Monday. The company said it will appeal. A third sentence."
        assertEquals("U.S. regulators fined Acme Inc. \$2.5 billion on Monday. The company said it will appeal.",
            Article.firstSentences(2, text))
        assertEquals("One sentence only", Article.firstSentences(2, "One sentence only"))
        assertEquals("He said “stop.”", Article.firstSentences(1, "He said “stop.” Then left."))
    }

    @Test fun alertBodyUsesTheAlertLineThenTheFirstSentence() {
        assertEquals("Line.", one(""","alertLine":"Line.","summary":"First. Second."""")!!.alertBody)
        assertEquals("First.", one(""","summary":"First. Second."""")!!.alertBody)
        val bare = Article.fromJson(article(""","alsoCoveredBy":["A","B"]""").replace("Excerpt.", ""))!!
        assertEquals("Example + 2 more", bare.alertBody)
        assertNotNull(bare.toJson())
    }
}
