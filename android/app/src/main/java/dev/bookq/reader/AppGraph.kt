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
import kotlinx.coroutines.async
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import okhttp3.HttpUrl
import okhttp3.OkHttpClient

/** Hand-written DI container — rebuilt pieces hang off the server scope. */
class AppGraph(app: Application) {

    val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)

    /** One base client — derived clients share its pool/dispatcher via
     *  `newBuilder()`, so server switches never strand idle pools. */
    private val sharedClient = OkHttpClient()

    val settings: AppSettings = AppConfig(app, scope)

    /** Connectivity watch → the amber offline banner (web ●online parity). */
    val online = dev.bookq.data.offline.OnlineStatus(app).also { it.start() }

    /** Presentation-capable displays + the tv toggle (issue #301) —
     *  app-scoped like iOS `ExternalDisplayStore.shared`: attach is
     *  independent of which server is mounted. */
    val extDisplays = dev.bookq.reader.ext.ExternalDisplayController(app)
        .also { it.start() }

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

    /** Read+decode the newest tree snapshot while settings still load —
     *  moves the multi-MB JSON parse off the cold-start critical path.
     *  ServerScope applies it only when the backing file matches its
     *  host (a stale other-host candidate is just wasted warmth). */
    private val treePrewarm =
        scope.async(Dispatchers.IO) {
            val dir = java.io.File(app.filesDir, "state")
            val file = dir.listFiles { f ->
                f.isFile && f.name.startsWith("tree-") && f.name.endsWith(".json")
            }?.maxByOrNull { it.lastModified() } ?: return@async null
            val text = dev.bookq.data.ScopedFiles.readOrNull(file)
                ?: return@async null
            val snap = runCatching {
                dev.bookq.core.api.BookqJson.decodeFromString<
                    dev.bookq.data.TreeRepository.TreeSnapshot>(text)
            }.getOrNull() ?: return@async null
            dev.bookq.data.TreeRepository.PrewarmedSnapshot(file.name, snap)
        }

    /**
     * Active per-(host, account) service bundle — rebuilt whenever
     * [AppModel.scopeValue] changes (server URL or login identity).
     * Collected by the UI via [serverScope].
     */
    private val _serverScope = MutableStateFlow<ServerScope?>(null)
    val serverScope = _serverScope.asStateFlow()

    /** Identity of the live [ServerScope] — a same-server re-login with a
     *  different account MUST rebuild it (hidden roots + progress files
     *  are account-keyed). The OkHttp instance is not identity. */
    private var serverScopeKey: Scope? = null

    init {
        scope.launch {
            model.scopeValue.collect { sc ->
                val old = _serverScope.value
                if (sc == null || sc.host.isEmpty()) {
                    _serverScope.value = null
                    serverScopeKey = null
                    old?.close()
                    return@collect
                }
                val api = model.api ?: return@collect
                if (old != null && serverScopeKey == sc) return@collect
                old?.close()
                // Construct + start on IO: the repos' init blocks and
                // restoreSnapshot read+decode multi-MB files — a main-
                // thread cold start would jank (StrictMode DiskRead).
                val created = withContext(Dispatchers.IO) {
                    PerfTunables.mark("scope.build.begin")
                    ServerScope(
                        context = app,
                        api = api,
                        host = sc.host,
                        email = sc.email,
                        mediaClient = buildMediaClient(api.baseUrl),
                        tokenProvider = { auth.token },
                        parent = scope,
                        treePrewarm = treePrewarm,
                    ).also { it.warmShallow() }
                }
                serverScopeKey = sc
                _serverScope.value = created
                PerfTunables.mark("scope.emitted")
                // Full snapshot restore + network kickoff continue off
                // the publish path — the grid already has the shallow
                // snapshot to paint. Dies with the scope on close().
                created.ioScope.launch { created.start() }
            }
        }
    }

    private fun buildMediaClient(base: HttpUrl): okhttp3.OkHttpClient =
        BookqHttp.apiClient(
            baseUrl = base,
            tokenProvider = { auth.token },
            refresher = { runBlocking { auth.refreshAccessToken() } },
            base = sharedClient,
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
        // Scope identity at exchange start — a logout or server/account
        // switch lands on scopeValue synchronously, so comparing after
        // the exchange catches a stale login even if the StateFlow
        // emission hasn't been collected yet.
        val before = model.scopeValue.value
        scope.launch {
            try {
                auth.completeLogin(callbackUri)
                if (model.scopeValue.value != before) {
                    // The persisted pair belongs to a dead session —
                    // wipe it rather than reviving that login.
                    auth.logout()
                    return@launch
                }
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
