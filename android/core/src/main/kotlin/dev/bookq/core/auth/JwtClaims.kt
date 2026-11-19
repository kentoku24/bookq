package dev.bookq.core.auth

import dev.bookq.core.api.AuthUser
import dev.bookq.core.api.BookqJson
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.util.Base64

object JwtClaims {
    /**
     * Decode the JWT payload's `sub`/`name`/`picture` — the token was
     * minted for this client, so claim trust is inherent (no signature
     * check needed client-side). Returns null on malformed input or an
     * empty `sub`.
     */
    fun userFromAccessToken(token: String): AuthUser? {
        val parts = token.split('.')
        if (parts.size < 2) return null
        val payload = try {
            Base64.getUrlDecoder().decode(parts[1])
        } catch (e: IllegalArgumentException) {
            return null
        }
        val obj = try {
            BookqJson.parseToJsonElement(String(payload, Charsets.UTF_8)).jsonObject
        } catch (e: Exception) {
            return null
        }
        val email = obj["sub"]?.jsonPrimitive?.contentOrNull
            ?.takeIf { it.isNotEmpty() } ?: return null
        return AuthUser(
            email = email,
            name = obj["name"]?.jsonPrimitive?.contentOrNull ?: email,
            picture = obj["picture"]?.jsonPrimitive?.contentOrNull,
        )
    }
}
