package com.pranjald.techgist

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import com.pranjald.techgist.notify.StoryNotification
import com.pranjald.techgist.ui.ContentScreen
import com.pranjald.techgist.ui.TechgistTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge()
        super.onCreate(savedInstanceState)
        val graph = TechgistApp.graph(this)
        // An alert that launched the app still opens its story (but not again when the screen is rebuilt).
        if (savedInstanceState == null) handle(intent)
        setContent { TechgistTheme { ContentScreen(graph) } }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handle(intent)
    }

    /** A tapped alert or its Read button. A server push names its story by `eventId`, by `url`, or both. */
    private fun handle(intent: Intent?) {
        intent ?: return
        val url = intent.getStringExtra(StoryNotification.EXTRA_URL)?.takeIf { it.startsWith("http") }
        val eventId = intent.getStringExtra(StoryNotification.EXTRA_EVENT)?.takeIf { it.isNotEmpty() }
        val graph = TechgistApp.graph(this)
        when (intent.action) {
            StoryNotification.ACTION_READ -> if (url != null) graph.pendingRequest = AlertRequest.Read(url)
            StoryNotification.ACTION_SHOW -> if (url != null || eventId != null) graph.pendingRequest = AlertRequest.Show(url, eventId)
            else -> return
        }
        StoryNotification.cancel(this, intent.getIntExtra(StoryNotification.EXTRA_NOTIFICATION_ID, 0))
    }
}
