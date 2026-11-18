package com.pranjald.techgist.ui

import android.Manifest
import android.app.TimePickerDialog
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import android.text.format.DateFormat
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.asPaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBars
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.KeyboardArrowRight
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.outlined.ArrowCircleUp
import androidx.compose.material.icons.outlined.BookmarkBorder
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.SwipeToDismissBox
import androidx.compose.material3.SwipeToDismissBoxValue
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.material3.rememberSwipeToDismissBoxState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.onClick
import androidx.compose.ui.semantics.role
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import com.pranjald.techgist.BuildConfig
import com.pranjald.techgist.Graph
import com.pranjald.techgist.data.FeedSource
import com.pranjald.techgist.data.Keys
import com.pranjald.techgist.data.NewsStore
import com.pranjald.techgist.data.RelativeTime
import com.pranjald.techgist.data.StorySearch
import com.pranjald.techgist.model.Article
import com.pranjald.techgist.model.NewsOrigin
import com.pranjald.techgist.notify.AlertSound
import com.pranjald.techgist.notify.DailyDigest
import com.pranjald.techgist.notify.StoryNotification
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.util.Calendar

/** A full-screen page over the feed: a bar with a back arrow, a title and actions, then the content. */
@Composable
private fun Page(title: String, onClose: () -> Unit, grouped: Boolean = false, actions: @Composable () -> Unit = {},
                 content: @Composable ColumnScope.() -> Unit) {
    val p = LocalPalette.current
    Column(Modifier.fillMaxSize().background(if (grouped) p.groupedBackground else p.background)
        .clickable(enabled = false) {}.statusBarsPadding().imePadding()) {
        Row(Modifier.fillMaxWidth().height(52.dp).padding(horizontal = 4.dp), verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(48.dp).clip(CircleShape).clickable(onClickLabel = "Back", role = Role.Button, onClick = onClose)
                .clearAndSetSemantics { contentDescription = "Back"; role = Role.Button; onClick { onClose(); true } },
                contentAlignment = Alignment.Center) {
                Icon(Icons.AutoMirrored.Filled.ArrowBack, null, tint = p.accent)
            }
            Text(title, color = p.primary, style = Theme.screenTitle, modifier = Modifier.weight(1f).padding(start = 4.dp))
            actions()
        }
        content()
    }
}

/** A compact row for lists of stories: thumbnail, headline, source and age. */
@Composable
fun StoryRow(article: Article, modifier: Modifier = Modifier) {
    val p = LocalPalette.current
    Row(modifier.fillMaxWidth().background(p.background).padding(horizontal = 16.dp, vertical = 10.dp)
        .clearAndSetSemantics {
            contentDescription = "${article.displayTitle}. ${article.source}, ${RelativeTime.spoken(article.publishedAt)}"
            role = Role.Button
        }) {
        StoryImage(article, article.thumbImage, Modifier.size(56.dp).clip(RoundedCornerShape(8.dp)))
        Spacer(Modifier.width(12.dp))
        Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(article.displayTitle, color = p.primary, style = Theme.rowTitle)
            Text("${article.source} · ${RelativeTime.short(article.publishedAt)}", color = p.metaText, style = Theme.caption)
        }
    }
}

@Composable
private fun Divider(start: Int = 16) {
    Box(Modifier.fillMaxWidth().padding(start = start.dp).height(1.dp).background(LocalPalette.current.hairline))
}

@Composable
private fun SearchField(value: String, onChange: (String) -> Unit, hint: String, modifier: Modifier = Modifier,
                        focus: FocusRequester? = null) {
    val p = LocalPalette.current
    Row(modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 6.dp).heightIn(min = 40.dp)
        .clip(RoundedCornerShape(10.dp)).background(p.secondaryBackground).padding(horizontal = 10.dp),
        verticalAlignment = Alignment.CenterVertically) {
        Icon(Icons.Filled.Search, null, tint = p.metaText, modifier = Modifier.size(20.dp))
        Spacer(Modifier.width(6.dp))
        Box(Modifier.weight(1f)) {
            if (value.isEmpty()) Text(hint, color = p.metaText, style = Theme.body)
            BasicTextField(value, onChange, singleLine = true, textStyle = Theme.body.copy(color = p.primary),
                cursorBrush = SolidColor(p.accent), keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
                modifier = Modifier.fillMaxWidth().then(if (focus != null) Modifier.focusRequester(focus) else Modifier))
        }
        if (value.isNotEmpty()) {
            Icon(Icons.Filled.Close, "Clear", tint = p.metaText,
                modifier = Modifier.size(28.dp).clip(CircleShape).clickable { onChange("") }.padding(4.dp))
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SavedScreen(store: NewsStore, onClose: () -> Unit) {
    val p = LocalPalette.current
    val context = LocalContext.current
    var query by remember { mutableStateOf("") }
    var confirmClear by remember { mutableStateOf(false) }
    val results = if (query.isEmpty()) store.saved else store.saved.filter { StorySearch.matches(it, query) }
    Page("Saved", onClose, actions = {
        if (store.saved.isNotEmpty()) TextButton(onClick = { confirmClear = true }) { Text("Clear all", color = p.destructive) }
    }) {
        if (store.saved.isEmpty()) {
            EmptyState(Icons.Outlined.BookmarkBorder, "Nothing saved", "Tap the bookmark on a story to keep it here.")
        } else {
            SearchField(query, { query = it }, "Search saved stories")
            if (results.isEmpty()) {
                EmptyState(Icons.Filled.Search, "No results for “$query”", "Check the spelling or try a new search.")
            } else {
                LazyColumn(Modifier.fillMaxSize(), contentPadding = WindowInsets.navigationBars.asPaddingValues()) {
                    items(results, key = { it.id }) { article ->
                        val dismiss = rememberSwipeToDismissBoxState(confirmValueChange = {
                            if (it == SwipeToDismissBoxValue.EndToStart) { store.toggleSaved(article); true } else false
                        })
                        SwipeToDismissBox(dismiss, enableDismissFromStartToEnd = false, modifier = Modifier.animateItem(),
                            backgroundContent = {
                                Box(Modifier.fillMaxSize().background(p.destructive).padding(horizontal = 20.dp),
                                    contentAlignment = Alignment.CenterEnd) {
                                    Text("Remove", color = Color.White, style = Theme.bodySemibold)
                                }
                            }) {
                            StoryRow(article, Modifier.clickable { Reader.open(context, article.url) })
                        }
                        Divider(84)
                    }
                }
            }
        }
    }
    if (confirmClear) {
        AlertDialog(onDismissRequest = { confirmClear = false }, title = { Text("Remove all saved stories?") },
            confirmButton = {
                TextButton(onClick = { confirmClear = false; store.clearSaved() }) { Text("Remove all", color = p.destructive) }
            },
            dismissButton = { TextButton(onClick = { confirmClear = false }) { Text("Cancel") } })
    }
}

/** Searches everything on this phone: the current edition's tabs and Saved. */
@Composable
fun SearchScreen(store: NewsStore, onClose: () -> Unit, onSelect: (Article) -> Unit) {
    var query by remember { mutableStateOf("") }
    val focus = remember { FocusRequester() }
    val results = remember(query, store.articles, store.saved) { StorySearch.results(query, store.articles, store.saved) }
    // The field is focused as the page opens, so typing works straight away.
    LaunchedEffect(Unit) { delay(250); runCatching { focus.requestFocus() } }
    Page("Search", onClose) {
        SearchField(query, { query = it }, "Headline, topic or outlet", focus = focus)
        when {
            query.isBlank() -> EmptyState(Icons.Filled.Search, "Search stories", "Find a story by headline, summary or outlet.")
            results.isEmpty() -> EmptyState(Icons.Filled.Search, "No results for “$query”", "Check the spelling or try a new search.")
            else -> LazyColumn(Modifier.fillMaxSize(), contentPadding = WindowInsets.navigationBars.asPaddingValues()) {
                items(results, key = { it.id }) { article ->
                    StoryRow(article, Modifier.clickable { onClose(); onSelect(article) })
                    Divider(84)
                }
            }
        }
    }
}

/** Every outlet reporting a story. Each one opens its own report; the list stays underneath. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun CoverageSheet(article: Article, onDismiss: () -> Unit, onRead: (Article) -> Unit) {
    val p = LocalPalette.current
    val context = LocalContext.current
    ModalBottomSheet(onDismissRequest = onDismiss, containerColor = p.groupedBackground,
        sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = LocalContext.current.resources.configuration.fontScale >= LIST_FONT_SCALE)) {
        Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(bottom = 24.dp)) {
            Text("Full coverage", color = p.primary, style = Theme.screenTitle,
                modifier = Modifier.align(Alignment.CenterHorizontally).padding(bottom = 14.dp))
            Text("Covered by ${article.coverage} outlets · first reported ${RelativeTime.spoken(article.publishedAt)} by ${article.source}",
                color = p.metaText, style = Theme.subheadline.copy(fontWeight = FontWeight.Medium),
                modifier = Modifier.padding(horizontal = 32.dp).padding(bottom = 8.dp))
            Group {
                Row(Modifier.fillMaxWidth().clickable { onRead(article) }.padding(horizontal = 16.dp, vertical = 10.dp),
                    verticalAlignment = Alignment.CenterVertically) {
                    SourceMonogram(article.source, 32.dp)
                    Spacer(Modifier.width(12.dp))
                    Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                        Text(article.source, color = p.primary, style = Theme.bodySemibold)
                        Text(article.title, color = p.metaText, style = Theme.meta)
                    }
                    Icon(Icons.AutoMirrored.Filled.KeyboardArrowRight, null, tint = p.metaText, modifier = Modifier.size(20.dp))
                }
                for (name in article.alsoCoveredBy ?: emptyList()) {
                    Divider(60)
                    // An older edition names outlets without their links.
                    val link = article.coverageLinks?.get(name)
                    Row(Modifier.fillMaxWidth()
                        .then(if (link != null) Modifier.clickable(onClickLabel = "Opens this outlet's report") { Reader.open(context, link) } else Modifier)
                        .padding(horizontal = 16.dp, vertical = 10.dp).heightIn(min = 32.dp),
                        verticalAlignment = Alignment.CenterVertically) {
                        SourceMonogram(name, 32.dp)
                        Spacer(Modifier.width(12.dp))
                        Text(name, color = p.primary, style = Theme.body, modifier = Modifier.weight(1f))
                        if (link != null) {
                            Icon(Icons.AutoMirrored.Filled.KeyboardArrowRight, null, tint = p.metaText, modifier = Modifier.size(20.dp))
                        }
                    }
                }
            }
            article.points?.let { points ->
                Spacer(Modifier.height(20.dp))
                Group {
                    Row(Modifier.padding(horizontal = 16.dp, vertical = 12.dp), verticalAlignment = Alignment.CenterVertically) {
                        Icon(Icons.Outlined.ArrowCircleUp, null, tint = p.accent)
                        Spacer(Modifier.width(12.dp))
                        Text("$points points on Hacker News", color = p.primary, style = Theme.body)
                    }
                }
            }
        }
    }
}

/** A rounded group of rows on the grouped background, as in a settings list. */
@Composable
private fun Group(header: String? = null, footer: String? = null, content: @Composable ColumnScope.() -> Unit) {
    val p = LocalPalette.current
    Column(Modifier.padding(horizontal = 16.dp)) {
        if (header != null) {
            Text(header.uppercase(), color = p.metaText, style = Theme.meta, modifier = Modifier.padding(start = 16.dp, bottom = 7.dp))
        }
        Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(p.cell), content = content)
        if (footer != null) {
            Text(footer, color = p.metaText, style = Theme.meta, modifier = Modifier.padding(horizontal = 16.dp).padding(top = 7.dp))
        }
    }
}

@Composable
private fun SettingRow(title: String, value: String? = null, accent: Boolean = false, enabled: Boolean = true,
                       onClick: (() -> Unit)? = null, trailing: (@Composable () -> Unit)? = null) {
    val p = LocalPalette.current
    Row(Modifier.fillMaxWidth().heightIn(min = 48.dp)
        .then(if (onClick != null) Modifier.clickable(enabled = enabled, onClick = onClick) else Modifier)
        .padding(horizontal = 16.dp, vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(title, color = if (accent) p.accent.copy(alpha = if (enabled) 1f else 0.5f) else p.primary, style = Theme.body,
            modifier = Modifier.weight(1f))
        if (value != null) Text(value, color = p.metaText, style = Theme.body)
        trailing?.invoke()
    }
}

@Composable
private fun ToggleRow(title: String, checked: Boolean, leading: (@Composable () -> Unit)? = null, onChange: (Boolean) -> Unit) {
    val p = LocalPalette.current
    Row(Modifier.fillMaxWidth().heightIn(min = 48.dp).clickable(role = Role.Switch) { onChange(!checked) }
        .padding(horizontal = 16.dp, vertical = 4.dp), verticalAlignment = Alignment.CenterVertically) {
        if (leading != null) { leading(); Spacer(Modifier.width(10.dp)) }
        Text(title, color = p.primary, style = Theme.body, modifier = Modifier.weight(1f))
        Switch(checked, null, colors = SwitchDefaults.colors(checkedTrackColor = p.accent, checkedThumbColor = Color.White,
            uncheckedTrackColor = p.tertiaryFill, uncheckedThumbColor = Color.White, uncheckedBorderColor = Color.Transparent))
    }
}

@Composable
fun SettingsScreen(graph: Graph, onClose: () -> Unit) {
    val p = LocalPalette.current
    val context = LocalContext.current
    val settings = graph.settings
    val store = graph.store
    var digest by remember { mutableStateOf(settings.bool(Keys.DIGEST_ENABLED)) }
    var minute by remember { mutableIntStateOf(DailyDigest.minuteOfDay(settings)) }
    var breaking by remember { mutableStateOf(settings.bool(Keys.BREAKING_NEWS)) }
    var sound by remember { mutableStateOf(AlertSound.current(settings)) }
    var showDenied by remember { mutableStateOf(false) }
    var pickSound by remember { mutableStateOf(false) }
    var sampleSent by remember { mutableStateOf(false) }
    var sources by remember { mutableStateOf(false) }
    /** The switch that is waiting for the permission dialog's answer. */
    var asking by remember { mutableStateOf<String?>(null) }

    fun set(key: String, on: Boolean) {
        settings.put(key, on)
        if (key == Keys.DIGEST_ENABLED) { digest = on; DailyDigest.reschedule(context) }
        else { breaking = on; graph.syncPush() }
    }
    val permission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        asking?.let { key -> set(key, granted) }
        asking = null
        showDenied = !granted
    }
    /** Turning a notification switch on asks for the permission when the phone has not given it. */
    fun turnOn(key: String) {
        when {
            StoryNotification.allowed(context) -> set(key, true)
            Build.VERSION.SDK_INT >= 33 -> { asking = key; permission.launch(Manifest.permission.POST_NOTIFICATIONS) }
            else -> showDenied = true
        }
    }

    if (sources) {
        BackHandler { sources = false }
        SourcesScreen(store) { sources = false }
        return
    }
    Page("Settings", onClose, grouped = true) {
        Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState())
            .padding(top = 8.dp, bottom = 24.dp).padding(WindowInsets.navigationBars.asPaddingValues()),
            verticalArrangement = Arrangement.spacedBy(26.dp)) {
            Group("Notifications", footer = (if (graph.push.isAvailable)
                "One notification each morning with the top story, and alerts from Techgist through the day as big stories break, never at night. Turning alerts off removes this phone from Techgist's alert list."
            else "One notification each morning with the top story, and an alert when a story is covered by several outlets at once, at most 3 a day. Android decides when the app checks in the background, so alerts can arrive a while after a story breaks.")
                + if (digest || breaking) " The preview arrives 5 seconds after you ask for it." else "") {
                ToggleRow("Morning gist", digest) { if (it) turnOn(Keys.DIGEST_ENABLED) else set(Keys.DIGEST_ENABLED, false) }
                if (digest) {
                    Divider()
                    val time = remember(minute) {
                        val c = Calendar.getInstance().apply { set(Calendar.HOUR_OF_DAY, minute / 60); set(Calendar.MINUTE, minute % 60) }
                        DateFormat.getTimeFormat(context).format(c.time)
                    }
                    SettingRow("Time", time, onClick = {
                        TimePickerDialog(context, { _, h, m ->
                            minute = h * 60 + m
                            settings.put(Keys.DIGEST_MINUTE, minute)
                            DailyDigest.reschedule(context)
                        }, minute / 60, minute % 60, DateFormat.is24HourFormat(context)).show()
                    })
                }
                Divider()
                ToggleRow("Big story alerts", breaking) { if (it) turnOn(Keys.BREAKING_NEWS) else set(Keys.BREAKING_NEWS, false) }
                if (digest || breaking) {
                    Divider()
                    SettingRow("Alert sound", sound.title, onClick = { pickSound = true })
                    val story = store.topStory
                    if (story != null) {
                        Divider()
                        SettingRow(if (sampleSent) "Preview on its way" else "Preview an alert", accent = true, enabled = !sampleSent,
                            onClick = {
                                sampleSent = true
                                val app = context.applicationContext
                                graph.scope.launch {
                                    delay(5000)
                                    StoryNotification.post(app, story, StoryNotification.Kind.BREAKING)
                                }
                            })
                    }
                }
            }
            Group(footer = "Muted sources don't appear in any tab. You can also mute a source from the More button on a story.") {
                SettingRow("Sources", if (store.disabledSources.isEmpty()) "All on" else "${store.disabledSources.size} muted",
                    onClick = { sources = true }) {
                    Icon(Icons.AutoMirrored.Filled.KeyboardArrowRight, null, tint = p.metaText, modifier = Modifier.size(20.dp))
                }
            }
            Group("About", footer = "Summaries marked AI are written by Techgist's model from the publisher's story and checked against it. Every card links to the original. Your reading, saves and settings stay on this device."
                + if (graph.push.isAvailable) " With Big story alerts on, Techgist's server holds this phone's notification address, time zone and language so it can send them, and nothing else." else "") {
                SettingRow("Version", "${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE})")
                val origin = when (val o = store.origin) {
                    is NewsOrigin.FromEdition -> "Techgist edition, ${RelativeTime.spoken(o.publishedAt)}"
                    NewsOrigin.Direct -> "Direct from publishers"
                    null -> null
                }
                if (origin != null) {
                    Divider()
                    Column(Modifier.padding(horizontal = 16.dp, vertical = 8.dp)) {
                        Text("Stories", color = p.primary, style = Theme.body)
                        Text(origin, color = p.metaText, style = Theme.subheadline)
                    }
                }
                Divider()
                SettingRow("Privacy Policy", accent = true, onClick = { Reader.open(context, PRIVACY_URL) })
                Divider()
                SettingRow("Support", accent = true, onClick = { Reader.open(context, SUPPORT_URL) })
            }
        }
    }

    if (pickSound) {
        AlertDialog(onDismissRequest = { pickSound = false }, title = { Text("Alert sound") },
            confirmButton = { TextButton(onClick = { pickSound = false }) { Text("Done") } },
            text = {
                Column {
                    for (option in AlertSound.entries) {
                        Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(8.dp)).clickable(role = Role.RadioButton) {
                            sound = option
                            settings.put(Keys.ALERT_SOUND, option.key)
                            // Let them hear it, and tell the server which one this phone wants.
                            option.preview(context)
                            StoryNotification.ensureChannel(context, option)
                            graph.syncPush()
                        }.padding(vertical = 4.dp), verticalAlignment = Alignment.CenterVertically) {
                            RadioButton(selected = sound == option, onClick = null, modifier = Modifier.padding(8.dp))
                            Text(option.title, style = Theme.body)
                        }
                    }
                }
            })
    }
    if (showDenied) {
        AlertDialog(onDismissRequest = { showDenied = false }, title = { Text("Notifications are off") },
            text = { Text("Allow notifications for Techgist in Settings to get the morning gist and big story alerts.") },
            confirmButton = {
                TextButton(onClick = {
                    showDenied = false
                    runCatching {
                        context.startActivity(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                            .putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                    }.onFailure {
                        runCatching {
                            context.startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                                Uri.parse("package:${context.packageName}")).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                        }
                    }
                }) { Text("Open Settings") }
            },
            dismissButton = { TextButton(onClick = { showDenied = false }) { Text("Cancel") } })
    }
}

/** Every source in the current edition, with a switch to mute it. */
@Composable
private fun SourcesScreen(store: NewsStore, onClose: () -> Unit) {
    val names = remember(store.articles, store.disabledSources) {
        (store.articles.map { it.source } + FeedSource.all.map { it.name } + store.disabledSources).toSet()
            .sortedWith(String.CASE_INSENSITIVE_ORDER)
    }
    Page("Sources", onClose, grouped = true) {
        LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(top = 8.dp,
            bottom = 24.dp + WindowInsets.navigationBars.asPaddingValues().calculateBottomPadding())) {
            if (store.disabledSources.isNotEmpty()) {
                item {
                    Group { SettingRow("Unmute all", accent = true, onClick = { store.disabledSources = emptySet() }) }
                    Spacer(Modifier.height(20.dp))
                }
            }
            item {
                Group {
                    names.forEachIndexed { i, name ->
                        if (i > 0) Divider(52)
                        ToggleRow(name, name !in store.disabledSources, leading = { SourceMonogram(name, 26.dp) }) { on ->
                            store.disabledSources = if (on) store.disabledSources - name else store.disabledSources + name
                        }
                    }
                }
            }
        }
    }
}

const val PRIVACY_URL = "https://pranjaldoshi96.github.io/apps/techgist/privacy/"
const val SUPPORT_URL = "https://pranjaldoshi96.github.io/apps/techgist/support/"
