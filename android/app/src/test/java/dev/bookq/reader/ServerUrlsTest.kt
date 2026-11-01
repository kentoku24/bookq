package dev.bookq.reader

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** Server URL normalization + cleartext policy (AppConfigTests.swift port). */
class ServerUrlsTest {

    @Test fun httpsAccepted() {
        assertEquals("https://bookq.tail-x.ts.net",
            ServerUrls.normalize("https://bookq.tail-x.ts.net"))
        assertEquals("https://bookq.tail-x.ts.net",
            ServerUrls.normalize("bookq.tail-x.ts.net")) // scheme-less → https
        assertEquals("https://bookq.tail-x.ts.net",
            ServerUrls.normalize("  https://bookq.tail-x.ts.net/  ")) // trim + drop trailing /
    }

    /** Remote http is rejected — the token would travel in cleartext. */
    @Test fun remoteHttpRejected() {
        assertNull(ServerUrls.normalize("http://bookq.tail-x.ts.net"))
        assertNull(ServerUrls.normalize("http://192.168.1.10:8080"))
        assertNull(ServerUrls.normalize("http://10.0.0.5"))
    }

    /** Loopback (+ emulator host bridge) stays allowed for dev servers. */
    @Test fun loopbackAndEmulatorHttpAllowed() {
        assertEquals("http://localhost:8099",
            ServerUrls.normalize("http://localhost:8099"))
        assertEquals("http://127.0.0.1:8080",
            ServerUrls.normalize("http://127.0.0.1:8080"))
        assertEquals("http://10.0.2.2:8080",
            ServerUrls.normalize("http://10.0.2.2:8080"))
    }

    @Test fun pathPrefixKept() {
        assertEquals("https://host/sub", ServerUrls.normalize("https://host/sub/"))
    }

    @Test fun garbageRejected() {
        assertNull(ServerUrls.normalize(""))
        assertNull(ServerUrls.normalize("   "))
        assertNull(ServerUrls.normalize("ftp://x"))
        assertNull(ServerUrls.normalize("http://"))
    }
}
