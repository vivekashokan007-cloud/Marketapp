package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.time.Instant
import java.time.ZoneId

/**
 * Codex reply 20 Nov 2026: compact, bounded, privacy-safe on-device trace of the single-leg Paper path on the Trade
 * tab (configuration -> decision/row rendered -> tap -> exact-contract quote -> outcome / restart recovery), per index.
 *
 *  - One file per IST session: files/single_leg_trace_v1/<yyyy-MM-dd>.json (schema mr.single_leg_trace.v1).
 *    session_evidence_v1 exports it as the OPTIONAL part `single_leg_trace`, attached after the released parts.
 *  - Allow-list only: every record is rebuilt from FIELDS (strings clipped, enums checked).  Anything else the page
 *    sends -- capital amounts, tokens, account details, quote bodies -- is dropped, never stored.
 *  - Bounded: MAX_RECORDS / MAX_BYTES.  Routine RENDER records are deduplicated per index (same identity and display
 *    state) and evicted FIRST when the cap is reached; tap / quote / recovery / outcome records are kept until no
 *    RENDER or CONFIG record is left to evict.  Every eviction is counted (dropped_renders / dropped_other) and the
 *    document is marked truncated.
 *  - Pure Kotlin (no Android types) so it is JVM-testable; the bridge passes context.filesDir.
 */
class SingleLegTradeTraceStore(
    private val filesDir: File,
    private val clock: () -> Long = { System.currentTimeMillis() },
    private val maxRecords: Int = MAX_RECORDS,
    private val maxBytes: Int = MAX_BYTES,
) {
    companion object {
        const val SCHEMA = "mr.single_leg_trace.v1"
        const val DIR = "single_leg_trace_v1"
        const val MAX_RECORDS = 1500
        const val MAX_BYTES = 384 * 1024
        const val KEEP_SESSIONS = 7
        private val GLOBAL_LOCK = Any()
        private const val STR_MAX = 120
        private val IST: ZoneId = ZoneId.of("Asia/Kolkata")
        val KINDS = setOf("CONFIG", "RENDER", "TAP", "REFUSED", "SUBMITTED", "OUTCOME", "RECOVERY_STARTED",
            "RECOVERY_BRIDGE_WAIT", "RECOVERY_OUTCOME", "N_SUBMIT", "N_QUOTE_STARTED", "N_QUOTE_RESULT", "N_ADMISSION_PENDING", "N_READINESS", "N_EXIT", "N_OUTCOME", "PAGE_QUEUE_LOSS")
        private val INDEXES = setOf("NF", "BNF")
        private val STRING_FIELDS = listOf("source", "app", "pwa", "session_date", "index_selector", "decision_id",
            "instrument_key", "row_state", "branch", "index_action", "request_id", "phase", "status", "reason",
            "origin", "stage", "action", "pending_request_id", "outcome_status", "source_poll_ts", "acquisition_id", "position_id")
        private val BOOL_FIELDS = listOf("enabled", "capital_set", "paper_mode", "trade_visible", "action_exposed",
            "quote_requested", "requires_new_tap", "reconciled", "recovered", "bridge_ready")
        private val LIST_FIELDS = listOf("reasons")
        /** RENDER records with the same per-index display key are routine churn. */
        private val RENDER_KEY = listOf("decision_id", "instrument_key", "trade_visible", "row_state", "action_exposed",
            "branch", "reason", "reasons", "index_action", "pending_request_id")
        private val CONFIG_KEY = listOf("app", "pwa", "session_date", "enabled", "index_selector", "capital_set", "paper_mode")
        private val ID_RE = Regex("""^[A-Za-z0-9_|:.\-]{1,120}$""")
        private val TOKENISH = Regex("""(?i)(bearer|token|secret|password|eyJ[A-Za-z0-9_-]{8,})""")
    }

    private val lock = GLOBAL_LOCK

    fun sessionDate(ms: Long): String = Instant.ofEpochMilli(ms).atZone(IST).toLocalDate().toString()

    private fun clip(v: Any?): String? {
        if (v == null || v == JSONObject.NULL) return null
        val s = v.toString().trim()
        if (s.isEmpty()) return null
        if (TOKENISH.containsMatchIn(s)) return "[REDACTED]"
        return s.take(STR_MAX)
    }

    /** The allow-listed copy of one page/native record, or null when it is not a trace record. */
    fun sanitize(raw: JSONObject, nowMs: Long): JSONObject? {
        val kind = raw.optString("kind", "")
        if (kind !in KINDS) return null
        val out = JSONObject().put("kind", kind).put("t_ms", raw.optLong("t_ms", nowMs).takeIf { it > 0 } ?: nowMs)
        if (kind == "PAGE_QUEUE_LOSS") {
            val count = raw.opt("dropped_count") as? Int ?: return null
            return out.takeIf { count > 0 }?.put("dropped_count", count)
        }
        val ix = raw.optString("index", "").uppercase()
        if (ix in INDEXES) out.put("index", ix)
        for (k in STRING_FIELDS) {
            val s = clip(raw.opt(k)) ?: continue
            if ((k == "decision_id" || k == "instrument_key" || k == "request_id" || k == "pending_request_id") && !ID_RE.matches(s)) {
                out.put(k, "[INVALID]"); continue
            }
            out.put(k, s)
        }
        for (k in listOf("http_status", "request_start_ms", "receipt_ms", "receipt_mono_ms", "runtime_ms")) {
            val n = raw.opt(k) as? Number
            if (n != null && n.toDouble().isFinite() && n.toLong() >= 0) out.put(k, n.toLong())
        }
        for (k in BOOL_FIELDS) if (raw.has(k) && !raw.isNull(k)) {
            val b = raw.opt(k); if (b is Boolean) out.put(k, b)
        }
        for (k in LIST_FIELDS) raw.optJSONArray(k)?.let { a ->
            val l = JSONArray(); for (i in 0 until minOf(a.length(), 8)) clip(a.opt(i))?.let { l.put(it) }
            out.put(k, l)
        }
        return out
    }

    private fun file(session: String): File = File(File(filesDir, DIR), "$session.json")

    private fun empty(session: String) = JSONObject().put("schema", SCHEMA).put("session_date", session)
        .put("records", JSONArray()).put("dropped_renders", 0).put("dropped_other", 0).put("deduped_renders", 0)
        .put("rejected_records", 0).put("truncated", false)
        .put("limits", JSONObject().put("max_records", maxRecords).put("max_bytes", maxBytes))
        .put("privacy", "allow-listed fields only; no capital amount, credential, account detail or quote body")

    private fun read(session: String): JSONObject {
        val f = file(session)
        if (!f.isFile) return empty(session)
        return try {
            val d = JSONObject(f.readText())
            if (d.optString("schema") != SCHEMA || d.optJSONArray("records") == null) empty(session).put("reset_unreadable", true) else d
        } catch (e: Exception) { empty(session).put("reset_unreadable", true) }
    }

    private fun write(session: String, doc: JSONObject) {
        val dir = File(filesDir, DIR)
        if (!dir.isDirectory) dir.mkdirs()
        val tmp = File(dir, "$session.json.tmp")
        try {
            tmp.writeText(doc.toString())
            check(tmp.renameTo(file(session))) { "Single-leg trace write failed" }
        } finally {
            tmp.delete()
        }
        prune(dir)
    }

    private fun prune(dir: File) {
        val days = dir.listFiles { f -> f.name.matches(Regex("""\d{4}-\d{2}-\d{2}\.json""")) }?.sortedBy { it.name } ?: return
        if (days.size > KEEP_SESSIONS) days.take(days.size - KEEP_SESSIONS).forEach {
            if (CloudArchive.preserveBeforeDiscard(it)) it.delete()
        }
    }

    private val pendingDeduped = HashMap<String, Int>()
    private fun key(r: JSONObject, fields: List<String>) = fields.joinToString("\u0001") { r.opt(it)?.toString() ?: "" }

    /** Append one record (page JSON or native).  Returns {ok, status, records, truncated}.  Never throws. */
    fun append(json: String): String = try {
        val now = clock()
        val raw = JSONObject(json.take(4096))
        synchronized(lock) {
            val rec = sanitize(raw, now)
            val session = sessionDate(rec?.optLong("t_ms", now) ?: now)
            val doc = read(session)
            if (rec == null) {
                doc.put("rejected_records", doc.optInt("rejected_records") + 1); write(session, doc)
                return@synchronized JSONObject().put("ok", false).put("status", "REJECTED_NOT_A_TRACE_RECORD").toString()
            }
            val recs = doc.getJSONArray("records")
            val kind = rec.getString("kind")
            if (kind == "RENDER" || kind == "CONFIG") {
                // dedup against the latest record of the same kind for the same index (no file write for a dup)
                val fields = if (kind == "RENDER") RENDER_KEY else CONFIG_KEY
                for (i in recs.length() - 1 downTo 0) {
                    val o = recs.getJSONObject(i)
                    if (o.optString("kind") != kind || o.optString("index") != rec.optString("index")) continue
                    if (key(o, fields) == key(rec, fields)) {
                        pendingDeduped[session] = (pendingDeduped[session] ?: 0) + 1
                        return@synchronized JSONObject().put("ok", true).put("status", "DEDUPED").put("records", recs.length()).toString()
                    }
                    break
                }
            }
            pendingDeduped[session]?.let { doc.put("deduped_renders", doc.optInt("deduped_renders") + it) }
            if (kind == "PAGE_QUEUE_LOSS") {
                doc.put("page_queue_dropped", doc.optLong("page_queue_dropped") + rec.getInt("dropped_count"))
                doc.put("truncated", true)
            } else {
                recs.put(rec)
            }
            enforceCap(doc)
            write(session, doc)
            pendingDeduped.remove(session)
            JSONObject().put("ok", true).put("status", "APPENDED").put("records", doc.getJSONArray("records").length())
                .put("truncated", doc.optBoolean("truncated")).toString()
        }
    } catch (t: Throwable) {
        try { JSONObject().put("ok", false).put("status", "TRACE_ERROR").put("error", t.javaClass.simpleName).toString() }
        catch (t2: Throwable) { "{\"ok\":false,\"status\":\"TRACE_ERROR\"}" }
    }

    private fun bytes(doc: JSONObject) = doc.toString().toByteArray(Charsets.UTF_8).size

    /** Evict oldest RENDER, then oldest CONFIG, then oldest other record, until both caps hold. */
    internal fun enforceCap(doc: JSONObject) {
        var recs = doc.getJSONArray("records")
        while (recs.length() > maxRecords || bytes(doc) > maxBytes) {
            var victim = -1
            for (i in 0 until recs.length()) if (recs.getJSONObject(i).optString("kind") == "RENDER") { victim = i; break }
            val isRender = victim >= 0
            if (victim < 0) {
                val configs = (0 until recs.length()).filter { recs.getJSONObject(it).optString("kind") == "CONFIG" }
                victim = if (configs.size > 1) configs.first() else 0      // keep the latest configuration
            }
            if (recs.length() == 0) break
            val kept = JSONArray()
            for (i in 0 until recs.length()) if (i != victim) kept.put(recs.get(i))
            doc.put("records", kept); recs = kept
            if (isRender) doc.put("dropped_renders", doc.optInt("dropped_renders") + 1)
            else doc.put("dropped_other", doc.optInt("dropped_other") + 1)
            doc.put("truncated", true)
        }
    }

    /** Read-only copy of one session's document (null when absent). */
    fun readSession(session: String): JSONObject? = synchronized(lock) { if (file(session).isFile) read(session) else null }
}
