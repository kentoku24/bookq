package dev.bookq.reader

import android.content.Context
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import okhttp3.HttpUrl

enum class BrowseLayout { TILE, LIST, CARD }

/** User-configurable settings (AppConfig.swift port). */
interface AppSettings {
    val serverUrl: StateFlow<String>
    val originalImages: StateFlow<Boolean>
    val browseLayout: StateFlow<BrowseLayout>
    val volumeKeys: StateFlow<Boolean>
    val dynamicColor: StateFlow<Boolean>
    val perfHud: StateFlow<Boolean>

    suspend fun awaitLoaded()
    suspend fun setServerUrl(v: String)
    suspend fun setOriginalImages(v: Boolean)
    suspend fun setBrowseLayout(v: BrowseLayout)
    suspend fun setVolumeKeys(v: Boolean)
    suspend fun setDynamicColor(v: Boolean)
    suspend fun setPerfHud(v: Boolean)

    fun baseUrl(): HttpUrl? = ServerUrls.parse(serverUrl.value)
}

private val Context.bookqPrefs by preferencesDataStore(name = "bookq")

/** DataStore-backed [AppSettings]. One-shot initial load, then the
 *  StateFlows are the source of truth (single writer = these setters). */
class AppConfig(context: Context, private val scope: CoroutineScope) : AppSettings {

    private object Keys {
        val SERVER_URL = stringPreferencesKey("serverUrl")
        val ORIGINAL_IMAGES = booleanPreferencesKey("originalImages")
        val BROWSE_LAYOUT = stringPreferencesKey("browseLayout")
        val VOLUME_KEYS = booleanPreferencesKey("volumeKeys")
        val DYNAMIC_COLOR = booleanPreferencesKey("dynamicColor")
        val PERF_HUD = booleanPreferencesKey("perfHud")
    }

    private val store = context.bookqPrefs
    private val loaded = MutableStateFlow(false)

    private val _serverUrl = MutableStateFlow("")
    override val serverUrl = _serverUrl.asStateFlow()
    private val _originalImages = MutableStateFlow(false)
    override val originalImages = _originalImages.asStateFlow()
    private val _browseLayout = MutableStateFlow(BrowseLayout.TILE)
    override val browseLayout = _browseLayout.asStateFlow()
    private val _volumeKeys = MutableStateFlow(false)
    override val volumeKeys = _volumeKeys.asStateFlow()
    private val _dynamicColor = MutableStateFlow(false)
    override val dynamicColor = _dynamicColor.asStateFlow()
    private val _perfHud = MutableStateFlow(false)
    override val perfHud = _perfHud.asStateFlow()

    init {
        scope.launch {
            val p = store.data.first()
            _serverUrl.value = p[Keys.SERVER_URL].orEmpty()
            _originalImages.value = p[Keys.ORIGINAL_IMAGES] ?: false
            _browseLayout.value = p[Keys.BROWSE_LAYOUT]
                ?.let { runCatching { BrowseLayout.valueOf(it) }.getOrNull() }
                ?: BrowseLayout.TILE
            _volumeKeys.value = p[Keys.VOLUME_KEYS] ?: false
            _dynamicColor.value = p[Keys.DYNAMIC_COLOR] ?: false
            _perfHud.value = p[Keys.PERF_HUD] ?: false
            loaded.value = true
        }
    }

    override suspend fun awaitLoaded() {
        loaded.first { it }
    }

    override suspend fun setServerUrl(v: String) {
        store.edit { it[Keys.SERVER_URL] = v }
        _serverUrl.value = v
    }

    override suspend fun setOriginalImages(v: Boolean) {
        store.edit { it[Keys.ORIGINAL_IMAGES] = v }
        _originalImages.value = v
    }

    override suspend fun setBrowseLayout(v: BrowseLayout) {
        store.edit { it[Keys.BROWSE_LAYOUT] = v.name }
        _browseLayout.value = v
    }

    override suspend fun setVolumeKeys(v: Boolean) {
        store.edit { it[Keys.VOLUME_KEYS] = v }
        _volumeKeys.value = v
    }

    override suspend fun setDynamicColor(v: Boolean) {
        store.edit { it[Keys.DYNAMIC_COLOR] = v }
        _dynamicColor.value = v
    }

    override suspend fun setPerfHud(v: Boolean) {
        store.edit { it[Keys.PERF_HUD] = v }
        _perfHud.value = v
    }
}
