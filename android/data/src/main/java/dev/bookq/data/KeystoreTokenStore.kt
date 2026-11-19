package dev.bookq.data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.AtomicFile
import dev.bookq.core.api.BookqJson
import dev.bookq.core.api.TokenPair
import java.io.File
import java.security.GeneralSecurityException
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import kotlinx.serialization.encodeToString

/**
 * [TokenStore] backed by an AES-256-GCM key in AndroidKeyStore. The pair
 * is serialized as ONE JSON blob, encrypted (12-byte IV prepended), and
 * written atomically via [AtomicFile] inside `noBackupFilesDir` (excluded
 * from cloud backup and device transfer).
 */
class KeystoreTokenStore(context: Context) : TokenStore {

    private val file = File(context.noBackupFilesDir, "bookq-token-pair.bin")

    private fun key(): SecretKey {
        val ks = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
        (ks.getEntry(KEY_ALIAS, null) as? KeyStore.SecretKeyEntry)
            ?.let { return it.secretKey }
        val generator = KeyGenerator.getInstance(
            KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE)
        generator.init(
            KeyGenParameterSpec.Builder(KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build())
        return generator.generateKey()
    }

    override fun load(): TokenPair? {
        if (!file.isFile) return null
        val bytes = file.readBytes()
        if (bytes.size <= IV_LEN + GCM_TAG_BYTES) return null
        return try {
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.DECRYPT_MODE, key(),
                GCMParameterSpec(GCM_TAG_BITS, bytes, 0, IV_LEN))
            val plain = cipher.doFinal(bytes, IV_LEN, bytes.size - IV_LEN)
            BookqJson.decodeFromString<TokenPair>(String(plain, Charsets.UTF_8))
        } catch (e: GeneralSecurityException) {
            null // corrupt file or rotated key — treat as logged out
        } catch (e: Exception) {
            null
        }
    }

    override fun save(pair: TokenPair) {
        val plain = BookqJson.encodeToString(pair).toByteArray(Charsets.UTF_8)
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, key())
        val ciphertext = cipher.doFinal(plain)
        val atomic = AtomicFile(file)
        val out = atomic.startWrite()
        try {
            out.write(cipher.iv)
            out.write(ciphertext)
            atomic.finishWrite(out)
        } catch (e: Exception) {
            atomic.failWrite(out)
            throw e
        }
    }

    override fun clear() {
        file.delete()
    }

    private companion object {
        const val ANDROID_KEYSTORE = "AndroidKeyStore"
        const val KEY_ALIAS = "bookq-token-pair"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val IV_LEN = 12
        const val GCM_TAG_BITS = 128
        const val GCM_TAG_BYTES = 16
    }
}
