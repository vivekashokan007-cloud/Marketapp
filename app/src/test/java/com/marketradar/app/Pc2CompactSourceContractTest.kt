package com.marketradar.app

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class Pc2CompactSourceContractTest {
    private fun source(path: String): String = listOf(File(path), File("app/$path"), File("../$path"))
        .first { it.isFile }.readText()

    @Test
    fun durableEnqueuePrecedesSnapshotCompactionAndLegacyDualWriteRemains() {
        val service = source("src/main/java/com/marketradar/app/MarketWatchService.kt")
        val build = service.indexOf("val pc2CompactBatch = Pc2CompactBatch.build(rawSnapObj)")
        val enqueue = service.indexOf("Pc2TelemetryOutbox.enqueue(", build)
        val compact = service.indexOf("EvaluationLocalCache.compactBrainSnapshotForPersistence(rawSnapObj)", enqueue)
        val legacy = service.indexOf("SupabaseClient.savePc2AuthorityDecisions(snapObj)", compact)
        val newDrain = service.indexOf("drainPc2CompactOutbox()", legacy)
        assertTrue(build >= 0 && enqueue > build && compact > enqueue && legacy > compact && newDrain > legacy)
    }

    @Test
    fun compactReferenceSurvivesSnapshotCompaction() {
        val cache = source("src/main/java/com/marketradar/app/EvaluationLocalCache.kt")
        assertTrue(cache.contains("snapshot_pc2_authority_compact_ref"))
        assertTrue(cache.contains("snapshot_pc2_authority_decisions"))
    }

    @Test
    fun migrationIsAdditiveRlsAndInsertOnlyForMobileRoles() {
        val sql = source("supabase/migrations/20260929100000_pc2_compact_dual_write.sql")
            .lowercase()
        assertTrue(sql.contains("create table if not exists public.ml_pc2_policy_registry"))
        assertTrue(sql.contains("create table if not exists public.ml_pc2_decision_batches"))
        assertTrue(sql.contains("enable row level security"))
        assertTrue(sql.contains("grant select, insert"))
        assertTrue(sql.contains("on delete restrict"))
        assertFalse(sql.contains("grant select, insert, update"))
        assertFalse(sql.contains("for update"))
        assertFalse(sql.contains("for delete"))
        assertFalse(sql.contains("drop table"))
        assertFalse(sql.contains("delete from"))
        assertFalse(sql.contains("truncate"))
    }
}
