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
        // b503: the post-build path moved to CaptureEngine. The lossless compact envelope is
        // captured as canonical bytes BEFORE compaction, frozen into the durable capture bundle,
        // then enqueued; the legacy PC2 rows remain a separate (now restart-safe) channel.
        val engine = source("src/main/java/com/marketradar/app/CaptureEngine.kt")
        val build = engine.indexOf("Pc2CompactBatch.build(rawSnapObj)")
        val bytes = engine.indexOf("Pc2CompactBatch.canonicalJson(compact.envelope())", build)
        val compact = engine.indexOf("EvaluationLocalCache.compactBrainSnapshotForPersistence(rawSnapObj)", bytes)
        val legacy = engine.indexOf("Pc2LegacyAuthorityRows.build(snapObj)", compact)
        val bundle = engine.indexOf("journal.writeBundle(", legacy)
        val enqueue = engine.indexOf("channels.enqueueCompact(", bundle)
        assertTrue(build >= 0 && bytes > build && compact > bytes && legacy > compact && bundle > legacy && enqueue > bundle)
        val runtime = source("src/main/java/com/marketradar/app/CaptureRuntime.kt")
        assertTrue(runtime.contains("Pc2TelemetryOutbox.enqueue(filesDir, built)"))
        assertTrue(runtime.contains("SupabaseClient.uploadPc2AuthorityDecisionRows("))
        val service = source("src/main/java/com/marketradar/app/MarketWatchService.kt")
        val legacyDrain = service.indexOf("CaptureRuntime.drainLegacy()")
        assertTrue(legacyDrain >= 0 && service.indexOf("drainPc2CompactOutbox()", legacyDrain) > legacyDrain)
    }

    @Test
    fun compactReferenceSurvivesSnapshotCompaction() {
        val cache = source("src/main/java/com/marketradar/app/EvaluationLocalCache.kt")
        assertTrue(cache.contains("snapshot_pc2_authority_compact_ref"))
        assertTrue(cache.contains("snapshot_pc2_authority_decisions"))
    }

    @Test
    fun migrationIsAdditiveRlsAndReadOnlyForMobileRoles() {
        val sql = source("supabase/migrations/20260929100000_pc2_compact_dual_write.sql")
            .lowercase()
        assertTrue(sql.contains("create table if not exists public.ml_pc2_policy_registry"))
        assertTrue(sql.contains("create table if not exists public.ml_pc2_decision_batches"))
        assertTrue(sql.contains("enable row level security"))
        // Round 3 (B4): mobile roles may read, never write, the compact tables.
        assertTrue(sql.contains("grant select on table public.ml_pc2_policy_registry to anon, authenticated"))
        assertTrue(sql.contains("grant select on table public.ml_pc2_decision_batches to anon, authenticated"))
        assertFalse(sql.contains("grant select, insert"))
        assertFalse(Regex("grant[^;]*(insert|update|delete)[^;]*to anon").containsMatchIn(sql))
        // No write POLICY for any role. (`select ... for update` row locks inside
        // the ingestion function are not policies and are expected.)
        assertFalse(Regex("create policy[^;]*for (insert|update|delete|all)").containsMatchIn(sql))
        assertTrue(sql.contains("on delete restrict"))
        // The only write path is the authorised ingestion function.
        assertTrue(sql.contains("security definer"))
        assertTrue(sql.contains("grant execute on function public.pc2_ingest_compact_batch(text, jsonb, jsonb) to anon, authenticated"))
        assertTrue(sql.contains("revoke all on function public.pc2_ingest_compact_batch(text, jsonb, jsonb) from public"))
        // Legacy tables are not touched.
        assertFalse(sql.contains("ml_pc2_authority_decisions"))
        assertFalse(sql.contains("ml_brain_snapshots"))
        assertFalse(sql.contains("drop table"))
        assertFalse(sql.contains("delete from"))
        // A TRUNCATE statement, not the word inside `source_possibly_truncated`.
        assertFalse(Regex("(^|;)\\s*truncate\\s", RegexOption.MULTILINE).containsMatchIn(sql))
    }

    /**
     * Codex review correction R5: every identity column must be a hash the
     * DATABASE can re-derive from the row, so a holder of the publishable key
     * cannot insert forged content under a legitimate identity.
     */
    @Test
    fun migrationBindsEveryIdentityColumnToTheStoredBytes() {
        val sql = source("supabase/migrations/20260929100000_pc2_compact_dual_write.sql")
            .lowercase()
        assertTrue(sql.contains("ml_pc2_policy_registry_content_bound"))
        assertTrue(sql.contains("ml_pc2_decision_batches_content_bound"))
        assertTrue(sql.contains("ml_pc2_decision_batches_identity_bound"))
        assertTrue(sql.contains("ml_pc2_decision_batches_counts_bound"))
        assertTrue(sql.contains("encode(sha256(convert_to(canonical_policy, 'utf8')), 'hex')"))
        assertTrue(sql.contains("encode(sha256(convert_to(grouped_canonical, 'utf8')), 'hex')"))
        // The grouped payload must be stored exactly once, as the bytes hashed.
        assertFalse(
            "a generated jsonb mirror would store the payload twice",
            sql.contains("grouped_decisions_json")
        )
    }

    /** The upload path must send the canonical bytes the constraints check. */
    @Test
    fun theWriterSendsTheCanonicalBytesTheConstraintsVerify() {
        val builder = source("src/main/java/com/marketradar/app/Pc2CompactBatch.kt")
        assertTrue(builder.contains("\"canonical_policy\", canonicalPolicy"))
        assertTrue(builder.contains("\"grouped_canonical\", groupedCanonical"))
        assertTrue(builder.contains("\"grouped_digest\", groupedDigest"))
        assertTrue(builder.contains("val batchId = batchIdOf("))
    }
}
