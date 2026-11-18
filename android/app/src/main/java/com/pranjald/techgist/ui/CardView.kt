package com.pranjald.techgist.ui

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.KeyboardArrowRight
import androidx.compose.material.icons.filled.AutoAwesome
import androidx.compose.material.icons.filled.Bookmark
import androidx.compose.material.icons.filled.LocalFireDepartment
import androidx.compose.material.icons.filled.MoreHoriz
import androidx.compose.material.icons.outlined.BookmarkBorder
import androidx.compose.material.icons.outlined.Share
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.clipToBounds
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Shadow
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.layout.IntrinsicMeasurable
import androidx.compose.ui.layout.IntrinsicMeasureScope
import androidx.compose.ui.layout.Layout
import androidx.compose.ui.layout.Measurable
import androidx.compose.ui.layout.MeasurePolicy
import androidx.compose.ui.layout.MeasureResult
import androidx.compose.ui.layout.MeasureScope
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.semantics.CustomAccessibilityAction
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.customActions
import androidx.compose.ui.semantics.onClick
import androidx.compose.ui.semantics.role
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Constraints
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import coil.compose.AsyncImage
import coil.compose.AsyncImagePainter
import com.pranjald.techgist.data.NewsStore
import com.pranjald.techgist.data.RelativeTime
import com.pranjald.techgist.model.Article
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/** A story's picture, filling whatever frame it is given. Stories without a usable picture get `StoryCover`. */
@Composable
fun StoryImage(article: Article, url: String?, modifier: Modifier = Modifier) {
    val p = LocalPalette.current
    var failed by remember(url) { mutableStateOf(false) }
    var loaded by remember(url) { mutableStateOf(false) }
    Box(modifier.background(p.secondaryBackground).clipToBounds().clearAndSetSemantics { }) {
        if (url == null || failed) {
            StoryCover(article)
        } else {
            if (!loaded) Box(Modifier.fillMaxSize().background(p.tertiaryFill).shimmer())
            AsyncImage(model = url, contentDescription = null, contentScale = ContentScale.Crop,
                modifier = Modifier.fillMaxSize(),
                onState = {
                    if (it is AsyncImagePainter.State.Error) failed = true
                    if (it is AsyncImagePainter.State.Success) loaded = true
                })
        }
    }
}

/**
 * Shows the first of `candidates` whose natural height fits the space; the last one when none does. This is how
 * the card picks the richest layout that fits, without ever shrinking the text.
 */
@Composable
private fun FirstThatFits(modifier: Modifier, candidates: List<@Composable () -> Unit>) {
    Layout(content = { candidates.forEach { Box { it() } } }, modifier = modifier) { measurables, constraints ->
        var chosen = measurables.lastIndex
        for (i in 0 until measurables.lastIndex) {
            if (measurables[i].minIntrinsicHeight(constraints.maxWidth) <= constraints.maxHeight) { chosen = i; break }
        }
        val placeable = measurables[chosen].measure(constraints.copy(minHeight = 0))
        layout(constraints.maxWidth, constraints.maxHeight) { placeable.place(0, 0) }
    }
}

/**
 * A picture above text. The text is rigid; the picture is what gives way when space is short, between
 * `minHero` and `maxHero`. Its natural (minimum) height is the smallest picture plus the text.
 */
private class HeroPolicy(val minHero: Int, val aspect: Float) : MeasurePolicy {
    override fun MeasureScope.measure(measurables: List<Measurable>, constraints: Constraints): MeasureResult {
        val width = constraints.maxWidth
        val text = measurables[1].measure(Constraints(minWidth = width, maxWidth = width))
        val maxHero = maxOf(minHero, (width * aspect).toInt())
        val room = if (constraints.hasBoundedHeight) constraints.maxHeight - text.height else minHero
        val heroHeight = room.coerceIn(minHero, maxHero)
        val hero = measurables[0].measure(Constraints.fixed(width, heroHeight))
        return layout(width, heroHeight + text.height) {
            hero.place(0, 0)
            text.place(0, heroHeight)
        }
    }

    override fun IntrinsicMeasureScope.minIntrinsicHeight(measurables: List<IntrinsicMeasurable>, width: Int): Int =
        minHero + measurables[1].minIntrinsicHeight(width)
}

/**
 * One story, one screen. The text never shrinks: the card picks the richest layout that fits.
 * 1. Hero picture + full summary.
 * 2. Hero picture + short summary (the first two sentences).
 * 3. Thumbnail + full summary, for small phones and long headlines.
 * 4. Thumbnail + short summary.
 * 5. Text only + short summary. Only here, as a last resort, may the summary end in an ellipsis.
 * At the largest font sizes (`flows`) the card is as tall as its text and the feed scrolls freely.
 */
@Composable
fun CardView(article: Article, store: NewsStore, wide: Boolean, bottomInset: Dp, flows: Boolean, highlighted: Boolean,
             onRead: () -> Unit, onCoverage: () -> Unit, modifier: Modifier = Modifier) {
    val p = LocalPalette.current
    val context = LocalContext.current
    val isSaved = store.isSaved(article)
    val full = article.displaySummary
    val short = article.shortSummary
    val border by animateFloatAsState(if (highlighted) 1f else 0f, tween(350), label = "highlight")

    fun copyLink() {
        context.getSystemService(ClipboardManager::class.java)
            ?.setPrimaryClip(ClipData.newPlainText("Story link", article.url))
    }
    fun mute() { store.disabledSources = store.disabledSources + article.source }

    val spoken = remember(article) { spokenLabel(article) }
    val contentModifier = Modifier
        .clickable(interactionSource = remember { MutableInteractionSource() }, indication = null, onClick = onRead)
        .clearAndSetSemantics {
            contentDescription = spoken
            role = Role.Button
            onClick("Opens the full story") { onRead(); true }
            customActions = buildList {
                add(CustomAccessibilityAction(if (isSaved) "Remove from saved" else "Save") { store.toggleSaved(article); true })
                if (article.coverage >= 2) add(CustomAccessibilityAction("Full coverage") { onCoverage(); true })
                add(CustomAccessibilityAction("Copy link") { copyLink(); true })
                add(CustomAccessibilityAction("Mute ${article.source}") { mute(); true })
            }
        }

    Column(modifier.background(p.background).border(4.dp, p.accent.copy(alpha = border))) {
        if (flows) {
            Box(contentModifier.fillMaxWidth()) { Compact(article, full, onCoverage) }
        } else {
            val candidates: List<@Composable () -> Unit> = if (short != full) listOf(
                { Hero(article, full, wide, onCoverage) },
                { Hero(article, short, wide, onCoverage) },
                { Compact(article, full, onCoverage) },
                { Compact(article, short, onCoverage) },
                { TextOnly(article, short, onCoverage) },
            ) else listOf(
                { Hero(article, full, wide, onCoverage) },
                { Compact(article, full, onCoverage) },
                { TextOnly(article, full, onCoverage) },
            )
            FirstThatFits(contentModifier.weight(1f).fillMaxWidth().clipToBounds(), candidates)
        }
        Footer(article, isSaved, onRead, onCoverage, { store.toggleSaved(article) }, ::copyLink, ::mute)
        Spacer(Modifier.height(bottomInset))
    }
}

@Composable
private fun Hero(article: Article, summary: String, wide: Boolean, onCoverage: () -> Unit) {
    val minHero = with(LocalDensity.current) { Theme.minHeroHeight.roundToPx() }
    val policy = remember(minHero) { HeroPolicy(minHero, Theme.HERO_ASPECT) }
    Layout(content = {
        Box(Modifier.clip(RoundedCornerShape(bottomStart = Theme.imageCorner, bottomEnd = Theme.imageCorner))) {
            StoryImage(article, article.cardImage(wide), Modifier.fillMaxSize())
            Box(Modifier.fillMaxSize().background(Theme.scrim))
            SourceLabel(article.source, Color.White, shadow = true,
                modifier = Modifier.align(Alignment.BottomStart).padding(horizontal = Theme.horizontal).padding(bottom = 10.dp))
        }
        TextBlock(article, summary, onCoverage, Modifier.padding(top = Theme.imageToMeta))
    }, measurePolicy = policy)
}

@Composable
private fun Compact(article: Article, summary: String, onCoverage: () -> Unit) {
    Column(Modifier.padding(top = 14.dp)) {
        Row(Modifier.padding(horizontal = Theme.horizontal), verticalAlignment = Alignment.CenterVertically) {
            StoryImage(article, article.thumbImage,
                Modifier.size(Theme.thumbSize).clip(RoundedCornerShape(10.dp)))
            Spacer(Modifier.width(12.dp))
            SourceLabel(article.source, LocalPalette.current.primary)
        }
        TextBlock(article, summary, onCoverage, Modifier.padding(top = 12.dp))
    }
}

/** No picture, and the one layout whose summary may be cut short: a clipped card is worse than an ellipsis. */
@Composable
private fun TextOnly(article: Article, summary: String, onCoverage: () -> Unit) {
    Column(Modifier.fillMaxSize().padding(top = 14.dp)) {
        SourceLabel(article.source, LocalPalette.current.primary, modifier = Modifier.padding(horizontal = Theme.horizontal))
        TextBlock(article, summary, onCoverage, Modifier.padding(top = 10.dp), mayTruncate = true)
    }
}

@Composable
private fun SourceLabel(name: String, color: Color, modifier: Modifier = Modifier, shadow: Boolean = false) {
    Row(modifier, verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(7.dp)) {
        SourceMonogram(name, scales = true)
        Text(name, color = color, maxLines = 1, overflow = TextOverflow.Ellipsis,
            style = Theme.caption.copy(fontWeight = FontWeight.SemiBold,
                shadow = if (shadow) Shadow(Color.Black.copy(alpha = 0.6f), blurRadius = 6f) else null))
    }
}

@Composable
private fun TextBlock(article: Article, summary: String, onCoverage: () -> Unit, modifier: Modifier = Modifier,
                      mayTruncate: Boolean = false) {
    val p = LocalPalette.current
    Column(modifier.fillMaxWidth().padding(horizontal = Theme.horizontal).padding(bottom = 8.dp)) {
        MetaRow(article)
        Text(article.displayTitle, color = p.primary, style = Theme.headline,
            modifier = Modifier.padding(top = Theme.metaToHeadline))
        if (summary.isNotEmpty()) {
            if (mayTruncate) {
                // Whole lines only, as many as the space left holds.
                BoxWithConstraints(Modifier.weight(1f, fill = false).padding(top = Theme.headlineToBody)) {
                    val line = with(LocalDensity.current) { Theme.body.lineHeight.toDp() }
                    val lines = if (constraints.hasBoundedHeight) (maxHeight / line).toInt().coerceAtLeast(1) else Int.MAX_VALUE
                    Text(summary, color = p.bodyText, style = Theme.body, maxLines = lines, overflow = TextOverflow.Ellipsis)
                }
            } else {
                Text(summary, color = p.bodyText, style = Theme.body, modifier = Modifier.padding(top = Theme.headlineToBody))
            }
        }
        CoverageRow(article, onCoverage)
    }
}

@Composable
private fun MetaRow(article: Article) {
    val p = LocalPalette.current
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        when {
            article.isBreaking -> BadgePill("BREAKING", Theme.badgeBreaking, modifier = Modifier.pulseTwice())
            article.isDeveloping -> BadgePill("DEVELOPING", Theme.badgeDeveloping)
            article.isHot -> BadgePill("HOT", Theme.badgeHot, Icons.Filled.LocalFireDepartment)
        }
        article.kindLabel?.let { Text(it, color = p.metaText, style = Theme.badge) }
        Text(RelativeTime.label(article), color = p.metaText, style = Theme.meta, maxLines = 1)
        if (article.isAISummary) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                Icon(Icons.Filled.AutoAwesome, null, tint = p.metaText, modifier = Modifier.size(14.dp))
                Text("AI summary", color = p.metaText, style = Theme.meta, maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
        } else if (article.aiText == false) {
            Text("Summary pending", color = p.metaText, style = Theme.meta, maxLines = 1, overflow = TextOverflow.Ellipsis)
        }
    }
}

/** Two gentle pulses when a BREAKING badge first appears, then still. */
@Composable
private fun Modifier.pulseTwice(): Modifier {
    val scale = remember { Animatable(1f) }
    LaunchedEffect(Unit) {
        repeat(2) {
            scale.animateTo(1.12f, tween(350))
            scale.animateTo(1f, tween(350))
        }
    }
    return scale(scale.value)
}

@Composable
private fun CoverageRow(article: Article, onCoverage: () -> Unit) {
    val p = LocalPalette.current
    val others = article.alsoCoveredBy ?: emptyList()
    if (others.isNotEmpty()) {
        Row(Modifier.padding(top = 2.dp).heightIn(min = 44.dp)
            .clickable(interactionSource = remember { MutableInteractionSource() }, indication = null,
                onClickLabel = "Full coverage", onClick = onCoverage),
            verticalAlignment = Alignment.CenterVertically) {
            val names = (listOf(article.source) + others).take(4)
            Box(Modifier.width((22 + 16 * (names.size - 1)).dp).height(22.dp)) {
                names.forEachIndexed { i, name ->
                    SourceMonogram(name, 22.dp, Modifier.offset(x = (16 * i).dp).border(1.5.dp, p.background, CircleShape))
                }
            }
            Spacer(Modifier.width(8.dp))
            var text = "${article.source} + ${article.coverage - 1} more"
            article.points?.let { text += " · $it pts on HN" }
            Text(text, color = p.metaText, style = Theme.meta.copy(fontWeight = FontWeight.Medium),
                modifier = Modifier.weight(1f, fill = false), maxLines = 2, overflow = TextOverflow.Ellipsis)
            Icon(Icons.AutoMirrored.Filled.KeyboardArrowRight, null, tint = p.metaText, modifier = Modifier.size(18.dp))
        }
    } else if (article.points != null) {
        Text("${article.points} points on Hacker News", color = p.metaText, style = Theme.meta,
            modifier = Modifier.padding(top = Theme.bodyToCoverage))
    }
}

@Composable
private fun Footer(article: Article, isSaved: Boolean, onRead: () -> Unit, onCoverage: () -> Unit, onSave: () -> Unit,
                   onCopy: () -> Unit, onMute: () -> Unit) {
    val p = LocalPalette.current
    val context = LocalContext.current
    val haptics = LocalHapticFeedback.current
    val scope = rememberCoroutineScope()
    var preparingShare by remember { mutableStateOf(false) }
    var menu by remember { mutableStateOf(false) }
    Column(Modifier.background(p.background)) {
        Box(Modifier.fillMaxWidth().height(1.dp).background(p.hairline))
        Row(Modifier.fillMaxWidth().defaultMinSize(minHeight = Theme.footerHeight)
            .padding(start = Theme.horizontal, end = Theme.horizontal - 10.dp),
            verticalAlignment = Alignment.CenterVertically) {
            Row(Modifier.weight(1f).heightIn(min = 44.dp).clickable(
                interactionSource = remember { MutableInteractionSource() }, indication = null,
                onClickLabel = "Read more at ${article.source}", onClick = onRead),
                verticalAlignment = Alignment.CenterVertically) {
                Text("Read more at ${article.source}", color = p.accent, style = Theme.bodySemibold, maxLines = 1,
                    overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f, fill = false))
                Icon(Icons.AutoMirrored.Filled.KeyboardArrowRight, null, tint = p.accent, modifier = Modifier.size(20.dp))
            }
            Spacer(Modifier.width(8.dp))
            FooterButton(if (isSaved) "Remove from saved" else "Save story", onClick = {
                if (!isSaved) haptics.performHapticFeedback(HapticFeedbackType.LongPress)
                onSave()
            }) {
                Icon(if (isSaved) Icons.Filled.Bookmark else Icons.Outlined.BookmarkBorder, null, tint = p.accent)
            }
            FooterButton("Share", enabled = !preparingShare, onClick = {
                preparingShare = true
                scope.launch {
                    StoryShare.share(context, article, article.displaySummary)
                    delay(300)
                    preparingShare = false
                }
            }) {
                if (preparingShare) CircularProgressIndicator(Modifier.size(20.dp), color = p.accent, strokeWidth = 2.dp)
                else Icon(Icons.Outlined.Share, null, tint = p.accent)
            }
            Box {
                FooterButton("More", onClick = { menu = true }) { Icon(Icons.Filled.MoreHoriz, null, tint = p.accent) }
                DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
                    if (article.coverage >= 2) {
                        DropdownMenuItem(text = { Text("Full coverage") }, onClick = { menu = false; onCoverage() })
                    }
                    DropdownMenuItem(text = { Text("Copy link") }, onClick = { menu = false; onCopy() })
                    DropdownMenuItem(text = { Text("Mute ${article.source}", color = p.destructive) },
                        onClick = { menu = false; onMute() })
                }
            }
        }
    }
}

@Composable
private fun FooterButton(label: String, enabled: Boolean = true, onClick: () -> Unit, content: @Composable () -> Unit) {
    Box(Modifier.size(44.dp).clip(CircleShape)
        .clickable(enabled = enabled, onClickLabel = label, role = Role.Button, onClick = onClick)
        .clearAndSetSemantics { contentDescription = label; role = Role.Button; onClick { onClick(); true } },
        contentAlignment = Alignment.Center) { content() }
}

/** What a screen reader says for the whole card, in reading order. */
fun spokenLabel(article: Article): String {
    val parts = ArrayList<String>()
    if (article.isBreaking) parts.add("Breaking") else if (article.isDeveloping) parts.add("Developing")
    article.kindLabel?.let { parts.add(it.lowercase().replaceFirstChar(Char::uppercase)) }
    parts.add(article.displayTitle + ".")
    if (article.displaySummary.isNotEmpty()) parts.add(article.displaySummary)
    var source = article.source
    val others = article.coverage - 1
    if (others > 0) source += " and $others other ${if (others == 1) "outlet" else "outlets"}"
    parts.add(source + ", " + RelativeTime.spokenLabel(article) + ".")
    if (article.isAISummary) parts.add("Summarized by AI.")
    return parts.joinToString(" ")
}
