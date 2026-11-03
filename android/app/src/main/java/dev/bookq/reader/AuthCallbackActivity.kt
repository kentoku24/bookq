package dev.bookq.reader

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity

/**
 * Deep-link landing for `bookq://auth?code=…&state=…` — the Custom-Tab
 * fallback path (Auth Tab returns its result in-process instead).
 * `singleTask` so the URI forwards to the same task as MainActivity.
 * The exchange itself runs on the app scope, so this activity only
 * forwards the URI and then gets out of the way.
 */
class AuthCallbackActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (savedInstanceState == null) {
            handle(intent)
        } else {
            // Recreated (rotation etc.) — the app-scoped exchange already
            // owns this login; firing it again would double-exchange.
            finish()
        }
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
        (application as BookqApp).graph.completeLogin(uri.toString())
        // Fallback path: the Custom Tab sits under this activity in the
        // same task, so a bare finish() would reveal the stale browser
        // page. Pull MainActivity forward over it instead.
        startActivity(
            Intent(this, MainActivity::class.java)
                .addFlags(
                    Intent.FLAG_ACTIVITY_CLEAR_TOP or
                        Intent.FLAG_ACTIVITY_SINGLE_TOP,
                ),
        )
        finish()
    }
}
