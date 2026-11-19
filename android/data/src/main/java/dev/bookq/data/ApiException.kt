package dev.bookq.data

/** Wire/transport failures — mirrors the iOS `APIError` mapping. */
sealed class ApiException(message: String, cause: Throwable? = null) :
    Exception(message, cause) {

    /** 401/403 → re-login. */
    class Unauthorized(val status: Int) :
        ApiException("認証が必要です (HTTP $status)")

    /** Any other non-2xx status. */
    class Status(val code: Int) :
        ApiException("サーバーエラー (HTTP $code)")

    class Decode(cause: Throwable) :
        ApiException("レスポンス解析エラー: ${cause.message}", cause)

    class Network(cause: Throwable) :
        ApiException("ネットワークエラー: ${cause.message}", cause)
}
