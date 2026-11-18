package com.pranjald.techgist.ui

import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Path
import android.graphics.Rect
import android.graphics.RectF
import android.graphics.Shader
import android.graphics.Typeface
import android.graphics.drawable.BitmapDrawable
import android.net.Uri
import android.text.Layout
import android.text.StaticLayout
import android.text.TextPaint
import androidx.browser.customtabs.CustomTabColorSchemeParams
import androidx.browser.customtabs.CustomTabsIntent
import androidx.core.content.FileProvider
import coil.imageLoader
import coil.request.ImageRequest
import com.pranjald.techgist.R
import com.pranjald.techgist.model.Article
import com.pranjald.techgist.model.Category
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File

/** The full story in a Chrome Custom Tab: the reader's own browser, inside the app, with its logins and blockers. */
object Reader {
    fun open(context: Context, url: String) {
        if (!(url.startsWith("https://") || url.startsWith("http://"))) return
        val uri = Uri.parse(url)
        runCatching {
            CustomTabsIntent.Builder()
                .setShowTitle(true)
                .setShareState(CustomTabsIntent.SHARE_STATE_ON)
                .setDefaultColorSchemeParams(CustomTabColorSchemeParams.Builder().setToolbarColor(0xFFFFFFFF.toInt()).build())
                .setColorSchemeParams(CustomTabsIntent.COLOR_SCHEME_DARK,
                    CustomTabColorSchemeParams.Builder().setToolbarColor(0xFF000000.toInt()).build())
                .build().launchUrl(context, uri)
        }.onFailure {
            runCatching { context.startActivity(Intent(Intent.ACTION_VIEW, uri).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
        }
    }
}

/** Shares a story as a picture of its card with a Techgist footer, plus a short line linking to the app. */
object StoryShare {
    const val APP_URL = "https://play.google.com/store/apps/details?id=com.pranjald.techgist"
    private const val ACCENT = 0xFF215CED.toInt()

    fun text(article: Article): String =
        "${article.displayTitle}\n\nTech news in 60 words a story, on Techgist: $APP_URL"

    suspend fun share(context: Context, article: Article, summary: String) {
        val uri = runCatching {
            val photo = article.cardImage(wide = true)?.let { url ->
                val request = ImageRequest.Builder(context).data(url).size(1290).allowHardware(false).build()
                (context.imageLoader.execute(request).drawable as? BitmapDrawable)?.bitmap
            }
            withContext(Dispatchers.Default) {
                val logo = BitmapFactory.decodeResource(context.resources, R.drawable.logo)
                val card = render(article, summary, photo, logo)
                val dir = File(context.cacheDir, "share").apply { mkdirs() }
                dir.listFiles()?.forEach { it.delete() }
                val file = File(dir, "techgist-${System.currentTimeMillis()}.png")
                file.outputStream().use { card.compress(Bitmap.CompressFormat.PNG, 100, it) }
                FileProvider.getUriForFile(context, context.packageName + ".share", file)
            }
        }.getOrNull()
        val send = Intent(Intent.ACTION_SEND).putExtra(Intent.EXTRA_TEXT, text(article))
        if (uri != null) {
            send.setType("image/png").putExtra(Intent.EXTRA_STREAM, uri).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            send.clipData = android.content.ClipData.newRawUri(null, uri)
        } else {
            send.setType("text/plain")
        }
        runCatching { context.startActivity(Intent.createChooser(send, null).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
    }

    private fun paint(size: Float, color: Int, typeface: Typeface, s: Float) = TextPaint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = size * s; this.color = color; this.typeface = typeface
    }

    private fun block(text: String, paint: TextPaint, width: Int, spacing: Float = 1.15f): StaticLayout =
        StaticLayout.Builder.obtain(text, 0, text.length, paint, width)
            .setAlignment(Layout.Alignment.ALIGN_NORMAL).setLineSpacing(0f, spacing).setIncludePad(false).build()

    /** The shared picture: the story card as it looks in the app (always light), with a Techgist footer. */
    fun render(article: Article, summary: String, photo: Bitmap?, logo: Bitmap?, s: Float = 3f): Bitmap {
        val width = (390 * s).toInt()
        val photoHeight = (230 * s).toInt()
        val pad = (20 * s).toInt()
        val gap = (10 * s).toInt()
        val textWidth = width - 2 * pad
        val bold = Typeface.create(Typeface.SANS_SERIF, Typeface.BOLD)
        val source = block(article.source.uppercase(), paint(12f, ACCENT, bold, s), textWidth)
        val title = block(article.displayTitle, paint(20f, 0xFF000000.toInt(), Typeface.create(Typeface.SERIF, Typeface.BOLD), s), textWidth)
        val body = if (summary.isEmpty()) null
            else block(summary, paint(17f, 0xCC000000.toInt(), Typeface.SANS_SERIF, s), textWidth, 1.25f)
        val footerHeight = (54 * s).toInt()
        val textHeight = pad + source.height + gap + title.height + (body?.let { gap + it.height } ?: 0) + pad
        val bitmap = Bitmap.createBitmap(width, photoHeight + textHeight + footerHeight, Bitmap.Config.ARGB_8888)
        val c = Canvas(bitmap)
        c.drawColor(0xFFFFFFFF.toInt())

        val top = Rect(0, 0, width, photoHeight)
        if (photo != null) {
            // Fill the frame, cropping the longer side.
            val scale = maxOf(width / photo.width.toFloat(), photoHeight / photo.height.toFloat())
            val w = (width / scale).toInt()
            val h = (photoHeight / scale).toInt()
            val x = (photo.width - w) / 2
            val y = (photo.height - h) / 2
            c.drawBitmap(photo, Rect(x, y, x + w, y + h), top, Paint(Paint.FILTER_BITMAP_FLAG))
        } else {
            val (a, b) = coverColors(article.category)
            c.drawRect(top, Paint().apply {
                shader = LinearGradient(0f, 0f, width.toFloat(), photoHeight.toFloat(), a, b, Shader.TileMode.CLAMP)
            })
        }

        var y = photoHeight + pad
        for (layout in listOfNotNull(source, title, body)) {
            c.save(); c.translate(pad.toFloat(), y.toFloat()); layout.draw(c); c.restore()
            y += layout.height + gap
        }

        val footerTop = (photoHeight + textHeight).toFloat()
        c.drawRect(0f, footerTop, width.toFloat(), footerTop + footerHeight, Paint().apply { color = ACCENT })
        val white = 0xFFFFFFFF.toInt()
        val left = 16 * s
        val logoSize = 30 * s
        val logoTop = footerTop + (footerHeight - logoSize) / 2
        if (logo != null) {
            val box = RectF(left, logoTop, left + logoSize, logoTop + logoSize)
            c.save()
            c.clipPath(Path().apply { addRoundRect(box, 7 * s, 7 * s, Path.Direction.CW) })
            c.drawBitmap(logo, null, box, Paint(Paint.FILTER_BITMAP_FLAG))
            c.restore()
        }
        val name = paint(15f, white, bold, s)
        val small = paint(12f, white, Typeface.SANS_SERIF, s)
        val textLeft = left + logoSize + 10 * s
        c.drawText("Techgist", textLeft, footerTop + 24 * s, name)
        c.drawText("Tech news in 60 words", textLeft, footerTop + 40 * s, small)
        val right = width - 16 * s
        val tiny = paint(11f, 0xD9FFFFFF.toInt(), Typeface.SANS_SERIF, s).apply { textAlign = Paint.Align.RIGHT }
        c.drawText("Read it on", right, footerTop + 22 * s, tiny)
        c.drawText("Techgist", right, footerTop + 40 * s, name.apply { textAlign = Paint.Align.RIGHT })
        return bitmap
    }

    private fun coverColors(category: Category): Pair<Int, Int> = when (category) {
        Category.TOP -> 0xFF2454D9.toInt() to 0xFF5C33C7.toInt()
        Category.AI -> 0xFF732BCC.toInt() to 0xFFC73880.toInt()
        Category.STARTUPS -> 0xFFD15924.toInt() to 0xFFC22954.toInt()
        Category.DEV -> 0xFF0A7870.toInt() to 0xFF17479E.toInt()
        Category.BLOGS -> 0xFFC27514.toInt() to 0xFFAD3D29.toInt()
    }
}
