package dev.bookq.reader

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.launch

/**
 * Deep-link landing for `bookq://auth?code=…&state=…` — the Custom-Tab
 * fallback path (Auth Tab returns its result in-process instead).
 * `singleTask` so the URI forwards to the same task as MainActivity;
 * finishing reveals it again.
 */
class AuthCallbackActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        handle(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handle(intent)
    }

    private fun handle(intent: Intent?) {
        val uri = intent?.data
        if (uri == null) {
            finish()
            return
        }
        val graph = (application as BookqApp).graph
        lifecycleScope.launch {
            graph.completeLogin(uri.toString())
            finish()
        }
    }
}
