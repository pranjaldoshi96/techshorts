package com.pranjald.techgist.ui

import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.AutoAwesome
import androidx.compose.material.icons.filled.Bolt
import androidx.compose.material.icons.filled.Code
import androidx.compose.material.icons.filled.FormatQuote
import androidx.compose.material.icons.filled.Send
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.getValue
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.clipToBounds
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.layout.Layout
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.pranjald.techgist.model.Article
import com.pranjald.techgist.model.Category

/** The app's colours in one appearance. The values are the iOS app's (its asset catalog and system colours). */
data class Palette(
    val dark: Boolean,
    val accent: Color,
    /** Text on the accent colour: white on the light-mode blue, black on the lighter dark-mode blue. */
    val onAccent: Color,
    val background: Color,
    val secondaryBackground: Color,
    val tertiaryFill: Color,
    val groupedBackground: Color,
    val cell: Color,
    val primary: Color,
    val bodyText: Color,
    val metaText: Color,
    val hairline: Color,
    val destructive: Color,
)

private val Light = Palette(
    dark = false, accent = Color(0xFF215CED), onAccent = Color.White, background = Color.White,
    secondaryBackground = Color(0xFFF2F2F7), tertiaryFill = Color(0x1F767680), groupedBackground = Color(0xFFF2F2F7),
    cell = Color.White, primary = Color.Black, bodyText = Color.Black.copy(alpha = 0.85f),
    metaText = Color.Black.copy(alpha = 0.62f), hairline = Color.Black.copy(alpha = 0.08f), destructive = Color(0xFFD70015),
)

private val Dark = Palette(
    dark = true, accent = Color(0xFF6C97FF), onAccent = Color.Black, background = Color.Black,
    secondaryBackground = Color(0xFF1C1C1E), tertiaryFill = Color(0x3D767680), groupedBackground = Color.Black,
    cell = Color(0xFF1C1C1E), primary = Color.White, bodyText = Color.White.copy(alpha = 0.85f),
    metaText = Color.White.copy(alpha = 0.62f), hairline = Color.White.copy(alpha = 0.10f), destructive = Color(0xFFFF6961),
)

val LocalPalette = staticCompositionLocalOf { Light }

/** Design tokens (the iOS app's Theme). Sizes are in sp, so everything scales with the system font size. */
object Theme {
    val horizontal = 20.dp
    val imageToMeta = 14.dp
    val metaToHeadline = 8.dp
    val headlineToBody = 10.dp
    val bodyToCoverage = 12.dp
    val footerHeight = 48.dp
    val imageCorner = 12.dp
    /** The smallest hero picture worth showing; below this the card switches to a thumbnail. */
    val minHeroHeight = 170.dp
    /** Hero height is at most this share of the card's width (16:10). */
    const val HERO_ASPECT = 0.625f
    val thumbSize = 72.dp

    val headline = TextStyle(fontFamily = FontFamily.Serif, fontWeight = FontWeight.Bold, fontSize = 22.sp, lineHeight = 29.sp)
    val body = TextStyle(fontSize = 17.sp, lineHeight = 26.sp)
    val meta = TextStyle(fontSize = 13.sp, lineHeight = 18.sp)
    val badge = TextStyle(fontSize = 11.sp, lineHeight = 13.sp, fontWeight = FontWeight.Black, letterSpacing = 0.6.sp)
    val caption = TextStyle(fontSize = 12.sp, lineHeight = 16.sp)
    val subheadline = TextStyle(fontSize = 15.sp, lineHeight = 20.sp)
    val bodySemibold = TextStyle(fontSize = 17.sp, lineHeight = 22.sp, fontWeight = FontWeight.SemiBold)
    val rowTitle = TextStyle(fontFamily = FontFamily.Serif, fontWeight = FontWeight.SemiBold, fontSize = 17.sp, lineHeight = 22.sp)
    val screenTitle = TextStyle(fontSize = 17.sp, fontWeight = FontWeight.SemiBold)

    // Badge fills are dark enough for white text on them to pass AA.
    val badgeBreaking = Color(0xFFCC1C1C)
    val badgeDeveloping = Color(0xFFB35400)
    val badgeHot = Color(0xFFCC381A)

    /** Darkens the bottom of a picture so the white source label stays readable on any photo. */
    val scrim = Brush.verticalGradient(0.62f to Color.Transparent, 1f to Color.Black.copy(alpha = 0.62f))
}

@Composable
fun TechgistTheme(content: @Composable () -> Unit) {
    val p = if (isSystemInDarkTheme()) Dark else Light
    val scheme = if (p.dark) {
        darkColorScheme(primary = p.accent, onPrimary = p.onAccent, background = p.background, surface = p.background,
            onBackground = p.primary, onSurface = p.primary, surfaceVariant = p.secondaryBackground,
            onSurfaceVariant = p.metaText, surfaceContainer = p.cell, surfaceContainerHigh = p.cell,
            surfaceContainerHighest = p.cell, surfaceContainerLow = p.cell, error = p.destructive,
            secondaryContainer = p.secondaryBackground, onSecondaryContainer = p.primary, outline = p.metaText)
    } else {
        lightColorScheme(primary = p.accent, onPrimary = p.onAccent, background = p.background, surface = p.background,
            onBackground = p.primary, onSurface = p.primary, surfaceVariant = p.secondaryBackground,
            onSurfaceVariant = p.metaText, surfaceContainer = p.cell, surfaceContainerHigh = p.cell,
            surfaceContainerHighest = p.secondaryBackground, surfaceContainerLow = p.cell, error = p.destructive,
            secondaryContainer = p.secondaryBackground, onSecondaryContainer = p.primary, outline = p.metaText)
    }
    CompositionLocalProvider(LocalPalette provides p) {
        MaterialTheme(colorScheme = scheme, content = content)
    }
}

/** A soft sweep of light across a placeholder while content loads. */
@Composable
fun Modifier.shimmer(): Modifier {
    val transition = rememberInfiniteTransition(label = "shimmer")
    val phase by transition.animateFloat(-1f, 1f, infiniteRepeatable(tween(1300, easing = LinearEasing), RepeatMode.Restart),
        label = "phase")
    return clipToBounds().drawWithContent {
        drawContent()
        val w = size.width * 0.6f
        val x = phase * size.width * 1.3f
        drawRect(Brush.horizontalGradient(listOf(Color.Transparent, Color.White.copy(alpha = 0.28f), Color.Transparent),
            startX = x, endX = x + w), topLeft = Offset(x, 0f), size = androidx.compose.ui.geometry.Size(w, size.height))
    }
}

private val monogramPalette = listOf(
    Color(0xFF1A4AB8), Color(0xFF7A2BAB), Color(0xFFA82447), Color(0xFF944208),
    Color(0xFF086159), Color(0xFF3B475C), Color(0xFF125E29), Color(0xFF851F7A),
)

/** The colour for a publisher's monogram: always the same for that name, and the same as on iOS. */
fun monogramColor(name: String): Color {
    var sum = 7
    var i = 0
    while (i < name.length) {
        val cp = name.codePointAt(i)
        sum = sum * 31 + cp
        i += Character.charCount(cp)
    }
    return monogramPalette[(sum.toUInt() % monogramPalette.size.toUInt()).toInt()]
}

/** A round stand-in for a publisher's logo: its initial on a colour that is always the same for that name. */
@Composable
fun SourceMonogram(name: String, size: Dp = 20.dp, modifier: Modifier = Modifier, scales: Boolean = false) {
    val initial = (name.firstOrNull { it.isLetterOrDigit() } ?: 'T').uppercase()
    val fontScale = androidx.compose.ui.platform.LocalDensity.current.fontScale
    // Beside text it grows with the text (up to twice); elsewhere it is a picture of a fixed size.
    val side = if (scales) size * fontScale.coerceIn(1f, 2f) else size
    Box(modifier.size(side).clip(CircleShape).background(monogramColor(name)).clearAndSetSemantics { },
        contentAlignment = Alignment.Center) {
        Text(initial, color = Color.White, fontWeight = FontWeight.Bold,
            style = TextStyle(fontSize = (side.value * 0.55f / fontScale).sp, lineHeight = (side.value * 0.7f / fontScale).sp),
            textAlign = TextAlign.Center)
    }
}

/** The main button on a screen: the accent colour with text that passes AA on it in both appearances. */
@Composable
fun PrimaryButton(text: String, modifier: Modifier = Modifier, onClick: () -> Unit) {
    val p = LocalPalette.current
    Box(modifier.clip(CircleShape).background(p.accent).clickable(onClick = onClick)
        .defaultMinSize(minHeight = 44.dp).padding(horizontal = 20.dp, vertical = 12.dp),
        contentAlignment = Alignment.Center) {
        Text(text, color = p.onAccent, style = Theme.bodySemibold, textAlign = TextAlign.Center)
    }
}

/** A small filled capsule: BREAKING, DEVELOPING, HOT. */
@Composable
fun BadgePill(text: String, color: Color, icon: ImageVector? = null, modifier: Modifier = Modifier) {
    Row(modifier.clip(CircleShape).background(color).padding(horizontal = 7.dp, vertical = 3.dp),
        verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(3.dp)) {
        if (icon != null) Icon(icon, null, tint = Color.White, modifier = Modifier.size(11.dp))
        Text(text, color = Color.White, style = Theme.badge)
    }
}

/** A drawn cover for stories without a photo: a gradient in the tab's colours with its symbol. No text. */
@Composable
fun StoryCover(article: Article, modifier: Modifier = Modifier) {
    val (colors, symbol) = when (article.category) {
        Category.TOP -> listOf(Color(0xFF2454D9), Color(0xFF5C33C7)) to Icons.Filled.Bolt
        Category.AI -> listOf(Color(0xFF732BCC), Color(0xFFC73880)) to Icons.Filled.AutoAwesome
        Category.STARTUPS -> listOf(Color(0xFFD15924), Color(0xFFC22954)) to Icons.Filled.Send
        Category.DEV -> listOf(Color(0xFF0A7870), Color(0xFF17479E)) to Icons.Filled.Code
        Category.BLOGS -> listOf(Color(0xFFC27514), Color(0xFFAD3D29)) to Icons.Filled.FormatQuote
    }
    Layout(
        content = { Icon(symbol, null, tint = Color.White.copy(alpha = 0.16f)) },
        modifier = modifier.fillMaxSize().clipToBounds()
            .background(Brush.linearGradient(colors, Offset.Zero, Offset.Infinite)).clearAndSetSemantics { },
    ) { measurables, constraints ->
        val side = minOf(constraints.maxWidth, constraints.maxHeight)
        val icon = (side * 0.72f).toInt()
        val placeable = measurables.first().measure(androidx.compose.ui.unit.Constraints.fixed(icon, icon))
        layout(constraints.maxWidth, constraints.maxHeight) {
            placeable.place(constraints.maxWidth - icon + (side * 0.08f).toInt(), -(side * 0.04f).toInt())
        }
    }
}

/** A centred message for a screen with nothing in it. */
@Composable
fun EmptyState(icon: ImageVector, title: String, description: String, modifier: Modifier = Modifier,
               action: (@Composable () -> Unit)? = null) {
    val p = LocalPalette.current
    Column(modifier.fillMaxSize().padding(32.dp), horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center) {
        Icon(icon, null, tint = p.metaText, modifier = Modifier.size(52.dp))
        Spacer(Modifier.height(14.dp))
        Text(title, color = p.primary, style = TextStyle(fontSize = 22.sp, fontWeight = FontWeight.Bold), textAlign = TextAlign.Center)
        Spacer(Modifier.height(6.dp))
        Text(description, color = p.metaText, style = Theme.subheadline, textAlign = TextAlign.Center,
            modifier = Modifier.widthIn(max = 320.dp))
        if (action != null) {
            Spacer(Modifier.height(18.dp))
            action()
        }
    }
}

@Suppress("unused")
private fun Modifier.nudge(x: Dp, y: Dp) = offset(x, y)
