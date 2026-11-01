package dev.bookq.core.auth

import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64

/** RFC 7636 PKCE helpers. */
object Pkce {
    private const val ALPHABET =
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~"
    private val random = SecureRandom()

    /** RFC 7636 §4.1 — `length` chars of the unreserved alphabet (43–128). */
    fun makeVerifier(length: Int = 64): String {
        require(length in 43..128) { "verifier length must be 43..128" }
        val sb = StringBuilder(length)
        repeat(length) { sb.append(ALPHABET[random.nextInt(ALPHABET.length)]) }
        return sb.toString()
    }

    /** `BASE64URL-ENCODE(SHA256(verifier))` without padding. */
    fun challenge(verifier: String): String {
        val digest = MessageDigest.getInstance("SHA-256")
            .digest(verifier.toByteArray(Charsets.US_ASCII))
        return Base64.getUrlEncoder().withoutPadding().encodeToString(digest)
    }
}
