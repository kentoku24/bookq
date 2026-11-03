package dev.bookq.core

import dev.bookq.core.auth.JwtClaims
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import java.util.Base64

class JwtClaimsTest {
    private fun b64(s: String): String =
        Base64.getUrlEncoder().withoutPadding().encodeToString(s.toByteArray())

    private fun jwt(sub: String = "u@x.jp", name: String? = "U"): String {
        val namePart = name?.let { "\"name\":\"$it\"," } ?: ""
        return "${b64("{}")}.${b64("{\"sub\":\"$sub\",${namePart}\"exp\":9999999999}")}.sig"
    }

    @Test fun userDecodedFromJwt() {
        val u = JwtClaims.userFromAccessToken(jwt(sub = "me@x.jp", name = "Me"))
        assertEquals("me@x.jp", u?.email)
        assertEquals("Me", u?.name)
    }

    @Test fun nameDefaultsToEmail() {
        val u = JwtClaims.userFromAccessToken(jwt(sub = "me@x.jp", name = null))
        assertEquals("me@x.jp", u?.name)
    }

    @Test fun nilForGarbageToken() {
        assertNull(JwtClaims.userFromAccessToken("not-a-jwt"))
        assertNull(JwtClaims.userFromAccessToken("a.b.c"))
        assertNull(JwtClaims.userFromAccessToken("${b64("{}")}.${b64("{\"sub\":\"\"}")}.x"))
    }
}
