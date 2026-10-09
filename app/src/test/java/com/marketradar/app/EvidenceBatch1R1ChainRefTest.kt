package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * R1: the Kotlin producer of chain_evidence_ref_v1 must emit EXACTLY the golden fixture that the Python
 * consumer test (test_paper_menu_evidence.ChainReferenceContractTests) reads.
 */
class EvidenceBatch1R1ChainRefTest {
    private fun fixture(): JSONObject = JSONObject(listOf(
        File("src/test/resources/evidence_batch1/chain_evidence_ref_v1.json"),
        File("app/src/test/resources/evidence_batch1/chain_evidence_ref_v1.json")
    ).first { it.isFile }.readText())

    private fun source(name: String): String = listOf(
        File("src/main/java/com/marketradar/app/$name"),
        File("app/src/main/java/com/marketradar/app/$name")
    ).first { it.isFile }.readText()

    @Test
    fun producerEmitsTheGoldenFixtureExactly() {
        val fx = fixture()
        val rowsJson = fx.getJSONArray("posted_rows")
        val rows = (0 until rowsJson.length()).map { rowsJson.getJSONObject(it) }
        val (ref, cov) = ChainEvidenceRef.build(fx.getString("poll_ts"), rows, fx.getString("persist_table_argument"))
        assertEquals(Pc2CompactBatch.canonicalJson(fx.getJSONObject("chain_evidence_ref")), Pc2CompactBatch.canonicalJson(ref))
        assertEquals(Pc2CompactBatch.canonicalJson(fx.getJSONObject("chain_evidence_coverage")), Pc2CompactBatch.canonicalJson(cov))
        assertTrue(ref.has("poll_ts"))
        assertFalse("the misnamed key must never be emitted", ref.has("chain_poll_ts"))
    }

    @Test
    fun failedAndFallbackSavesAreDistinguishable() {
        val fx = fixture()
        val rowsJson = fx.getJSONArray("posted_rows")
        val rows = (0 until rowsJson.length()).map { rowsJson.getJSONObject(it) }
        val failed = ChainEvidenceRef.build("2026-10-09T12:30:05+0530", rows, null).first
        assertEquals("FAILED", failed.getString("persist_status"))
        assertFalse(failed.getBoolean("saved"))
        val fallback = ChainEvidenceRef.build("2026-10-09T12:30:05+0530", rows, "chain_slices").first
        assertEquals("SAVED_FALLBACK_TABLE", fallback.getString("persist_status"))
        assertEquals("chain_slices", fallback.getString("persist_table"))
        assertFalse("a fallback save does not put rows where quote refs point", fallback.getBoolean("saved"))
        val none = ChainEvidenceRef.build("2026-10-09T12:30:05+0530", emptyList(), null).first
        assertEquals("NO_ROWS", none.getString("persist_status"))
        // Same poll_ts string in every case: the timestamp is never dropped on failure.
        assertEquals("2026-10-09T12:30:05+0530", failed.getString("poll_ts"))
    }

    @Test
    fun serviceUsesTheContractAndTheDetailedSave() {
        val svc = source("MarketWatchService.kt")
        val fn = svc.substring(svc.indexOf("private suspend fun persistFetchedChainEvidenceBeforeBrain"), svc.indexOf("private fun extractChainSlice("))
        assertTrue(fn.contains("SupabaseClient.saveChainRowsDetailed(chainRowsJson)"))
        assertTrue(fn.contains("return ChainEvidenceRef.build(pollTs, chainSliceRows, persistTable)"))
        assertFalse(fn.contains(".put(\"poll_ts\""))   // no ad-hoc shape any more
        assertTrue(svc.contains("ctxObj.put(\"chain_evidence_coverage\", chainEvidenceCoverage)"))
        val py = listOf(File("src/main/python/paper_menu_evidence.py"), File("app/src/main/python/paper_menu_evidence.py")).first { it.isFile }.readText()
        assertFalse("consumer must not read the misnamed key", py.contains("chain_ref.get('chain_poll_ts')"))
        assertTrue(py.contains("CHAIN_REF_SCHEMA = 'chain_evidence_ref_v1'"))
    }
}
