package com.marketradar.app

/** Device acquisition window, frozen before any persistence/analysis-lane wait.
 * The start bounds the age of every response acquired in the block conservatively.
 * Provider feed timestamps remain separate in spot/chain provenance; this is not a feed-time claim.
 */
data class MarketSnapshotIdentity(val session: String, val collectedAtMs: Long, val completedAtMs: Long) {
    fun usableFor(today: String, nowMs: Long): Boolean =
        session == today && collectedAtMs > 0L && completedAtMs >= collectedAtMs &&
            completedAtMs <= nowMs + 60_000L

    data class Acquisition<T>(val value: T, val identity: MarketSnapshotIdentity)

    companion object {
        suspend fun <T> acquire(session: String, clock: () -> Long = System::currentTimeMillis,
                                fetch: suspend () -> T): Acquisition<T> {
            val start = clock()
            val value = fetch()
            return Acquisition(value, MarketSnapshotIdentity(session, start, clock()))
        }
    }
}
