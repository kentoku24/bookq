package dev.bookq.reader

import dev.bookq.core.api.VersionResponse
import dev.bookq.data.ApiException
import dev.bookq.data.AuthManager
import dev.bookq.data.VersionProbe
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import okhttp3.HttpUrl

/** Per-(host, account) scoping for repositories (added by later milestones). */
data class Scope(val host: String, val email: String?)

/** Top-level app state machine: configure server → authenticate → browse. */
sealed interface AppPhase {
    data object Onboarding : AppPhase   // no server URL yet
    data object Booting : AppPhase      // checking /api/v1/version
    data object Login : AppPhase        // server demands auth, no valid token
    data object Ready : AppPhase        // authenticated (or TEST_MODE server)
    data class BootError(val message: String) : AppPhase
}

class AppModel<T : VersionProbe>(
    val settings: AppSettings,
    val auth: AuthManager,
    private val apiFactory: (HttpUrl) -> T,
    private val scope: CoroutineScope,
    /** M2: does a host-scoped tree snapshot exist for a failed boot's
     *  offline fallback? Provided by AppGraph (file-backed). */
    private val snapshotProbe: (HttpUrl) -> Boolean = { false },
) {
    var api: T? = null
        private set

    private val _phase = MutableStateFlow<AppPhase>(AppPhase.Onboarding)
    val phase = _phase.asStateFlow()

    private val _scopeValue = MutableStateFlow<Scope?>(null)
    val scopeValue = _scopeValue.asStateFlow()

    private val _serverVersion = MutableStateFlow<VersionResponse?>(null)
    val serverVersion = _serverVersion.asStateFlow()

    /** Offline tree snapshot fallback for failed boots. A saved
     *  library is user data — only surface it to a restored session,
     *  or a logged-out launch would paint another user's folders. */
    fun hasOfflineSnapshot(): Boolean =
        auth.user.value != null &&
            settings.baseUrl()?.let(snapshotProbe) == true

    fun start() {
        // Definitive session death (refresh rejected by the server) —
        // only tears down a Ready session, never mid-boot states.
        scope.launch {
            auth.sessionExpired.collect {
                if (_phase.value == AppPhase.Ready) _phase.value = AppPhase.Login
            }
        }
        scope.launch {
            auth.restore()
            PerfTunables.mark("auth.restored")
            settings.awaitLoaded()
            PerfTunables.mark("settings.loaded")
            configureApi()
            if (api != null) boot() else _phase.value = AppPhase.Onboarding
        }
    }

    /** (Re)build the API client after a server URL change. */
    fun configureApi() {
        api = settings.baseUrl()?.let(apiFactory)
    }

    /**
     * Probe the server. 401 → login required; 200 → ready; anything else →
     * BootError (with an offline-snapshot escape hatch for M2).
     * [bootGen] invalidates in-flight probes — a late 401 racing
     * `didLogin()` must not push a Ready session back to Login.
     */
    private var bootGen = 0

    fun boot() {
        val probe = api ?: run {
            _phase.value = AppPhase.Onboarding
            return
        }
        val gen = ++bootGen
        PerfTunables.mark("boot snap=${hasOfflineSnapshot()}")
        // Snapshot-first (SW NetworkFirst parity): a host-scoped tree
        // snapshot paints the library instantly; the version probe then
        // revalidates in the background — 401 still flips to Login.
        if (hasOfflineSnapshot()) {
            _scopeValue.value = currentScope()
            _phase.value = AppPhase.Ready
        } else {
            _phase.value = AppPhase.Booting
        }
        scope.launch {
            try {
                val v = probe.version()
                if (gen != bootGen) return@launch
                _serverVersion.value = v
                _scopeValue.value = currentScope()
                _phase.value = AppPhase.Ready
            } catch (e: ApiException.Unauthorized) {
                if (gen != bootGen) return@launch
                _phase.value = AppPhase.Login
            } catch (e: Exception) {
                if (gen != bootGen) return@launch
                if (hasOfflineSnapshot()) {
                    // Offline fallback still needs a scope or browse spins forever.
                    _scopeValue.value = currentScope()
                    _phase.value = AppPhase.Ready
                } else {
                    _phase.value = AppPhase.BootError(e.message ?: "接続できませんでした")
                }
            }
        }
    }

    /** Server URL was saved → drop old-scope state, rebuild, probe. */
    suspend fun serverChanged() {
        bootGen++
        auth.logout()
        _serverVersion.value = null
        _scopeValue.value = null
        configureApi()
        boot()
    }

    suspend fun setServerAndBoot(url: String) {
        // Same URL → re-probe only. A teardown would kill the live
        // ServerScope and log the user out for nothing (also the
        // benchmark `--es serverURL` path re-saving its persisted URL).
        if (ServerUrls.parse(url) == settings.baseUrl()) {
            boot()
            return
        }
        settings.setServerUrl(url)
        serverChanged()
    }

    /** BootError → "サーバー変更" — send the user back to URL entry. */
    fun showOnboarding() {
        _phase.value = AppPhase.Onboarding
    }

    fun didLogin() {
        // Invalidate a boot still in flight (e.g. the token-less probe
        // racing a browser round-trip after process death).
        val gen = ++bootGen
        _scopeValue.value = currentScope()
        _phase.value = AppPhase.Ready
        // /api/v1/version doubles as the boot auth probe, so it 401'd
        // pre-login — quietly refetch for the settings version row.
        val probe = api ?: return
        scope.launch {
            try {
                val v = probe.version()
                if (gen == bootGen) _serverVersion.value = v
            } catch (_: Exception) { /* version row is advisory */ }
        }
    }

    suspend fun logout() {
        bootGen++
        auth.logout()
        _serverVersion.value = null
        _scopeValue.value = null
        _phase.value = AppPhase.Login
    }

    private fun currentScope(): Scope =
        Scope(host = settings.baseUrl()?.host ?: "", email = auth.user.value?.email)
}
