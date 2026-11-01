package dev.bookq.reader

import dev.bookq.core.api.VersionResponse
import dev.bookq.data.ApiException
import dev.bookq.data.AuthManager
import dev.bookq.data.BookqHttp
import dev.bookq.data.InMemoryPendingLoginStore
import dev.bookq.data.InMemoryTokenStore
import dev.bookq.data.VersionProbe
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runTest
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException

@OptIn(ExperimentalCoroutinesApi::class)
class AppModelTest {

    /** In-memory [AppSettings] — setters flip StateFlows synchronously. */
    private class FakeSettings(
        initialServerUrl: String = "",
    ) : AppSettings {
        private val _serverUrl = MutableStateFlow(initialServerUrl)
        override val serverUrl = _serverUrl.asStateFlow()
        override val originalImages = MutableStateFlow(false).asStateFlow()
        override val browseLayout = MutableStateFlow(BrowseLayout.TILE).asStateFlow()
        override val volumeKeys = MutableStateFlow(false).asStateFlow()
        override val dynamicColor = MutableStateFlow(false).asStateFlow()
        override val perfHud = MutableStateFlow(false).asStateFlow()
        override suspend fun awaitLoaded() {}
        override suspend fun setServerUrl(v: String) { _serverUrl.value = v }
        override suspend fun setOriginalImages(v: Boolean) {}
        override suspend fun setBrowseLayout(v: BrowseLayout) {}
        override suspend fun setVolumeKeys(v: Boolean) {}
        override suspend fun setDynamicColor(v: Boolean) {}
        override suspend fun setPerfHud(v: Boolean) {}
    }

    private class FakeProbe(
        private val behavior: suspend () -> VersionResponse,
    ) : VersionProbe {
        var calls = 0
        override suspend fun version(): VersionResponse {
            calls++
            return behavior()
        }
    }

    private fun auth(scope: CoroutineScope) = AuthManager(
        store = InMemoryTokenStore(),
        pendingStore = InMemoryPendingLoginStore(),
        exchangeClient = BookqHttp.plainClient(),
        baseUrlProvider = { null },
        scope = scope,
    )

    private fun model(
        scope: TestScope,
        settings: FakeSettings,
        probe: FakeProbe,
    ): AppModel<FakeProbe> =
        AppModel(settings, auth(scope), { probe }, scope)

    private val okVersion = VersionResponse("1.0.0", "abc", "2026-01-01")

    @Test fun noServerUrlOnboarding() = runTest {
        val s = TestScope(testScheduler)
        val m = model(s, FakeSettings(""), FakeProbe { okVersion })
        m.start()
        s.advanceUntilIdle()
        assertEquals(AppPhase.Onboarding, m.phase.value)
    }

    @Test fun version200Ready() = runTest {
        val s = TestScope(testScheduler)
        val m = model(s, FakeSettings("https://bookq.x.ts.net"), FakeProbe { okVersion })
        m.start()
        s.advanceUntilIdle()
        assertEquals(AppPhase.Ready, m.phase.value)
        assertEquals("bookq.x.ts.net", m.scopeValue.value?.host)
    }

    @Test fun version401Login() = runTest {
        val s = TestScope(testScheduler)
        val m = model(s, FakeSettings("https://bookq.x.ts.net"),
            FakeProbe { throw ApiException.Unauthorized(401) })
        m.start()
        s.advanceUntilIdle()
        assertEquals(AppPhase.Login, m.phase.value)
    }

    @Test fun networkErrorBootError() = runTest {
        val s = TestScope(testScheduler)
        val m = model(s, FakeSettings("https://bookq.x.ts.net"),
            FakeProbe { throw ApiException.Network(IOException("unreachable")) })
        m.start()
        s.advanceUntilIdle()
        assertTrue(m.phase.value is AppPhase.BootError)
    }

    @Test fun serverChangeResets() = runTest {
        val s = TestScope(testScheduler)
        var calls = 0
        val probe = FakeProbe { calls++; okVersion }
        val settings = FakeSettings("https://a.ts.net")
        val m = model(s, settings, probe)
        m.start()
        s.advanceUntilIdle()
        assertEquals(AppPhase.Ready, m.phase.value)
        settings.setServerUrl("https://b.ts.net")
        m.serverChanged()
        s.advanceUntilIdle()
        assertEquals(AppPhase.Ready, m.phase.value)
        assertEquals("b.ts.net", m.scopeValue.value?.host)
        assertEquals(2, calls)
    }

    /** Definitive session death during Ready → phase flips to Login. */
    @Test fun sessionExpiredMovesToLogin() = runTest {
        val s = TestScope(testScheduler)
        // Token exchange always answers 401 → refresh kills the session.
        val unauthed = okhttp3.OkHttpClient.Builder()
            .addInterceptor { chain ->
                okhttp3.Response.Builder()
                    .request(chain.request())
                    .protocol(okhttp3.Protocol.HTTP_1_1)
                    .code(401).message("expired")
                    .body("".toResponseBody())
                    .build()
            }.build()
        val auth = AuthManager(
            store = InMemoryTokenStore(),
            pendingStore = InMemoryPendingLoginStore(),
            exchangeClient = unauthed,
            baseUrlProvider = { "http://x".toHttpUrl() },
            scope = s,
        )
        auth.testSeed(dev.bookq.core.api.TokenPair("a", "r"))
        val settings = FakeSettings("https://bookq.x.ts.net")
        val m = AppModel(settings, auth, { FakeProbe { okVersion } }, s)
        m.start()
        s.advanceUntilIdle()
        assertEquals(AppPhase.Ready, m.phase.value)

        // refreshAccessToken suspends on the real OkHttp thread — call it
        // directly so the exchange lands before we drain the collector.
        auth.refreshAccessToken()
        s.advanceUntilIdle()
        assertEquals(AppPhase.Login, m.phase.value)
    }

    @Test fun bootErrorRetry() = runTest {
        val s = TestScope(testScheduler)
        var fail = true
        val probe = FakeProbe {
            if (fail) throw ApiException.Network(IOException("x")) else okVersion
        }
        val m = model(s, FakeSettings("https://bookq.x.ts.net"), probe)
        m.start()
        s.advanceUntilIdle()
        assertTrue(m.phase.value is AppPhase.BootError)
        fail = false
        m.boot()
        s.advanceUntilIdle()
        assertEquals(AppPhase.Ready, m.phase.value)
    }
}
