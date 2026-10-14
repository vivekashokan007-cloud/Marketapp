package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * Durable, bounded Rule B close journal per Paper trade (filesDir/ev_v12/rule_b_ledger.json), Codex R3.
 *
 * Policy state (state): OPEN -> LATE_EXIT_PENDING -> EXIT_FILLED | UNRESOLVED, or MANUAL_CLOSED (user closed it
 * first). Terminal policy states never change.
 *
 * Delivery state (delivery) for a terminal entry:
 *   PENDING_APPLY      the PWA has not applied it yet (offered by pending()).
 *   REMOTE_PENDING     the PWA journaled the exact close patch here (journal()) and is writing it remotely; still
 *                      offered by pending() WITH the journaled patch, so a restart/WebView reload retries the
 *                      SAME idempotent patch.
 *   REMOTE_CONFIRMED   the remote trades_v2 row was read back with the patch applied (ack()).
 *   MANUAL_CLOSE_CONFLICT  the conditional remote update found the row already closed by a manual close.
 * Only REMOTE_CONFIRMED / MANUAL_CLOSE_CONFLICT stop the offer. A trade missing from the PWA's open-trade cache
 * is NOT proof of closure. Bounded: undelivered entries are never pruned (they are limited by the max-two
 * accepted positions); delivered entries are pruned after RETAIN_MS and beyond MAX_ENTRIES.
 */
object RuleBFillLedger {
    const val SCHEMA = "ev_v12_rule_b_ledger_v2"
    const val MAX_ENTRIES = 50
    const val RETAIN_MS = 14L * 24 * 3600 * 1000
    const val MAX_PATCH_CHARS = 16 * 1024
    const val PENDING_APPLY = "PENDING_APPLY"
    const val REMOTE_PENDING = "REMOTE_PENDING"
    const val REMOTE_CONFIRMED = "REMOTE_CONFIRMED"
    const val MANUAL_CLOSE_CONFLICT = "MANUAL_CLOSE_CONFLICT"
    val TERMINAL = setOf("EXIT_FILLED", "UNRESOLVED", "MANUAL_CLOSED")
    val DELIVERED = setOf(REMOTE_CONFIRMED, MANUAL_CLOSE_CONFLICT)
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
        doc.put("schema", SCHEMA)
        val tmp = File(dir, "rule_b_ledger.json.tmp")
        tmp.writeText(doc.toString())
        if (!tmp.renameTo(file(dir))) {
            file(dir).delete(); tmp.renameTo(file(dir))
        }
    }

    private fun trades(doc: JSONObject) = doc.optJSONObject("trades") ?: JSONObject().also { doc.put("trades", it) }

    fun entry(dir: File, tradeId: String): JSONObject? = load(dir).optJSONObject("trades")?.optJSONObject(tradeId)

    fun stateOf(dir: File, tradeId: String): String? = entry(dir, tradeId)?.optString("state")?.ifBlank { null }

    /** Apply one policy step; returns true when the ledger changed. */
    fun record(dir: File, tradeId: String, decisionId: String?, step: RuleBExitPolicy.Step, nowMs: Long): Boolean =
        synchronized(lock) {
            if (step.action == "NONE") return false
            val doc = load(dir)
            val trades = trades(doc)
            val cur = trades.optJSONObject(tradeId)
            val curState = cur?.optString("state")
            if (curState in TERMINAL) return false
            if (step.action == "MARK_LATE" && curState == "LATE_EXIT_PENDING") return false
            val e = JSONObject().put("trade_id", tradeId).put("decision_id", decisionId ?: JSONObject.NULL)
                .put("policy_id", RuleBExitPolicy.POLICY_ID).put("state", step.state).put("reason", step.reason)
                .put("recorded_at_ms", nowMs)
            if (step.state in TERMINAL) e.put("delivery", PENDING_APPLY).put("delivery_updated_ms", nowMs)
            if (cur?.has("late_since_ms") == true) e.put("late_since_ms", cur.optLong("late_since_ms"))
            if (step.action == "MARK_LATE") e.put("late_since_ms", nowMs)
            step.fill?.let { e.put("fill", RuleBExitPolicy.stepToJson(step).getJSONObject("fill")) }
            trades.put(tradeId, e)
            prune(trades, nowMs)
            save(dir, doc)
            return true
        }

    /** Net economics computed at fill time (ev_v12_integration.rule_b_close_economics_json). */
    fun putEconomics(dir: File, tradeId: String, econ: JSONObject): Boolean = synchronized(lock) {
        val doc = load(dir)
        val e = trades(doc).optJSONObject(tradeId) ?: return false
        if (e.has("economics")) return false
        e.put("economics", econ)
        save(dir, doc)
        return true
    }

    /** Terminal entries not yet delivered (PENDING_APPLY, or REMOTE_PENDING with the journaled patch). */
    fun pending(dir: File): JSONArray = synchronized(lock) {
        val out = JSONArray()
        val trades = load(dir).optJSONObject("trades") ?: return out
        for (k in trades.keys()) {
            val e = trades.optJSONObject(k) ?: continue
            if (e.optString("state") in setOf("EXIT_FILLED", "UNRESOLVED") && e.optString("delivery") !in DELIVERED) out.put(e)
        }
        return out
    }

    /**
     * Journal the exact remote patch BEFORE writing it remotely. Idempotent for the same patch; a different
     * patch for an already journaled entry is refused (the first journaled patch is the one retried).
     */
    fun journal(dir: File, tradeId: String, patchJson: String, nowMs: Long): String = synchronized(lock) {
        if (patchJson.length > MAX_PATCH_CHARS) return "REFUSED_OVERSIZE"
        val patch = try { JSONObject(patchJson) } catch (e: Exception) { return "REFUSED_INVALID_JSON" }
        val doc = load(dir)
        val e = trades(doc).optJSONObject(tradeId) ?: return "UNKNOWN_TRADE"
        if (e.optString("state") !in setOf("EXIT_FILLED", "UNRESOLVED")) return "NOT_TERMINAL"
        when (e.optString("delivery")) {
            REMOTE_CONFIRMED, MANUAL_CLOSE_CONFLICT -> return "ALREADY_DELIVERED"
            REMOTE_PENDING -> return if (e.optJSONObject("journal_patch")?.toString() == patch.toString())
                "ALREADY_JOURNALED" else "JOURNALED_PATCH_DIFFERS"
        }
        e.put("delivery", REMOTE_PENDING).put("journal_patch", patch).put("journaled_ms", nowMs)
            .put("delivery_updated_ms", nowMs).put("remote_attempts", 0)
        save(dir, doc)
        return "JOURNALED"
    }

    /** Count a failed remote attempt (the entry stays REMOTE_PENDING and keeps being offered). */
    fun noteRemoteAttempt(dir: File, tradeId: String, nowMs: Long, detail: String?): Boolean = synchronized(lock) {
        val doc = load(dir)
        val e = trades(doc).optJSONObject(tradeId) ?: return false
        if (e.optString("delivery") != REMOTE_PENDING) return false
        e.put("remote_attempts", e.optInt("remote_attempts") + 1).put("last_remote_attempt_ms", nowMs)
            .put("last_remote_detail", (detail ?: "").take(120))
        save(dir, doc)
        return true
    }

    /** Remote persistence proven (REMOTE_CONFIRMED) or a manual close won (MANUAL_CLOSE_CONFLICT). */
    fun ack(dir: File, tradeId: String, deliveryState: String, nowMs: Long): Boolean = synchronized(lock) {
        if (deliveryState !in DELIVERED) return false
        val doc = load(dir)
        val e = trades(doc).optJSONObject(tradeId) ?: return false
        if (e.optString("state") !in setOf("EXIT_FILLED", "UNRESOLVED")) return false
        if (e.optString("delivery") != REMOTE_PENDING) return false   // must journal first
        e.put("delivery", deliveryState).put("delivery_updated_ms", nowMs).put("acked_at_ms", nowMs)
        save(dir, doc)
        return true
    }

    /**
     * The user closed a Rule B trade manually. Before any Rule B terminal state: MANUAL_CLOSED (Rule B stops).
     * If a Rule B fill is already terminal but not yet delivered, the manual close competes with it: the entry is
     * kept and the conditional remote update decides (the PWA reports MANUAL_CLOSE_CONFLICT or REMOTE_CONFIRMED).
     */
    fun noteManualClose(dir: File, tradeId: String, decisionId: String?, nowMs: Long): String = synchronized(lock) {
        val doc = load(dir)
        val trades = trades(doc)
        val cur = trades.optJSONObject(tradeId)
        val st = cur?.optString("state")
        if (st == "MANUAL_CLOSED") return "ALREADY_MANUAL"
        if (st == "EXIT_FILLED" || st == "UNRESOLVED") {
            cur!!.put("manual_close_seen_ms", nowMs)
            save(dir, doc)
            return "RULE_B_TERMINAL_PENDING_DELIVERY"
        }
        trades.put(tradeId, JSONObject().put("trade_id", tradeId).put("decision_id", decisionId ?: JSONObject.NULL)
            .put("policy_id", RuleBExitPolicy.POLICY_ID).put("state", "MANUAL_CLOSED").put("reason", "USER_MANUAL_CLOSE")
            .put("recorded_at_ms", nowMs).put("delivery", REMOTE_CONFIRMED).put("delivery_updated_ms", nowMs)
            .put("acked_at_ms", nowMs))
        prune(trades, nowMs)
        save(dir, doc)
        return "MANUAL_CLOSED"
    }

    fun prune(trades: JSONObject, nowMs: Long) {
        val keys = trades.keys().asSequence().toList()
        fun delivered(e: JSONObject?) = e != null && e.optString("delivery") in DELIVERED
        for (k in keys) {
            val e = trades.optJSONObject(k) ?: continue
            if (delivered(e) && nowMs - e.optLong("acked_at_ms", nowMs) > RETAIN_MS) trades.remove(k)
        }
        if (trades.length() > MAX_ENTRIES) {
            trades.keys().asSequence().toList()
                .filter { delivered(trades.optJSONObject(it)) }
                .sortedBy { trades.optJSONObject(it)?.optLong("recorded_at_ms") ?: 0L }
                .take(trades.length() - MAX_ENTRIES)
                .forEach { trades.remove(it) }
        }
    }
}
