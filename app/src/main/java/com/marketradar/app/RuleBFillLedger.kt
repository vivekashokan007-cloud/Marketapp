package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * Durable, bounded ledger of Rule B state per Paper trade (filesDir/ev_v12/rule_b_ledger.json).
 *
 * The 60-second PositionTickService records the first FILL / MARK_LATE / MARK_UNRESOLVED per trade here
 * (atomic temp+rename). The PWA consumes fills (NativeBridge.evRuleBPending), closes the Paper trade
 * idempotently with exit_reason RULE_B_PLANNED_EXIT / RULE_B_LATE_EXIT at the frozen fill prices, then
 * acknowledges (NativeBridge.evRuleBAck). A terminal entry is never overwritten. Bounded: at most
 * MAX_ENTRIES, acknowledged entries older than RETAIN_MS are pruned.
 */
object RuleBFillLedger {
    const val SCHEMA = "ev_v12_rule_b_ledger_v1"
    const val MAX_ENTRIES = 50
    const val RETAIN_MS = 14L * 24 * 3600 * 1000
    private val lock = Any()

    fun file(dir: File) = File(dir, "rule_b_ledger.json")

    fun load(dir: File): JSONObject = synchronized(lock) {
        val f = file(dir)
        if (!f.exists()) return JSONObject().put("schema", SCHEMA).put("trades", JSONObject())
        return try { JSONObject(f.readText()) } catch (e: Exception) {
            // Never overwrite an unreadable ledger silently: keep it aside and start an empty one.
            f.renameTo(File(dir, "rule_b_ledger.corrupt.${System.currentTimeMillis()}.json"))
            JSONObject().put("schema", SCHEMA).put("trades", JSONObject()).put("recovered_from_corrupt", true)
        }
    }

    private fun save(dir: File, doc: JSONObject) {
        dir.mkdirs()
        val tmp = File(dir, "rule_b_ledger.json.tmp")
        tmp.writeText(doc.toString())
        if (!tmp.renameTo(file(dir))) {
            file(dir).delete(); tmp.renameTo(file(dir))
        }
    }

    fun stateOf(dir: File, tradeId: String): String? =
        load(dir).optJSONObject("trades")?.optJSONObject(tradeId)?.optString("state")?.ifBlank { null }

    /** Apply one policy step; returns true when the ledger changed. */
    fun record(dir: File, tradeId: String, decisionId: String?, step: RuleBExitPolicy.Step, nowMs: Long): Boolean =
        synchronized(lock) {
            if (step.action == "NONE") return false
            val doc = load(dir)
            val trades = doc.optJSONObject("trades") ?: JSONObject().also { doc.put("trades", it) }
            val cur = trades.optJSONObject(tradeId)
            val curState = cur?.optString("state")
            if (curState == "EXIT_FILLED" || curState == "UNRESOLVED") return false
            if (step.action == "MARK_LATE" && curState == "LATE_EXIT_PENDING") return false
            val e = JSONObject().put("trade_id", tradeId).put("decision_id", decisionId ?: JSONObject.NULL)
                .put("policy_id", RuleBExitPolicy.POLICY_ID).put("state", step.state).put("reason", step.reason)
                .put("recorded_at_ms", nowMs).put("acked", false)
            if (cur?.has("late_since_ms") == true) e.put("late_since_ms", cur.optLong("late_since_ms"))
            if (step.action == "MARK_LATE") e.put("late_since_ms", nowMs)
            step.fill?.let { e.put("fill", RuleBExitPolicy.stepToJson(step).getJSONObject("fill")) }
            trades.put(tradeId, e)
            prune(trades, nowMs)
            save(dir, doc)
            return true
        }

    /** Fills / unresolved marks the PWA has not acknowledged yet. */
    fun pending(dir: File): JSONArray = synchronized(lock) {
        val out = JSONArray()
        val trades = load(dir).optJSONObject("trades") ?: return out
        for (k in trades.keys()) {
            val e = trades.optJSONObject(k) ?: continue
            if (!e.optBoolean("acked") && e.optString("state") in setOf("EXIT_FILLED", "UNRESOLVED")) out.put(e)
        }
        return out
    }

    fun ack(dir: File, tradeId: String, nowMs: Long): Boolean = synchronized(lock) {
        val doc = load(dir)
        val e = doc.optJSONObject("trades")?.optJSONObject(tradeId) ?: return false
        if (e.optString("state") !in setOf("EXIT_FILLED", "UNRESOLVED")) return false
        e.put("acked", true).put("acked_at_ms", nowMs)
        save(dir, doc)
        return true
    }

    private fun prune(trades: JSONObject, nowMs: Long) {
        val keys = trades.keys().asSequence().toList()
        for (k in keys) {
            val e = trades.optJSONObject(k) ?: continue
            if (e.optBoolean("acked") && nowMs - e.optLong("acked_at_ms", nowMs) > RETAIN_MS) trades.remove(k)
        }
        if (trades.length() > MAX_ENTRIES) {
            trades.keys().asSequence().toList()
                .filter { trades.optJSONObject(it)?.optBoolean("acked") == true }
                .sortedBy { trades.optJSONObject(it)?.optLong("recorded_at_ms") ?: 0L }
                .take(trades.length() - MAX_ENTRIES)
                .forEach { trades.remove(it) }
        }
    }
}
