package dev.bookq.reader.ui.browse

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import coil3.ImageLoader
import coil3.compose.AsyncImage
import coil3.request.ImageRequest
import coil3.request.bitmapConfig
import dev.bookq.core.browse.LevelModel
import dev.bookq.data.BookqApi

/**
 * Tile cover — `renderTileImg` order: multi-thumb sprite (one server-
 * composed image, ≥2 thumbs), else `thumb` → `thumbs[0]` single, else the
 * folder placeholder. Loads through the scope's authed [ImageLoader].
 */
@Composable
fun CoverImage(
    cover: LevelModel.Cover,
    folder: String,
    api: BookqApi,
    imageLoader: ImageLoader,
    coverW: Int,
    modifier: Modifier = Modifier,
) {
    val url = remember(cover, folder, coverW) {
        when (cover) {
            is LevelModel.Cover.Single ->
                api.thumbUrl(folder, cover.name, coverW).toString()
            is LevelModel.Cover.Sprite ->
                api.thumbSpriteUrl(
                    folder, cover.names, cover.spec.rows, coverW).toString()
            LevelModel.Cover.None -> null
        }
    }
    if (url == null) {
        CoverPlaceholder(modifier)
        return
    }
    val context = LocalContext.current
    val request = remember(url) {
        // RGB_565: halves decode + GPU-upload bytes on Mali-G57 —
        // banding is invisible at grid-thumb sizes (#302).
        ImageRequest.Builder(context).data(url)
            .bitmapConfig(android.graphics.Bitmap.Config.RGB_565)
            .build()
    }
    AsyncImage(
        model = request,
        imageLoader = imageLoader,
        contentDescription = null,
        contentScale = ContentScale.Crop,
        modifier = modifier,
    )
}

@Composable
fun CoverPlaceholder(modifier: Modifier = Modifier) {
    Box(
        modifier.fillMaxSize(),
        contentAlignment = Alignment.Center,
    ) {
        // Web `_tilePlaceholder` is the literal 📁 glyph.
        Text("📁", color = MaterialTheme.colorScheme.outline)
    }
}

/** The `w` query both `/thumb/` and the sprite endpoint clamp to — web's
 *  `_coverW()`/`coverWidth`: 400 only at dpr ≥ 2, else 200 (keeps decode
 *  cost off the Mali-G57 scroll cliff). Cards always use 400. */
fun coverWidth(density: Float): Int = if (density >= 2f) 400 else 200
