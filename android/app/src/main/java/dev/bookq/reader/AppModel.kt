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

    /** Offline tree snapshot fallback for failed boots. */
    fun hasOfflineSnapshot(): Boolean =
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
            settings.awaitLoaded()
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
     */
    fun boot() {
        val probe = api ?: run {
            _phase.value = AppPhase.Onboarding
            return
        }
        _phase.value = AppPhase.Booting
        scope.launch {
            try {
                _serverVersion.value = probe.version()
                _scopeValue.value = currentScope()
                _phase.value = AppPhase.Ready
            } catch (e: ApiException.Unauthorized) {
                _phase.value = AppPhase.Login
            } catch (e: Exception) {
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
    fun serverChanged() {
        auth.logout()
        _serverVersion.value = null
        configureApi()
        boot()
    }

    suspend fun setServerAndBoot(url: String) {
        settings.setServerUrl(url)
        serverChanged()
    }

    /** BootError → "サーバー変更" — send the user back to URL entry. */
    fun showOnboarding() {
        _phase.value = AppPhase.Onboarding
    }

    fun didLogin() {
        _scopeValue.value = currentScope()
        _phase.value = AppPhase.Ready
    }

    fun logout() {
        auth.logout()
        _phase.value = AppPhase.Login
    }

    private fun currentScope(): Scope =
        Scope(host = settings.baseUrl()?.host ?: "", email = auth.user.value?.email)
}
