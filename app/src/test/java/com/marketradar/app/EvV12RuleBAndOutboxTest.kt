package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/** Implied-EV v1.2: Kotlin Rule B parity with the Python reference vectors, ledger, outbox, ctx gating. */
class EvV12RuleBAndOutboxTest {
    private fun root(): File = listOf(File("."), File("app")).first { File(it, "src/test/resources/ev_v12").isDirectory }

    private fun vectors(): JSONObject = JSONObject(File(root(), "src/test/resources/ev_v12/rule_b_vectors.json").readText())

    @Test
    fun ruleBMatchesPythonVectors() {
        val v = vectors()
        assertEquals(RuleBExitPolicy.POLICY_ID, v.getString("policy_id"))
        val arr = v.getJSONArray("vectors")
        assertTrue(arr.length() >= 20)
        for (i in 0 until arr.length()) {
            val c = arr.getJSONObject(i)
            val got = RuleBExitPolicy.stepToJson(RuleBExitPolicy.step(
                RuleBExitPolicy.positionFromJson(c.getJSONObject("position")),
                RuleBExitPolicy.tickFromJson(c.getJSONObject("tick"))))
            val exp = c.getJSONObject("expect")
            val name = c.getString("name")
            assertEquals(name, exp.opt("state"), got.opt("state"))
            assertEquals(name, exp.getString("action"), got.getString("action"))
            assertEquals(name, exp.getString("reason"), got.getString("reason"))
            if (exp.has("fill")) {
                val ef = exp.getJSONObject("fill"); val gf = got.getJSONObject("fill")
                assertEquals(name, ef.getString("kind"), gf.getString("kind"))
                assertEquals(name, ef.getString("ts_iso"), gf.getString("ts_iso"))
                assertEquals(name, ef.getDouble("close_value_pts"), gf.getDouble("close_value_pts"), 1e-9)
                val el = ef.getJSONArray("legs"); val gl = gf.getJSONArray("legs")
                assertEquals(el.length(), gl.length())
                for (j in 0 until el.length()) {
                    assertEquals(el.getJSONObject(j).getDouble("price"), gl.getJSONObject(j).getDouble("price"), 0.0)
                    assertEquals(el.getJSONObject(j).getString("instrument_key"), gl.getJSONObject(j).getString("instrument_key"))
                }
            } else {
                assertFalse(name, got.has("fill"))
            }
        }
    }

    @Test
    fun holidaysMatchPythonCalendar() {
        val py = File(root(), "src/main/python/ev_v12_calendar.py").readText()
        val block = py.substringAfter("NSE_HOLIDAYS_2026 = (").substringBefore(")")
        val dates = Regex("'(\\d{4}-\\d{2}-\\d{2})'").findAll(block).map { it.groupValues[1] }.toSet()
        assertEquals(dates, RuleBExitPolicy.NSE_HOLIDAYS_2026)
    }

    @Test
    fun ledgerIsIdempotentAndTerminalNeverChanges() {
        val dir = Files.createTempDirectory("rb").toFile()
        val fill = RuleBExitPolicy.Step("EXIT_FILLED", "FILL", "PLANNED_EXIT",
            RuleBExitPolicy.Fill("PLANNED_EXIT", "2026-10-12T15:15:00+05:30",
                listOf(RuleBExitPolicy.FillLeg("S", -1, 10.5, 10.0, 10.5, 1.0)), -10.5))
        assertTrue(RuleBFillLedger.record(dir, "t1", "ev12:NF:2026-10-08:abc", RuleBExitPolicy.Step("LATE_EXIT_PENDING", "MARK_LATE", "x"), 1))
        assertFalse(RuleBFillLedger.record(dir, "t1", null, RuleBExitPolicy.Step("LATE_EXIT_PENDING", "MARK_LATE", "x"), 2))
        assertTrue(RuleBFillLedger.record(dir, "t1", "ev12:NF:2026-10-08:abc", fill, 3))
        assertFalse(RuleBFillLedger.record(dir, "t1", null, RuleBExitPolicy.Step("UNRESOLVED", "MARK_UNRESOLVED", "y"), 4))
        assertEquals("EXIT_FILLED", RuleBFillLedger.stateOf(dir, "t1"))
        assertEquals(1, RuleBFillLedger.pending(dir).length())
        assertEquals(1L, RuleBFillLedger.pending(dir).getJSONObject(0).getLong("late_since_ms"))
        assertTrue(RuleBFillLedger.ack(dir, "t1", 5))
        assertEquals(0, RuleBFillLedger.pending(dir).length())
        assertFalse(RuleBFillLedger.record(dir, "t1", null, RuleBExitPolicy.Step("NONE", "NONE", "z"), 6))
    }

    @Test
    fun ruleBTradeDetectionIsPaperOnly() {
        val es = JSONObject().put("position_policy_id", "paper_rule_b_v1")
        assertTrue(RuleBExitPolicy.isRuleBTrade(JSONObject().put("paper", true).put("entry_snapshot", es)))
        assertTrue(RuleBExitPolicy.isRuleBTrade(JSONObject().put("paper", true).put("entry_snapshot", es.toString())))
        assertFalse(RuleBExitPolicy.isRuleBTrade(JSONObject().put("paper", false).put("entry_snapshot", es)))
        assertFalse(RuleBExitPolicy.isRuleBTrade(JSONObject().put("paper", true).put("entry_snapshot", "{bad")))
    }

    @Test
    fun outboxRemovesOnlyOnExactAck() {
        val dir = Files.createTempDirectory("evo").toFile()
        val text = "{\"decision_id\":\"ev12:NF:2026-10-08:0123456789abcdef\"}"
        val sha = EvTelemetryOutbox.sha256(text)
        val id = "ev12:NF:2026-10-08:0123456789abcdef"
        assertEquals("REFUSED_DIGEST_MISMATCH", EvTelemetryOutbox.enqueue(dir, "decision", id, text, "0".repeat(64), 1))
        assertEquals("REFUSED_OVERSIZE", EvTelemetryOutbox.enqueue(dir, "outcome", id, "x".repeat(4097),
            EvTelemetryOutbox.sha256("x".repeat(4097)), 1))
        assertEquals("QUEUED", EvTelemetryOutbox.enqueue(dir, "decision", id, text, sha, 1))
        assertEquals("ALREADY_QUEUED", EvTelemetryOutbox.enqueue(dir, "decision", id, text, sha, 2))
        var r = EvTelemetryOutbox.drain(dir, 10) { EvTelemetryOutbox.Ack("ACCEPTED", id, "f".repeat(64)) }
        assertEquals(1, r.retry); assertEquals(1, EvTelemetryOutbox.entries(dir).size)
        r = EvTelemetryOutbox.drain(dir, 11) { null }
        assertEquals(1, r.retry); assertEquals(2, EvTelemetryOutbox.entries(dir)[0].attempts)
        r = EvTelemetryOutbox.drain(dir, 12) { EvTelemetryOutbox.Ack("DUPLICATE", id, sha) }
        assertEquals(1, r.acked); assertEquals(0, EvTelemetryOutbox.entries(dir).size)
        assertEquals("ALREADY_ACKED", EvTelemetryOutbox.enqueue(dir, "decision", id, text, sha, 13))
        // conflict is kept aside, never retried
        val t2 = "{\"o\":1}"; val s2 = EvTelemetryOutbox.sha256(t2)
        EvTelemetryOutbox.enqueue(dir, "outcome", id, t2, s2, 14)
        r = EvTelemetryOutbox.drain(dir, 15) { EvTelemetryOutbox.Ack("CONFLICT", null, null) }
        assertEquals(1, r.conflicts); assertEquals(0, EvTelemetryOutbox.entries(dir).size)
    }

    @Test
    fun outboxIsFiniteAndAges() {
        val dir = Files.createTempDirectory("evf").toFile()
        for (i in 0 until EvTelemetryOutbox.MAX_ENTRIES) {
            val t = "{\"i\":$i}"
            assertEquals("QUEUED", EvTelemetryOutbox.enqueue(dir, "outcome", "id$i", t, EvTelemetryOutbox.sha256(t), 0))
        }
        val t = "{\"i\":-1}"
        assertEquals("REFUSED_OUTBOX_FULL", EvTelemetryOutbox.enqueue(dir, "outcome", "x", t, EvTelemetryOutbox.sha256(t), 0))
        val r = EvTelemetryOutbox.drain(dir, EvTelemetryOutbox.MAX_AGE_MS + 1) { null }
        assertEquals(EvTelemetryOutbox.MAX_ENTRIES, r.dropped)
        assertEquals(0, EvTelemetryOutbox.entries(dir).size)
    }

    @Test
    fun ctxKeysOnlyForPaperAndEnabled() {
        val dir = Files.createTempDirectory("evc").toFile()
        for ((mode, exec) in listOf("active" to "live", "active" to "sandbox", "off" to "paper", "active" to "")) {
            val c = JSONObject().put("executionMode", exec)
            assertFalse(EvExperiment.applyContext(c, mode, exec, dir, 1L, 1L, null, "2.6.73"))
            assertEquals(1, c.length())
        }
        val c = JSONObject().put("executionMode", "paper")
        assertTrue(EvExperiment.applyContext(c, "observe", "paper", dir, 5L, 4L, "{\"schema\":\"x\"}", "2.6.73"))
        assertEquals("observe", c.getString("evExperimentMode"))
        assertEquals(4L, c.getLong("evQuoteFetchedAtMs"))
        assertTrue(c.has("evCalibration"))
    }

    @Test
    fun detachMovesEnvelopesToOutbox() {
        val dir = Files.createTempDirectory("evd").toFile()
        val text = "{\"a\":\"1\"}"
        val res = JSONObject().put(EvExperiment.RESULT_KEY, JSONObject().put("status", "DECIDED")
            .put("pending_envelopes", org.json.JSONArray().put(JSONObject().put("decision_id", "d").put("envelope_text", text)
                .put("envelope_sha256", EvTelemetryOutbox.sha256(text)))))
        val d = EvExperiment.detach(res, dir, 1)
        assertEquals(listOf("QUEUED"), d.queued)
        assertFalse(res.getJSONObject(EvExperiment.RESULT_KEY).has("pending_envelopes"))
        assertEquals(1, res.getJSONObject(EvExperiment.RESULT_KEY).getInt("outbox_pending"))
    }
}
