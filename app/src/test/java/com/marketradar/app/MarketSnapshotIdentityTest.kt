package com.marketradar.app

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test

class MarketSnapshotIdentityTest {
    private val session = "2026-10-15"
    private val start = 1_792_036_800_000L

    @Test fun acquisitionClockSurvivesPersistenceAndWaitingForTheRealAnalysisLane() = runBlocking(Dispatchers.Default) {
        val authority = AnalysisAuthority()
        authority.attachRevisionSource { "B" }
        val busy = CompletableDeferred<Unit>()
        val release = CompletableDeferred<Unit>()
        val holder = async { authority.withPollLane { busy.complete(Unit); release.await() } }
        busy.await()
        var clock = start
        // Same acquisition wrapper used around MarketWatchService's actual parallel fetches.
        val acquired = MarketSnapshotIdentity.acquire(session, { clock }) {
            clock += 20_000L
            "NF/BNF quotes and chains"
        }
        assertEquals(start, acquired.identity.collectedAtMs)
        assertEquals(start + 20_000L, acquired.identity.completedAtMs)
        clock += 90_000L // evidence persistence + lane wait must count as age
        val queued = async {
            authority.withPollLane {
                assertTrue(acquired.identity.usableFor(session, clock))
                assertEquals(110_000L, clock - acquired.identity.collectedAtMs)
                val id = AnalysisAuthority.Identity(session, "B", acquired.identity.collectedAtMs, "POLL")
                authority.publish(id) { true }
            }
        }
        release.complete(Unit)
        holder.await()
        assertEquals(AnalysisAuthority.Outcome.PUBLISHED, queued.await())
        assertEquals(start, authority.lastPublished()!!.marketCollectedAtMs)
        // A refresh keeps the exact same acquired identity, including the network/queue elapsed time.
        val cached = acquired.identity
        assertNull(AnalysisAuthority.refreshPrecondition(cached.session, cached.collectedAtMs, session, start + 360_000L))
        assertEquals(AnalysisAuthority.Outcome.MARKET_TOO_OLD,
            AnalysisAuthority.refreshPrecondition(cached.session, cached.collectedAtMs, session, start + 360_001L))
    }

    @Test fun invalidIdentityNeverAuthorizesPublicationOrNotification() {
        assertFalse(MarketSnapshotIdentity(session, 0L, start).usableFor(session, start))
        assertFalse(MarketSnapshotIdentity(session, start, start - 1L).usableFor(session, start))
        assertFalse(MarketSnapshotIdentity(session, start, start + 120_000L).usableFor(session, start))
        assertFalse(MarketSnapshotIdentity(session, start, start).usableFor("2026-10-16", start))
        val authority = AnalysisAuthority()
        authority.attachRevisionSource { "B" }
        val id = AnalysisAuthority.Identity(session, "B", 0L, "POLL")
        assertEquals(AnalysisAuthority.Outcome.MARKET_IDENTITY_UNAVAILABLE, authority.publish(id) { error("must not commit") })
        assertFalse(authority.dispatchIfCurrent(id) { error("must not dispatch") })
    }
}
