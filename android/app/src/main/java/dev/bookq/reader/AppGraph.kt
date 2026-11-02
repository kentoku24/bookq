package dev.bookq.reader

import android.app.Application
import dev.bookq.data.AuthManager
import dev.bookq.data.BookqApi
import dev.bookq.data.BookqHttp
import dev.bookq.data.FilePendingLoginStore
import dev.bookq.data.KeystoreTokenStore
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import okhttp3.HttpUrl
import okhttp3.OkHttpClient

/** Hand-written DI container — rebuilt pieces hang off the server scope. */
class AppGraph(app: Application) {

    val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)

    /** One base client — derived clients share its pool/dispatcher via
     *  `newBuilder()`, so server switches never strand idle pools. */
    private val sharedClient = OkHttpClient()

    val settings: AppSettings = AppConfig(app, scope)

    val auth = AuthManager(
        store = KeystoreTokenStore(app),
        pendingStore = FilePendingLoginStore(app),
        exchangeClient = BookqHttp.plainClient(base = sharedClient),
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
        snapshotProbe = { base -> ServerScope.hasTreeSnapshot(app, base.host) },
    )

    /**
     * Active per-(host, account) service bundle — rebuilt whenever
     * [AppModel.scopeValue] changes (server URL or login identity).
     * Collected by the UI via [serverScope].
     */
    private val _serverScope = MutableStateFlow<ServerScope?>(null)
    val serverScope = _serverScope.asStateFlow()

    init {
        scope.launch {
            model.scopeValue.collect { sc ->
                val old = _serverScope.value
                if (sc == null || sc.host.isEmpty()) {
                    _serverScope.value = null
                    old?.close()
                    return@collect
                }
                val api = model.api ?: return@collect
                if (old != null && old.api === api) return@collect
                old?.close()
                _serverScope.value = ServerScope(
                    context = app,
                    api = api,
                    host = sc.host,
                    email = sc.email,
                    mediaClient = buildMediaClient(api.baseUrl),
                    parent = scope,
                ).also { it.start() }
            }
        }
    }

    private fun buildMediaClient(base: HttpUrl): okhttp3.OkHttpClient =
        BookqHttp.apiClient(
            baseUrl = base,
            tokenProvider = { auth.token },
            refresher = { runBlocking { auth.refreshAccessToken() } },
        )

    /** Latest login failure surfaced to the UI (Snackbar). */
    private val _loginError = MutableStateFlow<String?>(null)
    val loginError = _loginError.asStateFlow()

    fun consumeLoginError() {
        _loginError.value = null
    }

    private fun buildApi(base: HttpUrl): BookqApi =
        BookqApi(base, buildMediaClient(base))

    /** Unauthenticated health probe for onboarding. */
    suspend fun healthCheck(url: String): Boolean {
        val base = ServerUrls.parse(url) ?: return false
        return try {
            BookqApi(base, BookqHttp.plainClient(base = sharedClient)).health()
            true
        } catch (e: Exception) {
            false
        }
    }

    /**
     * Shared by the Auth Tab result path and the bookq://auth activity.
     * Runs on the app scope — Activity/Composition churn (rotation while
     * the exchange is in flight) must not cancel it, and a cancelled
     * attempt must not surface as a login error.
     */
    fun completeLogin(callbackUri: String) {
        scope.launch {
            try {
                auth.completeLogin(callbackUri)
                model.didLogin()
            } catch (e: AuthManager.AuthError) {
                _loginError.value = e.message
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                _loginError.value = e.message ?: "ログインに失敗しました"
            }
        }
    }
}
