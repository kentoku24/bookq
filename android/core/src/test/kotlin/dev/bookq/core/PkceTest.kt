package dev.bookq.core

import dev.bookq.core.auth.Pkce
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PkceTest {
    private val alphabet =
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~".toSet()

    /** Verifier is 64 chars of the RFC 7636 unreserved alphabet. */
    @Test fun verifierShape() {
        val v = Pkce.makeVerifier()
        assertEquals(64, v.length)
        assertTrue(v.all { it in alphabet })
    }

    /** Challenge = base64url(SHA-256(verifier)) — 43 chars, no +/=/. */
    @Test fun challengeShape() {
        val c = Pkce.challenge(Pkce.makeVerifier())
        assertEquals(43, c.length)
        assertFalse(c.contains("+") || c.contains("/") || c.contains("="))
    }

    /** RFC 7636 appendix B golden vector. */
    @Test fun challengeGoldenVector() {
        assertEquals(
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
            Pkce.challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
        )
    }
}
