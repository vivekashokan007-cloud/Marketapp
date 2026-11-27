package com.marketradar.app

import org.junit.Assert.*
import org.junit.Test
import java.io.File

/** 27 Nov R2: the scheduling bound itself (pure JVM). End-to-end frequency/bytes: CloudRecoveryLargeInventoryTest. */
class RecoveryBudgetTest {
    private val h = RecoveryBudget.History(lastFullEndWallMs = 1_000_000L, lastFullDurationMs = 250_000L)
    private fun input(now: Long, reason: String? = null, history: RecoveryBudget.History? = h, bg: Boolean = false, trigger: String = "scheduled") =
        RecoveryBudget.Input(reason, history, now, bg, trigger)

    @Test fun gapIsTheDutyCycleClampedToFiveToFifteenMinutes() {
        assertEquals(RecoveryBudget.MIN_GAP_MS, RecoveryBudget.gapMs(0))
        assertEquals(RecoveryBudget.MIN_GAP_MS, RecoveryBudget.gapMs(20_000))        // 20 s pass -> 3 min -> 5 min floor
        assertEquals(810_000L, RecoveryBudget.gapMs(90_000))                          // 90 s pass -> 13.5 min (10 % duty)
        assertEquals(RecoveryBudget.MAX_GAP_MS, RecoveryBudget.gapMs(250_000))        // phone's ~250 s pass -> 15 min cap
        assertEquals(RecoveryBudget.MAX_GAP_MS, RecoveryBudget.gapMs(Long.MAX_VALUE / 100))
    }

    @Test fun correctnessConditionsAreNeverDeferred() {
        for (r in listOf("RESTORE_PENDING", "NOT_INITIALIZED", "NOT_READY", "NOT_MONITOR_READY", "WEB_CAPTURE_FAILED", "REVISION_CHANGED", "MANIFEST_CHANGED"))
            assertEquals(r, RecoveryBudget.RUN, RecoveryBudget.decide(input(1_000_001L, reason = r, bg = true)))
        val ok = "a".repeat(64)
        fun reason(ready: Boolean = true, mon: Boolean = true, restore: Boolean = false, web: Boolean = false, init: Boolean = true,
                   rev: Long = 3, local: Long = 3, head: String = ok, localSha: String = ok) =
            RecoveryBudget.correctnessReason(ready, mon, restore, web, init, rev, local, head, localSha)
        assertNull(reason())
        assertEquals("RESTORE_PENDING", reason(restore = true)); assertEquals("NOT_INITIALIZED", reason(init = false))
        assertEquals("NOT_READY", reason(ready = false)); assertEquals("NOT_MONITOR_READY", reason(mon = false))
        assertEquals("WEB_CAPTURE_FAILED", reason(web = true)); assertEquals("REVISION_CHANGED", reason(rev = 4))
        assertEquals("MANIFEST_CHANGED", reason(head = "b".repeat(64))); assertEquals("MANIFEST_CHANGED", reason(head = "", localSha = ""))
    }

    @Test fun cooldownThenBackgroundRuleWithPersistedHistory() {
        assertEquals(RecoveryBudget.RUN, RecoveryBudget.decide(input(5_000_000L, history = null)))             // first ever
        assertEquals(RecoveryBudget.RUN, RecoveryBudget.decide(input(999_999L)))                               // clock went back
        assertEquals("DEFER_COOLDOWN_900s", RecoveryBudget.decide(input(1_000_000L)))
        assertEquals("DEFER_COOLDOWN_1s", RecoveryBudget.decide(input(1_899_999L)))
        assertEquals(RecoveryBudget.RUN, RecoveryBudget.decide(input(1_900_000L)))
        assertEquals("DEFER_BACKGROUND_UNTIL_WORKER", RecoveryBudget.decide(input(1_900_000L, bg = true)))
        assertEquals(RecoveryBudget.RUN, RecoveryBudget.decide(input(1_900_000L, bg = true, trigger = "worker")))
        assertTrue(RecoveryBudget.decide(input(1_000_000L, bg = true, trigger = "worker")).startsWith("DEFER_COOLDOWN_")) // worker obeys cooldown
    }

    /** Worst case on the phone's measured numbers (REPLAY of server timings: one inventory hash ~83 s, 3 per old pass). */
    @Test fun worstCaseHashingIoPerHourIsBoundedByTheGap() {
        val inventory = 14_511L * 1024 * 1024
        val pass = 166_000L                     // R2 FULL pass: 2 inventory hashes x ~83 s (upload/commit excluded)
        val gap = RecoveryBudget.gapMs(pass)
        val passesPerHour = 3_600_000.0 / (gap + pass)
        val gibPerHour = passesPerHour * 2 * inventory / (1024.0 * 1024 * 1024)
        assertEquals(RecoveryBudget.MAX_GAP_MS, gap)
        assertTrue("passes/h=$passesPerHour", passesPerHour < 3.4)
        assertTrue("GiB/h=$gibPerHour", gibPerHour < 97.0)
        println("R2_MEASURE scenario=phone_model gap_ms=$gap passes_per_hour=${"%.2f".format(passesPerHour)} gib_hashed_per_hour=${"%.1f".format(gibPerHour)} " +
            "legacy_gib_per_hour=${"%.1f".format(3_600_000.0 / 250_000 * 3 * inventory / (1024.0 * 1024 * 1024))} cpu_duty=${"%.3f".format(pass.toDouble() / (gap + pass))}")
    }

    private fun src(name: String) = listOf(File("src/main/java/com/marketradar/app/$name"),
        File("app/src/main/java/com/marketradar/app/$name")).first { it.exists() }.readText()

    @Test fun deferPathRunsAfterClaimAndGateAndCannotCommitPruneDeleteOrHash() {
        val body = src("CloudRecovery.kt").substringAfter("private fun runPassMeasured(").substringBefore("private fun fingerprintMap(")
        val budget = body.indexOf("RecoveryBudget.decide(")
        assertTrue(budget > body.indexOf("RecoveryPassGate.decide("))
        assertTrue(body.indexOf("head = transport.rpc(\"mr_cloud_claim\", claim)") in 0 until budget)
        val defer = body.substringAfter("if (budget != RecoveryBudget.RUN) {").substringBefore("return\n")
        for (forbidden in listOf("publishManifest", "mr_cloud_commit", "pruneAcknowledged", "delete", "fingerprints(", "vault.",
                "putLong(\"revision\"", "verifiedBaseline =", "VERIFIED_CHECKPOINT"))
            assertFalse(forbidden, defer.contains(forbidden))
        assertTrue(defer.contains("LOCAL_CHANGES_PENDING_UPLOAD"))
        // Exactly two full SHA-256 inventories remain on the FULL path, both before the commit.
        val full = body.substringAfter("if (budget != RecoveryBudget.RUN) {")
        assertEquals(2, Regex("fingerprints\\((sources|inventory\\(c\\))\\)").findAll(full).count())
        assertTrue(full.indexOf("require(before == fingerprints(inventory(c))") < full.indexOf("vault.publishManifest("))
        assertTrue(full.indexOf("stats.commitConfirmed = true") > full.indexOf("\"HEAD_READBACK_MISMATCH\""))
        // The storage walk is queued off the monitor; runPass never walks storage itself.
        val runPass = src("CloudRecovery.kt").substringAfter("@Synchronized fun runPass(").substringBefore("private const val BUDGET_END")
        assertFalse(runPass.contains("logStorage(c)")); assertTrue(runPass.contains("requestStorageWalk(c)"))
    }
}
