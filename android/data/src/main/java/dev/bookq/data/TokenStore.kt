package dev.bookq.data

import dev.bookq.core.api.TokenPair

/**
 * Persistence for the access+refresh pair. The pair is written as ONE
 * record so a refresh response can never land half-saved.
 */
interface TokenStore {
    fun load(): TokenPair?
    fun save(pair: TokenPair)
    fun clear()
}

/** Volatile store for tests. */
class InMemoryTokenStore : TokenStore {
    @Volatile private var pair: TokenPair? = null
    override fun load(): TokenPair? = pair
    override fun save(pair: TokenPair) { this.pair = pair }
    override fun clear() { pair = null }
}
