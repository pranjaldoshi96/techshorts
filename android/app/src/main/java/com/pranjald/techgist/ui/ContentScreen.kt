package com.pranjald.techgist.ui

import android.Manifest
import android.os.Build
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.spring
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInHorizontally
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutHorizontally
import androidx.compose.animation.slideOutVertically
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.asPaddingValues
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.navigationBars
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowUpward
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.outlined.BookmarkBorder
import androidx.compose.material.icons.outlined.History
import androidx.compose.material.icons.outlined.Schedule
import androidx.compose.material.icons.outlined.Settings
import androidx.compose.material.icons.outlined.WifiOff
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.onClick
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.selected
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.pranjald.techgist.AlertRequest
import com.pranjald.techgist.Graph
import com.pranjald.techgist.R
import com.pranjald.techgist.data.Keys
import com.pranjald.techgist.data.RelativeTime
import com.pranjald.techgist.model.Article
import com.pranjald.techgist.model.Category
import com.pranjald.techgist.model.NewsOrigin
import com.pranjald.techgist.notify.DailyDigest
import com.pranjald.techgist.notify.StoryNotification
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.time.Duration
import java.time.Instant

/** The one page on top of the feed. A single value, so two can never compete to appear. */
sealed interface Sheet {
    data object Saved : Sheet
    data object Settings : Sheet
    data object Search : Sheet
    data class Coverage(val article: Article) : Sheet
}

@Composable
fun ContentScreen(graph: Graph) {
    val store = graph.store
    val readState = graph.readState
    val p = LocalPalette.current
    val context = LocalContext.current
    val haptics = LocalHapticFeedback.current
    val scope = rememberCoroutineScope()
    var sheet by remember { mutableStateOf<Sheet?>(null) }
    /** The card on screen in each tab, so a tab comes back where it was left. */
    val positions = remember { mutableStateMapOf<Category, String>() }
    var forward by remember { mutableStateOf(true) }
    var highlighted by remember { mutableStateOf<String?>(null) }
    var pillHidden by remember { mutableStateOf(false) }
    /** Bumped when a story becomes read, so the tabs' unread dots are worked out again. */
    var readVersion by remember { mutableIntStateOf(0) }
    DisposableEffect(readState) {
        readState.onChange = { readVersion++ }
        onDispose { readState.onChange = null }
    }

    fun read(article: Article) {
        readState.markRead(article.id)
        Reader.open(context, article.url)
    }
    fun scrollToTop() {
        store.visibleArticles.firstOrNull()?.let { positions[store.category] = it.id }
    }
    fun select(tab: Category) {
        forward = tab.ordinal >= store.category.ordinal
        if (tab == store.category) scrollToTop() else {
            haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
            store.category = tab
        }
    }

    // On the first launch Android asks whether Techgist may send notifications. Yes turns on the morning gist and
    // big story alerts; no leaves both off. Either can be changed in Settings afterwards. Asked once.
    fun answered(granted: Boolean) {
        graph.settings.put(Keys.DIGEST_ENABLED, granted)
        graph.settings.put(Keys.BREAKING_NEWS, granted)
        if (granted) DailyDigest.reschedule(context)
        graph.syncPush()
    }
    val permission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission(), ::answered)
    LaunchedEffect(Unit) {
        if (!graph.settings.bool(Keys.NOTIFICATIONS_OFFERED)) {
            graph.settings.put(Keys.NOTIFICATIONS_OFFERED, true)
            if (Build.VERSION.SDK_INT >= 33) permission.launch(Manifest.permission.POST_NOTIFICATIONS)
            else answered(StoryNotification.allowed(context))
        }
    }
    // While the app is open, look for a newer edition every 5 minutes and offer it with the pill.
    LaunchedEffect(Unit) {
        while (true) {
            delay(300_000)
            store.checkForNewStories()
        }
    }
    // The pill steps aside after 8 seconds; the stories still arrive on the next refresh or tab change.
    LaunchedEffect(store.waitingCount) {
        pillHidden = false
        if (store.waitingCount > 0) {
            haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
            delay(8000)
            pillHidden = true
        }
    }
    LaunchedEffect(store.scrollToTopRequests) {
        if (store.scrollToTopRequests > 0) positions.clear()
    }
    LaunchedEffect(store.focus) {
        val focus = store.focus ?: return@LaunchedEffect
        positions[focus.category] = focus.id
        highlighted = focus.id
        delay(1600)
        if (highlighted == focus.id) highlighted = null
    }
    // A tapped alert: the card first; the full story is one tap away, as everywhere else. Only a story that has
    // left the edition (an old alert) opens its page, since there is no card to show.
    LaunchedEffect(graph.pendingRequest) {
        val request = graph.pendingRequest ?: return@LaunchedEffect
        graph.pendingRequest = null
        sheet = null
        when (request) {
            is AlertRequest.Show -> scope.launch {
                if (!store.showStory(request.url, request.eventId)) request.url?.let { Reader.open(context, it) }
            }
            is AlertRequest.Read -> {
                store.focusOn(request.url)
                readState.markRead(request.url)
                Reader.open(context, request.url)
            }
        }
    }
    BackHandler(sheet != null) { sheet = null }

    val bottomInset = WindowInsets.navigationBars.asPaddingValues().calculateBottomPadding()
    Box(Modifier.fillMaxSize().background(p.background)) {
        Column(Modifier.fillMaxSize().statusBarsPadding()) {
            TopBar(onLogo = ::scrollToTop, onSearch = { sheet = Sheet.Search }, onSaved = { sheet = Sheet.Saved },
                onSettings = { sheet = Sheet.Settings })
            val unread = remember(store.feeds, readVersion) {
                Category.entries.filter { tab -> store.articles(tab).any { !readState.isRead(it.id) } }.toSet()
            }
            ChipBar(store.category, unread, ::select)
            ProgressLine(progress(store.visibleArticles, positions[store.category]))
            banner(store.articles.isNotEmpty(), store.isOffline, store.lastUpdated, store.origin)?.let { StateBanner(it.first, it.second) }
            Box(Modifier.weight(1f).fillMaxWidth()) {
                AnimatedContent(store.category, transitionSpec = {
                    val way = if (forward) 1 else -1
                    (slideInHorizontally(tween(250)) { it * way } togetherWith slideOutHorizontally(tween(250)) { -it * way })
                }, label = "tab") { tab ->
                    FeedView(tab, store.articles(tab), store, positions[tab], { positions[tab] = it }, highlighted, bottomInset,
                        onRead = ::read, onCoverage = { sheet = Sheet.Coverage(it) },
                        onViewed = { readState.markRead(it.id) }, onSaved = { sheet = Sheet.Saved },
                        onSwipeTab = { step -> Category.entries.getOrNull(store.category.ordinal + step)?.let(::select) })
                }
                androidx.compose.animation.AnimatedVisibility(store.waitingCount > 0 && !pillHidden,
                    enter = slideInVertically(spring(dampingRatio = 0.8f)) { -it * 2 } + fadeIn(),
                    exit = slideOutVertically { -it * 2 } + fadeOut(),
                    modifier = Modifier.align(Alignment.TopCenter).padding(top = 12.dp)) {
                    NewStoriesPill(store.waitingCount) { store.showWaitingStories() }
                }
            }
        }

        SlideUp(sheet == Sheet.Saved) { SavedScreen(store) { sheet = null } }
        SlideUp(sheet == Sheet.Search) {
            // Brings a story's card on screen, or opens the story when it isn't in the edition any more.
            SearchScreen(store, onClose = { sheet = null }) { if (!store.focusOn(it.url)) read(it) }
        }
        SlideUp(sheet == Sheet.Settings) { SettingsScreen(graph) { sheet = null } }
        (sheet as? Sheet.Coverage)?.let { CoverageSheet(it.article, onDismiss = { sheet = null }, onRead = ::read) }
    }
}

@Composable
private fun SlideUp(visible: Boolean, content: @Composable () -> Unit) {
    AnimatedVisibility(visible, enter = slideInVertically(tween(280)) { it } + fadeIn(tween(120)),
        exit = slideOutVertically(tween(220)) { it } + fadeOut(tween(220))) { content() }
}

/** How far through the tab the reader is, 0 to 1. */
fun progress(items: List<Article>, position: String?): Float {
    if (items.isEmpty()) return 0f
    if (position == null) return 1f / items.size
    if (position == END_ID) return 1f
    val index = items.indexOfFirst { it.id == position }.coerceAtLeast(0)
    return (index + 1f) / items.size
}

/** The one line under the tabs when the stories are old, offline or without AI text; null when all is well. */
fun banner(hasStories: Boolean, offline: Boolean, lastUpdated: Instant?, origin: NewsOrigin?,
           now: Instant = Instant.now()): Pair<String, ImageVector>? {
    if (!hasStories) return null
    if (offline && lastUpdated != null) return "Offline · stories from ${RelativeTime.spoken(lastUpdated, now)}" to Icons.Outlined.WifiOff
    return when {
        origin is NewsOrigin.Direct || (origin is NewsOrigin.FromEdition && origin.degraded) ->
            "Summaries catching up · showing headlines" to Icons.Outlined.History
        origin is NewsOrigin.FromEdition && Duration.between(origin.publishedAt, now) > Duration.ofHours(2) ->
            "Updated ${RelativeTime.spoken(origin.publishedAt, now)}" to Icons.Outlined.Schedule
        else -> null
    }
}

@Composable
private fun TopBar(onLogo: () -> Unit, onSearch: () -> Unit, onSaved: () -> Unit, onSettings: () -> Unit) {
    val p = LocalPalette.current
    Row(Modifier.fillMaxWidth().height(52.dp).padding(start = 16.dp, end = 6.dp), verticalAlignment = Alignment.CenterVertically) {
        Row(Modifier.fillMaxHeight().clickable(interactionSource = remember { MutableInteractionSource() }, indication = null,
            onClickLabel = "Goes back to the first story", onClick = onLogo)
            .semantics(mergeDescendants = true) { contentDescription = "Techgist" },
            verticalAlignment = Alignment.CenterVertically) {
            Image(painterResource(R.drawable.logo), null, Modifier.size(28.dp).clip(RoundedCornerShape(6.dp)))
            Spacer(Modifier.width(8.dp))
            Text("Techgist", color = p.primary,
                style = TextStyle(fontFamily = FontFamily.Serif, fontWeight = FontWeight.Bold, fontSize = 19.sp))
        }
        Spacer(Modifier.weight(1f))
        BarButton(Icons.Filled.Search, "Search", onSearch)
        BarButton(Icons.Outlined.BookmarkBorder, "Saved stories", onSaved)
        BarButton(Icons.Outlined.Settings, "Settings", onSettings)
    }
}

@Composable
private fun BarButton(icon: ImageVector, label: String, onClick: () -> Unit) {
    Box(Modifier.size(46.dp).clip(CircleShape).clickable(onClickLabel = label, role = Role.Button, onClick = onClick)
        .clearAndSetSemantics { contentDescription = label; role = Role.Button; onClick { onClick(); true } },
        contentAlignment = Alignment.Center) {
        Icon(icon, null, tint = LocalPalette.current.accent, modifier = Modifier.size(25.dp))
    }
}

/** The tab chips. The selected chip is filled with the accent colour; a dot marks tabs with unread stories. */
@Composable
private fun ChipBar(selection: Category, unread: Set<Category>, onSelect: (Category) -> Unit) {
    val p = LocalPalette.current
    Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()).padding(horizontal = 16.dp),
        horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        for (category in Category.entries) {
            val selected = selection == category
            val fill by animateColorAsState(if (selected) p.accent else p.secondaryBackground, spring(dampingRatio = 0.85f), label = "chip")
            val dot = category in unread && !selected
            // The capsule is about 34 dp tall; the tap area is 44.
            Box(Modifier.heightIn(min = 44.dp).clickable(interactionSource = remember { MutableInteractionSource() },
                indication = null, role = Role.Tab) { onSelect(category) }
                .clearAndSetSemantics {
                    contentDescription = category.title
                    this.selected = selected
                    role = Role.Tab
                    if (dot) stateDescription = "Unread stories"
                    onClick { onSelect(category); true }
                }, contentAlignment = Alignment.Center) {
                Row(Modifier.clip(CircleShape).background(fill).padding(horizontal = 14.dp, vertical = 7.dp),
                    verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text(category.title, color = if (selected) p.onAccent else p.primary,
                        style = Theme.subheadline.copy(fontWeight = FontWeight.SemiBold), maxLines = 1)
                    if (dot) Box(Modifier.size(6.dp).clip(CircleShape).background(p.accent))
                }
            }
        }
    }
}

/** A thin line showing how far through the tab the reader is. */
@Composable
private fun ProgressLine(fraction: Float) {
    val p = LocalPalette.current
    val shown by animateFloatAsState(fraction.coerceIn(0f, 1f), tween(250), label = "progress")
    Box(Modifier.fillMaxWidth().height(2.dp).background(p.primary.copy(alpha = 0.08f)).clearAndSetSemantics { }) {
        Box(Modifier.fillMaxWidth(shown).fillMaxHeight().background(p.accent))
    }
}

@Composable
private fun StateBanner(text: String, icon: ImageVector) {
    val p = LocalPalette.current
    Row(Modifier.fillMaxWidth().background(p.secondaryBackground).padding(vertical = 6.dp, horizontal = Theme.horizontal),
        horizontalArrangement = Arrangement.Center, verticalAlignment = Alignment.CenterVertically) {
        Icon(icon, null, tint = p.primary, modifier = Modifier.size(16.dp))
        Spacer(Modifier.width(6.dp))
        Text(text, color = p.primary, style = Theme.meta.copy(fontWeight = FontWeight.Medium), maxLines = 2,
            overflow = TextOverflow.Ellipsis)
    }
}

/** "3 new stories": appears when a newer edition is waiting, and brings it in when tapped. */
@Composable
private fun NewStoriesPill(count: Int, onClick: () -> Unit) {
    val p = LocalPalette.current
    Row(Modifier.shadow(8.dp, CircleShape).clip(CircleShape).background(p.accent)
        .clickable(onClickLabel = "Shows the newest stories", onClick = onClick)
        .heightIn(min = 44.dp).padding(horizontal = 16.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
        Icon(Icons.Filled.ArrowUpward, null, tint = p.onAccent, modifier = Modifier.size(18.dp))
        Text("$count new ${if (count == 1) "story" else "stories"}", color = p.onAccent,
            style = Theme.subheadline.copy(fontWeight = FontWeight.Bold))
    }
}
