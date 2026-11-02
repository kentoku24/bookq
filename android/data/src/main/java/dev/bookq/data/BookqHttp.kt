package dev.bookq.data

import java.util.concurrent.TimeUnit
import okhttp3.CookieJar
import okhttp3.HttpUrl
import okhttp3.OkHttpClient

/**
 * One shared client shape: no cookies (native auth is Bearer-only — a
 * stray Set-Cookie session would win over Bearer server-side), no HTTP
 * cache (authed error responses must not replay into later boots).
 */
object BookqHttp {

    /** Client for API/media traffic to [baseUrl]. */
    fun apiClient(
        baseUrl: HttpUrl,
        tokenProvider: () -> String?,
        refresher: (() -> String?)? = null,
        base: OkHttpClient? = null,
    ): OkHttpClient {
        val builder = (base?.newBuilder() ?: OkHttpClient.Builder())
            .cookieJar(CookieJar.NO_COOKIES)
            .cache(null)
            .callTimeout(20, TimeUnit.SECONDS)
            .addInterceptor(AuthInterceptor(baseUrl, tokenProvider))
        if (refresher != null) {
            builder.authenticator(
                TokenAuthenticator(baseUrl, currentToken = tokenProvider,
                                   refresher = refresher))
        }
        return builder.build()
    }

    /** Bare client — token exchange / health probes (no auth attached). */
    fun plainClient(base: OkHttpClient? = null): OkHttpClient =
        (base?.newBuilder() ?: OkHttpClient.Builder())
            .cookieJar(CookieJar.NO_COOKIES)
            .cache(null)
            .build()
}
