package dev.bookq.data

import okhttp3.Authenticator
import okhttp3.HttpUrl
import okhttp3.Request
import okhttp3.Response
import okhttp3.Route

/**
 * RFC 6750 recovery: on 401, run the (blocking) refresh once and replay
 * the request with the rotated Bearer. Guards: same-origin only, only
 * when the failed request actually carried a Bearer, at most once per
 * request.
 *
 * Stale-token fast path: when the failed request's bearer differs from
 * the CURRENT token, another call already rotated it — retry once with
 * the current token and skip the refresh entirely. Refresh only runs
 * when the failed request carried the current token. The single-flight
 * property lives in the refresher (AuthManager), so parallel 401s
 * coalesce to one exchange.
 */
class TokenAuthenticator(
    private val baseUrl: HttpUrl,
    private val currentToken: () -> String?,
    private val refresher: () -> String?,
) : Authenticator {
    /** Request tag marking an already-retried request. */
    private object Retried

    override fun authenticate(route: Route?, response: Response): Request? {
        val request = response.request
        if (!AuthInterceptor.isSameOrigin(request.url, baseUrl)) return null
        val header = request.header("Authorization") ?: return null
        if (!header.startsWith("Bearer ")) return null
        if (request.tag(Retried::class.java) != null) return null

        val presented = header.removePrefix("Bearer ")
        val current = currentToken()
        val bearer = when {
            // Already rotated by a parallel refresh — replay as-is.
            current != null && presented != current -> current
            else -> refresher() ?: return null
        }
        return request.newBuilder()
            .header("Authorization", "Bearer $bearer")
            .tag(Retried::class.java, Retried)
            .build()
    }
}
