package dev.bookq.data

import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.bookq.core.api.TokenPair
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith

/** AES-GCM round-trip through AndroidKeyStore (instrumented — the
 *  keystore does not exist on the JVM). */
@RunWith(AndroidJUnit4::class)
class KeystoreTokenStoreTest {

    private val store = KeystoreTokenStore(
        ApplicationProvider.getApplicationContext())

    @Test
    fun roundTrip() {
        store.clear()
        assertNull(store.load())

        val pair = TokenPair(accessToken = "a.b.c", refreshToken = "r1")
        store.save(pair)
        assertEquals(pair, store.load())

        val pair2 = TokenPair(accessToken = "x.y.z", refreshToken = "r2")
        store.save(pair2)
        assertEquals(pair2, store.load())

        store.clear()
        assertNull(store.load())
    }
}
