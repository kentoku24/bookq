package dev.bookq.reader

import android.app.Application
import dev.bookq.data.AuthManager
import dev.bookq.data.BookqApi
import dev.bookq.data.BookqHttp
import dev.bookq.data.FilePendingLoginStore
import dev.bookq.data.KeystoreTokenStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.runBlocking
import okhttp3.HttpUrl

/** Hand-written DI container — rebuilt pieces hang off the server scope. */
class AppGraph(app: Application) {

    val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)

    val settings: AppSettings = AppConfig(app, scope)

    val auth = AuthManager(
        store = KeystoreTokenStore(app),
        pendingStore = FilePendingLoginStore(app),
        exchangeClient = BookqHttp.plainClient(),
        baseUrlProvider = { settings.baseUrl() },
        // Own IO scope: refresh continuations, keystore crypto and
        // AtomicFile IO must not run on (or wait on) the main thread.
        scope = CoroutineScope(SupervisorJob() + Dispatchers.IO),
    )

    val model = AppModel(
        settings = settings,
        auth = auth,
        apiFactory = ::buildApi,
        scope = scope,
    )

    /** Latest login failure surfaced to the UI (Snackbar). */
    private val _loginError = MutableStateFlow<String?>(null)
    val loginError = _loginError.asStateFlow()

    fun consumeLoginError() {
        _loginError.value = null
    }

    private fun buildApi(base: HttpUrl): BookqApi {
        val client = BookqHttp.apiClient(
            baseUrl = base,
            tokenProvider = { auth.token },
            refresher = { runBlocking { auth.refreshAccessToken() } },
        )
        return BookqApi(base, client)
    }

    /** Unauthenticated health probe for onboarding. */
    suspend fun healthCheck(url: String): Boolean {
        val base = ServerUrls.parse(url) ?: return false
        return try {
            BookqApi(base, BookqHttp.plainClient()).health()
            true
        } catch (e: Exception) {
            false
        }
    }

    /** Shared by the Auth Tab result path and the bookq://auth activity. */
    suspend fun completeLogin(callbackUri: String) {
        try {
            auth.completeLogin(callbackUri)
            model.didLogin()
        } catch (e: AuthManager.AuthError) {
            _loginError.value = e.message
        } catch (e: Exception) {
            _loginError.value = e.message ?: "ログインに失敗しました"
        }
    }
}
