package dev.bookq.data

import okhttp3.HttpUrl
import okhttp3.Interceptor
import okhttp3.Response

/**
 * Attaches `Authorization: Bearer <token>` ONLY when the request target is
 * the same origin (scheme + host + port) as the API base URL — media and
 * API requests qualify, anything off-origin must never see the token.
 */
class AuthInterceptor(
    private val baseUrl: HttpUrl,
    private val tokenProvider: () -> String?,
) : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response {
        val request = chain.request()
        val token = tokenProvider()
        val out = if (token != null && isSameOrigin(request.url, baseUrl)) {
            request.newBuilder()
                .header("Authorization", "Bearer $token")
                .build()
        } else {
            request
        }
        return chain.proceed(out)
    }

    companion object {
        fun isSameOrigin(url: HttpUrl, base: HttpUrl): Boolean =
            url.scheme == base.scheme && url.host == base.host && url.port == base.port
    }
}
