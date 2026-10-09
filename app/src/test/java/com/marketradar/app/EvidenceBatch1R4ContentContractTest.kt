package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** R4: durable-content digest + readback contract on the client side (server side: tools/paper_menu_db_suite). */
class EvidenceBatch1R4ContentContractTest {
    private fun frozen(): JSONObject {
        val c = JSONArray()
        // deliberately out of rank order: canonicalCandidates must order by generated_rank
        c.put(JSONObject().put("candidate_id", "B").put("generated_rank", 2).put("legs", JSONArray().put(JSONObject().put("bid", 10.5))))
        c.put(JSONObject().put("generated_rank", 1).put("candidate_id", "A").put("note", "é\u0001"))
        return JSONObject().put("candidates", c)
    }

    @Test fun emptyDigestIsSha256OfEmptyString() {
        assertEquals("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", PaperMenuCapture.contentDigest(emptyList()))
        assertEquals(PaperMenuCapture.EMPTY_CONTENT_SHA256, PaperMenuCapture.contentDigest(emptyList()))
    }

    @Test fun canonicalRowsAreRankOrderedAndDigestIsJoinOfRowHashes() {
        val rows = PaperMenuCapture.canonicalCandidates(frozen())
        assertEquals(listOf(1, 2), rows.map { it.first })
        assertTrue(rows[0].second.startsWith("{\"candidate_id\":\"A\""))
        val expected = PaperMenuCapture.sha256Hex(rows.joinToString("\n") { PaperMenuCapture.sha256Hex(it.second) })
        assertEquals(expected, PaperMenuCapture.contentDigest(rows.map { it.second }))
    }

    @Test fun readbackDetectsEveryDifference() {
        val rows = PaperMenuCapture.canonicalCandidates(frozen()).map { it.second }
        fun rb(vararg r: Triple<String, Int, String>) = JSONArray().apply {
            r.forEach { put(JSONObject().put("candidate_id", it.first).put("generated_rank", it.second).put("content_sha256", it.third)) }
        }
        val good = rb(Triple("A", 1, PaperMenuCapture.sha256Hex(rows[0])), Triple("B", 2, PaperMenuCapture.sha256Hex(rows[1])))
        assertNull(PaperMenuCapture.readbackDifference(rows, good))
        assertEquals("row_count:1!=2", PaperMenuCapture.readbackDifference(rows, rb(Triple("A", 1, PaperMenuCapture.sha256Hex(rows[0])))))
        assertEquals("row_1:content_sha256", PaperMenuCapture.readbackDifference(rows,
            rb(Triple("A", 1, PaperMenuCapture.sha256Hex(rows[0])), Triple("B", 2, PaperMenuCapture.sha256Hex(rows[0])))))
        assertEquals("row_0:candidate_id", PaperMenuCapture.readbackDifference(rows,
            rb(Triple("X", 1, PaperMenuCapture.sha256Hex(rows[0])), Triple("B", 2, PaperMenuCapture.sha256Hex(rows[1])))))
        assertEquals("row_0:generated_rank", PaperMenuCapture.readbackDifference(rows,
            rb(Triple("A", 3, PaperMenuCapture.sha256Hex(rows[0])), Triple("B", 2, PaperMenuCapture.sha256Hex(rows[1])))))
    }

    @Test fun missedRowCarriesNullCountsAndEmptyDigest() {
        val m = JSONObject().put("menu_id", "pdm_x").put("session_date", "2026-10-09").put("capture_policy_version", "p")
        val row = PaperMenuCapture.missedRow(m, "abc")
        listOf("generated_count", "captured_count", "duplicate_count").forEach { assertTrue(row.isNull(it)) }
        assertEquals(PaperMenuCapture.EMPTY_CONTENT_SHA256, row.getString("content_sha256"))
    }
}
