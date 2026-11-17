package com.pranjald.techgist.ui

import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.animate
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.outlined.Newspaper
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.clipToBounds
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.input.pointer.PointerEventPass
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.input.pointer.util.VelocityTracker
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import coil.imageLoader
import coil.request.ImageRequest
import com.pranjald.techgist.data.FeedPaging
import com.pranjald.techgist.data.NewsStore
import com.pranjald.techgist.data.RelativeTime
import com.pranjald.techgist.model.Article
import com.pranjald.techgist.model.Category
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlin.math.abs

const val END_ID = "end-of-tab"

/** The font scale from which a story is taller than a screen, and the feed becomes an ordinary scrolling list. */
const val LIST_FONT_SCALE = 1.6f

/**
 * One tab's stories, one card per screen, paged vertically. At the largest font sizes a story is taller than a
 * screen, so the feed becomes an ordinary scrolling list of full-length cards instead.
 */
@Composable
fun FeedView(category: Category, items: List<Article>, store: NewsStore, position: String?, onPosition: (String) -> Unit,
             highlighted: String?, bottomInset: Dp, onRead: (Article) -> Unit, onCoverage: (Article) -> Unit,
             onViewed: (Article) -> Unit, onSaved: () -> Unit, onSwipeTab: (Int) -> Unit) {
    val haptics = LocalHapticFeedback.current
    val context = LocalContext.current
    LaunchedEffect(Unit) {
        // A tap of haptics when a refresh the reader asked for brings stories back.
        snapshotFlow { store.refreshesCompleted }.collect { if (it > 0) haptics.performHapticFeedback(HapticFeedbackType.LongPress) }
    }
    if (items.isEmpty()) {
        EmptyFeed(store)
        return
    }
    val currentId = position ?: items.first().id
    // Runs each time a card comes to rest: warm the next pictures, then count the card as read after 1.5 s.
    LaunchedEffect(currentId, items) {
        delay(300)
        val index = items.indexOfFirst { it.id == currentId }
        if (index < 0) return@LaunchedEffect
        store.readerIsAtTop = index == 0
        val wide = context.resources.configuration.screenWidthDp >= 390
        items.drop(index + 1).take(3).mapNotNull { it.cardImage(wide) }.forEach {
            context.imageLoader.enqueue(ImageRequest.Builder(context).data(it).build())
        }
        delay(1200)
        onViewed(items[index])
    }
    if (LocalDensity.current.fontScale >= LIST_FONT_SCALE) {
        FeedList(category, items, store, position, onPosition, highlighted, bottomInset, onRead, onCoverage, onSaved, onSwipeTab)
    } else {
        FeedPager(category, items, store, position, onPosition, highlighted, bottomInset, onRead, onCoverage, onSaved, onSwipeTab)
    }
}

/**
 * One card per screen. The page follows the finger and, on release, snaps to the next card in a quarter of a
 * second whatever the speed of the swipe. Only the card on screen and its two neighbours exist at any time.
 * Up and down move between stories (pulling down on the first one refreshes); left and right change tabs.
 */
@Composable
private fun FeedPager(category: Category, items: List<Article>, store: NewsStore, position: String?,
                      onPosition: (String) -> Unit, highlighted: String?, bottomInset: Dp, onRead: (Article) -> Unit,
                      onCoverage: (Article) -> Unit, onSaved: () -> Unit, onSwipeTab: (Int) -> Unit) {
    val ids = remember(items) { items.map { it.id } + END_ID }
    val index = (position?.let { ids.indexOf(it) } ?: 0).coerceAtLeast(0)
    val scope = rememberCoroutineScope()
    val density = LocalDensity.current.density
    /** How far the page is from rest, in pixels: the finger's drag, then the snap's way back to zero. */
    var drag by remember { mutableFloatStateOf(0f) }
    var settle by remember { mutableStateOf<Job?>(null) }
    var refreshing by remember { mutableStateOf(false) }
    val state = rememberUpdatedState(Triple(index, ids, onSwipeTab))
    val report by rememberUpdatedState(onPosition)

    BoxWithConstraints(Modifier.fillMaxSize().clipToBounds()) {
        val pageHeight = constraints.maxHeight.toFloat()
        val wide = maxWidth >= 390.dp

        fun go(target: Int) {
            val (now, all, _) = state.value
            val to = target.coerceIn(0, all.lastIndex)
            settle?.cancel()
            // The page that was `to - now` pages away is now the one at rest, so it starts that far from rest.
            val from = drag + (to - now) * pageHeight
            drag = from
            if (to != now) report(all[to])
            settle = scope.launch {
                animate(from, 0f, animationSpec = tween(240, easing = FastOutSlowInEasing)) { v, _ -> drag = v }
            }
        }

        Box(Modifier.fillMaxSize().pointerInput(pageHeight) {
            val slop = viewConfiguration.touchSlop
            awaitEachGesture {
                val down = awaitFirstDown(requireUnconsumed = false, pass = PointerEventPass.Initial)
                val tracker = VelocityTracker()
                tracker.addPosition(down.uptimeMillis, down.position)
                var vertical: Boolean? = null
                var dx = 0f
                var dy = 0f
                var base = 0f
                var ended = false
                try {
                    while (true) {
                        val event = awaitPointerEvent(PointerEventPass.Initial)
                        val change = event.changes.firstOrNull { it.id == down.id } ?: break
                        if (!change.pressed) { ended = true; break }
                        tracker.addPosition(change.uptimeMillis, change.position)
                        dx = change.position.x - down.position.x
                        dy = change.position.y - down.position.y
                        if (vertical == null && (abs(dx) > slop || abs(dy) > slop)) {
                            vertical = abs(dy) >= abs(dx)
                            // A new touch takes the page from wherever the last snap had brought it.
                            settle?.cancel()
                            base = drag
                        }
                        if (vertical != null) change.consume()
                        if (vertical == true) {
                            val (now, all, _) = state.value
                            // Past either end the page follows the finger at a third of its speed.
                            val atEnd = (now == 0 && dy > 0) || (now == all.lastIndex && dy < 0)
                            drag = base + if (atEnd) dy / 3 else dy
                        }
                    }
                } finally {
                    val (now, _, swipeTab) = state.value
                    when {
                        vertical == true && ended -> {
                            val velocity = tracker.calculateVelocity().y
                            val step = FeedPaging.step(dy / density, (dy + velocity * 0.2f) / density)
                            val refresh = now == 0 && dy / density > FeedPaging.REFRESH_PULL
                            go(now + step)
                            if (refresh && !refreshing) scope.launch {
                                refreshing = true
                                try { store.refresh(atLeastMs = 600) } finally { refreshing = false }
                            }
                        }
                        // A gesture the system took away: the page must not be left where the finger was.
                        vertical == true -> go(now)
                        vertical == false && ended -> FeedPaging.tabStep(dx / density, dy / density)?.let(swipeTab)
                    }
                }
            }
        }) {
            for (i in (index - 1).coerceAtLeast(0)..(index + 1).coerceAtMost(ids.lastIndex)) {
                key(ids[i]) {
                    val offset = i - index
                    Box(Modifier.fillMaxSize().graphicsLayer { translationY = offset * pageHeight + drag }
                        .then(if (offset != 0) Modifier.clearAndSetSemantics { } else Modifier)) {
                        if (i < items.size) {
                            val article = items[i]
                            CardView(article, store, wide, bottomInset, flows = false, highlighted = highlighted == article.id,
                                onRead = { onRead(article) }, onCoverage = { onCoverage(article) },
                                modifier = Modifier.fillMaxSize())
                        } else {
                            EndOfTabCard(category, items.size, store, onTop = { go(0) }, onSaved = onSaved,
                                modifier = Modifier.padding(bottom = bottomInset))
                        }
                    }
                }
            }
            if (index == 0 && (drag / density > 24 || store.isRefreshing || refreshing)) {
                CircularProgressIndicator(Modifier.align(Alignment.TopCenter).padding(top = 14.dp).size(26.dp),
                    color = LocalPalette.current.accent, strokeWidth = 2.5.dp)
            }
        }
    }
}

/** The feed at the largest font sizes: full-length cards in a list that scrolls freely. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun FeedList(category: Category, items: List<Article>, store: NewsStore, position: String?,
                     onPosition: (String) -> Unit, highlighted: String?, bottomInset: Dp, onRead: (Article) -> Unit,
                     onCoverage: (Article) -> Unit, onSaved: () -> Unit, onSwipeTab: (Int) -> Unit) {
    val p = LocalPalette.current
    val list = rememberLazyListState()
    val scope = rememberCoroutineScope()
    val density = LocalDensity.current.density
    var refreshing by remember { mutableStateOf(false) }
    // The list follows `position` when something else moves it (Back to top, a tapped alert) ...
    LaunchedEffect(position) {
        val target = if (position == END_ID) items.size else items.indexOfFirst { it.id == position }
        if (target >= 0 && target != list.firstVisibleItemIndex) list.animateScrollToItem(target)
    }
    // ... and reports the card at the top as the reader scrolls.
    LaunchedEffect(items) {
        snapshotFlow { list.firstVisibleItemIndex }.collect { i -> onPosition(items.getOrNull(i)?.id ?: END_ID) }
    }
    BoxWithConstraints(Modifier.fillMaxSize()) {
        val page = maxHeight
        PullToRefreshBox(isRefreshing = refreshing, onRefresh = {
            scope.launch {
                refreshing = true
                try { store.refresh(atLeastMs = 1200) } finally { refreshing = false }
            }
        }, modifier = Modifier.fillMaxSize().pointerInput(Unit) {
            val slop = viewConfiguration.touchSlop
            awaitEachGesture {
                val down = awaitFirstDown(requireUnconsumed = false, pass = PointerEventPass.Initial)
                var dx = 0f
                var dy = 0f
                var ended = false
                while (true) {
                    val change = awaitPointerEvent(PointerEventPass.Initial).changes.firstOrNull { it.id == down.id } ?: break
                    if (!change.pressed) { ended = true; break }
                    dx = change.position.x - down.position.x
                    dy = change.position.y - down.position.y
                    // A clearly sideways drag is a tab change, not a scroll.
                    if (abs(dx) > slop * 3 && abs(dx) > 1.5f * abs(dy)) change.consume()
                }
                if (ended) FeedPaging.tabStep(dx / density, dy / density)?.let(onSwipeTab)
            }
        }) {
            LazyColumn(state = list, modifier = Modifier.fillMaxSize()) {
                items(items, key = { it.id }) { article ->
                    Column {
                        CardView(article, store, wide = true, bottomInset = 0.dp, flows = true,
                            highlighted = highlighted == article.id, onRead = { onRead(article) },
                            onCoverage = { onCoverage(article) })
                        Box(Modifier.fillMaxWidth().height(10.dp).background(p.secondaryBackground))
                    }
                }
                item(key = END_ID) {
                    EndOfTabCard(category, items.size, store, onTop = { onPosition(items.first().id) }, onSaved = onSaved,
                        modifier = Modifier.height(page).padding(bottom = bottomInset))
                }
            }
        }
    }
}

@Composable
private fun EmptyFeed(store: NewsStore) {
    val scope = rememberCoroutineScope()
    if (store.isRefreshing || (store.lastUpdated == null && !store.isOffline)) {
        SkeletonCard()
    } else {
        EmptyState(Icons.Outlined.Newspaper, "No stories",
            if (store.articles.isEmpty()) "Couldn't reach Techgist. Check your connection." else "Nothing here right now.") {
            PrimaryButton("Try again") { scope.launch { store.refresh() } }
        }
    }
}

/** The shape of a card while the first edition loads. No sample content is bundled. */
@Composable
fun SkeletonCard() {
    val p = LocalPalette.current
    BoxWithConstraints(Modifier.fillMaxSize().background(p.background)
        .clearAndSetSemantics { contentDescription = "Loading today's stories" }) {
        val hero = maxWidth * Theme.HERO_ASPECT
        Column(Modifier.fillMaxSize().shimmer()) {
            Box(Modifier.fillMaxWidth().height(hero)
                .clip(RoundedCornerShape(bottomStart = Theme.imageCorner, bottomEnd = Theme.imageCorner))
                .background(p.tertiaryFill))
            Column(Modifier.padding(horizontal = Theme.horizontal).padding(top = Theme.imageToMeta),
                verticalArrangement = Arrangement.spacedBy(10.dp)) {
                @Composable fun bar(width: Float, height: Dp) =
                    Box(Modifier.fillMaxWidth(width).height(height).clip(RoundedCornerShape(4.dp)).background(p.tertiaryFill))
                bar(0.25f, 12.dp)
                bar(1f, 22.dp)
                bar(0.7f, 22.dp)
                Spacer(Modifier.height(2.dp))
                for (w in listOf(1f, 1f, 0.95f, 1f, 0.9f, 0.6f)) bar(w, 14.dp)
            }
        }
    }
}

/** The last page of a tab. */
@Composable
fun EndOfTabCard(category: Category, count: Int, store: NewsStore, onTop: () -> Unit, onSaved: () -> Unit,
                 modifier: Modifier = Modifier) {
    val p = LocalPalette.current
    var detail = "$count ${if (count == 1) "story" else "stories"} in ${category.title}"
    store.lastUpdated?.let { detail += " · updated ${RelativeTime.spoken(it)}" }
    Box(modifier.fillMaxSize().background(p.background), contentAlignment = Alignment.Center) {
        // Centred when it fits, scrollable when the font size makes it taller than the page.
        Column(Modifier.verticalScroll(rememberScrollState()).padding(24.dp),
            horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Icon(Icons.Filled.CheckCircle, null, tint = p.accent, modifier = Modifier.size(56.dp))
            Text("You're all caught up", color = p.primary, textAlign = TextAlign.Center,
                style = TextStyle(fontFamily = FontFamily.Serif, fontWeight = FontWeight.Bold, fontSize = 22.sp))
            Text(detail, color = p.metaText, style = Theme.subheadline, textAlign = TextAlign.Center)
            PrimaryButton("Back to top", Modifier.padding(top = 8.dp).widthIn(min = 220.dp), onTop)
            Box(Modifier.widthIn(min = 220.dp).heightIn(min = 44.dp).clip(RoundedCornerShape(22.dp)).clickable(onClick = onSaved),
                contentAlignment = Alignment.Center) {
                Text("Saved stories", color = p.accent, style = Theme.bodySemibold)
            }
        }
    }
}
