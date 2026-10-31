package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.time.LocalDate
import java.time.LocalDateTime

/**
 * M0 cost measurement on the JVM (box), NOT on a device: bytes written by one full collector day (2 indices x 78
 * slots, one attempt each, 78 envelopes, one instrument-map version per index) and the wall time of the collector
 * calls. Chain fixture: 131 strikes per index (~262 legs, matching A2's ~522 rows per poll for both indices).
 * Writes build/m0_cost.json for the review packet. Bounds are asserted loosely (budget target 100 KB/session raw
 * is REPORTED, not asserted).
 */
class M0CollectorCostMeasurementTest {
    private fun chain(index: String, expiry: String, n: Int, base: Int, step: Int): JSONObject {
        val data = JSONArray()
        for (i in 0 until n) {
            val k = base + i * step
            fun leg(t: String) = JSONObject().put("instrument_key", "NSE_FO|${index}${expiry.replace("-", "")}$k$t")
                .put("market_data", JSONObject().put("ltp", 101.5).put("bid_price", 101.0).put("ask_price", 102.0).put("oi", 12345))
            data.put(JSONObject().put("strike_price", k.toDouble()).put("expiry", expiry).put("call_options", leg("CE")).put("put_options", leg("PE")))
        }
        return JSONObject().put("status", "success").put("data", data)
    }

    @Test fun measure_full_day() {
        val dir = Files.createTempDirectory("m0cost").toFile()
        val day = "2026-10-22"
        fun ms(h: Int, m: Int, s: Int = 0) = LocalDateTime.of(LocalDate.parse(day), java.time.LocalTime.of(h, m, s)).atZone(ResearchCalendar.IST).toInstant().toEpochMilli()
        ResearchCollector.setEnabled(dir, true, ms(9, 0))
        val nf = chain("NF", "2026-10-27", 131, 21000, 50); val bnf = chain("BNF", "2026-10-28", 131, 48000, 100)
        val raw = JSONObject().put("schema", "research_m0_raw_forecast_v1").put("status", "NEUTRAL").put("direction", "NEUTRAL")
            .put("confidence", 41.0).put("horizon", "UNDECLARED").put("brain_version", "2.6.85")
            .put("raw", JSONObject().put("action", "WAIT").put("strategy", JSONObject.NULL).put("direction", "NEUTRAL")
                .put("market_direction", "NEUTRAL").put("confidence", 41).put("market_confidence", 41)
                .put("market_bull_score", 1.2).put("market_bear_score", 1.1).put("market_view_status", "AVAILABLE")
                .put("market_view_source", "synthesize_verdict_score_rule_v1").put("market_direction_scope", "COMBINED_NF_BNF_CONTEXT"))
        val t: MutableMap<String, Long> = mutableMapOf("begin" to 0L, "capture" to 0L, "envelope" to 0L, "reconcile" to 0L, "status" to 0L)
        fun timed(k: String, f: () -> Unit) { val a = System.nanoTime(); f(); t[k] = t[k]!! + (System.nanoTime() - a) }
        // warm-up (JIT) on a scratch dir
        val warm = Files.createTempDirectory("m0warm").toFile(); ResearchCollector.setEnabled(warm, true, ms(9, 0))
        repeat(5) { ResearchCollector.captureFromChain(warm, "NF", nf, 1, 2, "ml_option_chain_snapshots", 3, "x", 4) }
        warm.deleteRecursively()
        val b0 = M0Durable.bytesWritten; val w0 = M0Durable.writes
        for (s in ResearchCalendar.slotGrid(LocalDate.parse(day))) {
            val now = s.nominalMs + 5_000
            for ((ix, ch) in listOf("NF" to nf, "BNF" to bnf)) {
                var st: ResearchCollector.Begin.Started? = null
                timed("begin") { st = ResearchCollector.beginAttempt(dir, ix, "$day|${s.ordinal}", now) as ResearchCollector.Begin.Started }
                timed("capture") {
                    val c = ResearchCollector.captureFromChain(dir, ix, ch, now, now + 900, "ml_option_chain_snapshots", now + 1500, "ts", now + 1500)
                    ResearchCollector.recordCapture(dir, st!!.slotId, st!!.attemptId, c, now + 1600)
                }
            }
            timed("envelope") {
                ResearchCollector.putEnvelope(dir, ResearchCollector.buildEnvelope("$day|${s.ordinal}", JSONObject(raw.toString()).put("input_last_poll_slot_key", "$day|${s.ordinal}"),
                    now + 2000, now + 9000, "2.6.85", 513, JSONObject().put("execution_mode", "paper").put("trade_mode", "swing")), now + 9100)
            }
        }
        timed("reconcile") { ResearchCollector.reconcile(dir, ms(16, 0)) }
        timed("status") { ResearchCollector.status(dir, ms(16, 0)) }
        val sizes = JSONObject()
        var total = 0L
        dir.listFiles()!!.groupBy { it.name.substringBefore("_").substringBefore(".") }.forEach { (k, fs) ->
            val b = fs.sumOf { it.length() }; total += b; sizes.put(k, JSONObject().put("files", fs.size).put("bytes", b))
        }
        val slots = 2 * ResearchCalendar.slotGrid(LocalDate.parse(day)).size
        val out = JSONObject().put("measured_on", "JVM unit test on review box (NOT a device)").put("slots", slots)
            .put("envelopes", ResearchCalendar.slotGrid(LocalDate.parse(day)).size)
            .put("strikes_per_index", 131).put("bytes_by_kind", sizes).put("bytes_total_session", total)
            .put("ms_total_by_call", JSONObject(t.mapValues { it.value / 1_000_000.0 }))
            .put("ms_per_slot_capture_avg", t["capture"]!! / 1_000_000.0 / slots)
            .put("heap_used_mb_after", (Runtime.getRuntime().totalMemory() - Runtime.getRuntime().freeMemory()) / 1048576)
            .put("extra_network_requests_per_poll", 0)
            .put("bytes_written_session_incl_rewrites", M0Durable.bytesWritten - b0).put("fsynced_writes_session", M0Durable.writes - w0)
        File("build").mkdirs(); File("build/m0_cost.json").writeText(out.toString(1))
        println("M0_COST " + out)
        assertTrue(total < 2_000_000)
        dir.deleteRecursively()
    }
}
