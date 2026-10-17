package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

/** b510 Batch C: native X1 context gating, bounded status, bounded export, snapshot retention. */
class B510PaperBuyingResearchTest {
    @get:Rule val tmp = TemporaryFolder()

    private fun stale() = JSONObject().put("x1CaptureSource", "LIVE_PHONE_POLL").put("x1StateDir", "/stale")
        .put("x1PollMs", 1L).put("x1QuoteFetchedMs", JSONObject().put("NF", 1L))

    private fun hasNoX1(ctx: JSONObject) = PaperBuyingResearch.CONTEXT_KEYS.none { ctx.has(it) }

    @Test fun livePaperPollAttachesOnlyMeasuredQuotes() {
        val files = tmp.newFolder("files")
        val ctx = stale().put("executionMode", "paper")
        assertTrue(PaperBuyingResearch.applyContext(ctx, false, files, 1_000L, 900L, 0L))
        assertEquals("LIVE_PHONE_POLL", ctx.getString("x1CaptureSource"))
        assertEquals(File(files, "paper_buying_x1_v2").absolutePath, ctx.getString("x1StateDir"))
        assertEquals(1_000L, ctx.getLong("x1PollMs"))
        val quotes = ctx.getJSONObject("x1QuoteFetchedMs")
        assertEquals(900L, quotes.getLong("NF"))
        assertFalse("unfetched chain must not get a fabricated time", quotes.has("BNF"))
    }

    @Test fun refreshLiveMissingAndConflictingModesStripStaleKeys() {
        val files = tmp.newFolder("files")
        val cases = listOf(
            Pair(stale().put("executionMode", "paper"), true),          // refresh / re-analysis
            Pair(stale().put("executionMode", "live"), false),
            Pair(stale(), false),                                         // unresolved mode
            Pair(stale().put("executionMode", "PAPER"), false),          // must be exact
            Pair(stale().put("executionMode", "paper").put("execution_mode", "live"), false))
        for ((ctx, refresh) in cases) {
            assertFalse(PaperBuyingResearch.applyContext(ctx, refresh, files, 1L, 1L, 1L))
            assertTrue("stale X1 keys survived: $ctx", hasNoX1(ctx))
        }
        val agreeing = JSONObject().put("executionMode", "paper").put("execution_mode", "paper")
        assertTrue(PaperBuyingResearch.applyContext(agreeing, false, files, 1L, 1L, 1L))
    }

    @Test fun statusIsBoundedAndNeverClaimsAuthority() {
        val files = tmp.newFolder("files")
        assertEquals("NOT_STARTED", JSONObject(PaperBuyingResearch.status(files)).getString("status"))
        val dir = File(files, "paper_buying_x1_v2").apply { mkdirs() }
        File(dir, "status.json").writeText("""{"status":"OK","recommendation_authority":true}""")
        val ok = JSONObject(PaperBuyingResearch.status(files))
        assertEquals("OK", ok.getString("status"))
        assertFalse(ok.getBoolean("recommendation_authority"))
        assertTrue(ok.getBoolean("quote_simulation_only"))
        File(dir, "status.json").writeText("{not json")
        assertEquals("STATUS_UNREADABLE", JSONObject(PaperBuyingResearch.status(files)).getString("status"))
        File(dir, "status.json").writeText("{\"pad\":\"" + "x".repeat(17_000) + "\"}")
        assertEquals("STATUS_SIZE_LIMIT", JSONObject(PaperBuyingResearch.status(files)).getString("status"))
    }

    @Test fun exportIsBoundedHashedAndPathFree() {
        val files = tmp.newFolder("files")
        assertEquals("NO_JOURNAL", PaperBuyingResearch.exportEnvelope(files, 5L).getString("status"))
        val dir = File(files, "paper_buying_x1_v2").apply { mkdirs() }
        val journal = File(dir, "journal.json")
        journal.writeText("{\"records\":[]}")
        val env = PaperBuyingResearch.exportEnvelope(files, 5L)
        assertEquals("OK", env.getString("status"))
        assertEquals("x1_export_v1", env.getString("schema"))
        assertEquals(PaperBuyingResearch.sha256(journal.readBytes()), env.getString("journal_sha256"))
        assertTrue(env.getBoolean("journal_parse_ok"))
        assertFalse("export must not expose local paths", env.toString().contains(files.absolutePath))
        journal.writeText("{corrupt")                                   // still exported for review
        val corrupt = PaperBuyingResearch.exportEnvelope(files, 6L)
        assertEquals("OK", corrupt.getString("status"))
        assertFalse(corrupt.getBoolean("journal_parse_ok"))
        assertEquals("{corrupt", corrupt.getString("journal_text"))
        journal.writeBytes(ByteArray(1_000_001) { 'a'.code.toByte() })
        val big = PaperBuyingResearch.exportEnvelope(files, 7L)
        assertEquals("JOURNAL_SIZE_LIMIT", big.getString("status"))
        assertNull(big.optString("journal_text", null))
    }

    @Test fun boundedX1StatusSurvivesSnapshotCompactionAndUploadSanitizer() {
        val x1 = JSONObject().put("status", "OK").put("protocol_id", "X1_gap_continuation_v2_causal_paper_20261017")
            .put("research_only", true).put("recommendation_authority", false)
            .put("counts", JSONObject().put("NF", JSONObject().put("primary_resolved", 3)))
        val raw = JSONObject().put("ts", 1L).put("verdict_json", JSONObject().put("direction", "WAIT").toString())
            .put("market_forces_json", JSONObject().put("paper_buying_research", x1).toString())
        for (field in listOf("verdict_json", "market_forces_json")) raw.put(field, JSONObject(raw.getString(field)))
        val compact = EvaluationLocalCache.compactBrainSnapshotForPersistence(raw)
        val out = JSONObject(BrainSnapshotPayloadSanitizer.sanitize(compact).toString())
        val kept = out.getJSONObject("market_forces_json").getJSONObject("paper_buying_research")
        assertEquals("OK", kept.getString("status"))
        assertEquals(3, kept.getJSONObject("counts").getJSONObject("NF").getInt("primary_resolved"))
    }
}
