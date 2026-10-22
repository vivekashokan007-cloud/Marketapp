package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.time.Instant
import java.time.LocalDate
import java.util.UUID

/**
 * M0 full-day research COLLECTOR (Codex corrected brief, Part B2-B6). Collection only.
 *
 * Owns, under filesDir/research_m0 (bounded, atomic tmp+rename writes):
 *   meta.json            enabled flag, collector start, failure counters.
 *   seq.json             persistent (epoch, next) fetch sequence; the epoch is a UUID minted once, and again only
 *                        if the file is lost/corrupt, so (epoch, seq) never collides with old data after restart.
 *   manifest_<date>.json scheduled slots per index for one session, created from ResearchCalendar BEFORE any
 *                        request; states PENDING/CAPTURED/LATE/PARTIAL/MISSING/UNAVAILABLE with reasons.
 *   imap_<sha>.json      immutable instrument-key maps; written and verified before any slot references them.
 *   env_<slot>.json      immutable raw forecast envelopes (one per poll slot; brain scope is combined NF+BNF).
 *   conflicts.json       bounded list of conflicting retries (originals are never overwritten).
 *   protection.json      bounded staging/protection ledger (owner, archive ack, lag/capacity limits, final states).
 *
 * Never: deletes chain rows, copies chains or brain payloads, changes ranking/gates/sizing/exits/teacher/Real,
 * touches EV v1.2 journals/calibration, or blocks the poll/evening evaluator (every runtime entry point is
 * wrapped by [Runtime.safely]; a collector failure only increments a visible counter).
 */
object ResearchCollector {
    const val DIR = "research_m0"
    const val SCHEMA = "research_m0_collector_v1"
    /** A slot not captured within this many ms of its nominal time is MISSING at reconcile (it stays linkable: a
     *  later capture of the same slot is recorded as LATE, never moved to another slot). */
    const val ON_TIME_MS = 5L * 60_000
    const val MISSING_AFTER_MS = 15L * 60_000
    const val MAX_MANIFEST_SESSIONS = 120
    const val MAX_IMAP_FILES = 400
    const val MAX_ENVELOPE_FILES = 120 * 80
    const val MAX_CONFLICTS = 200
    const val MAX_ATTEMPTS_PER_SLOT = 6
    const val MAX_RECONCILE_SESSIONS = 90
    /** Must equal research_forecast_capture.RESULT_KEY (Python); pinned by a test. */
    const val RAW_FORECAST_KEY = "research_forecast_raw"
    val INDICES_FOR_POLL = listOf("NF", "BNF")
    val STATES = listOf("PENDING", "CAPTURED", "LATE", "PARTIAL", "MISSING", "UNAVAILABLE")

    private val lock = Any()

    fun sha256(text: String): String = MessageDigest.getInstance("SHA-256").digest(text.toByteArray(Charsets.UTF_8))
        .joinToString("") { "%02x".format(it) }

    fun sessionOf(ms: Long): String = Instant.ofEpochMilli(ms).atZone(ResearchCalendar.IST).toLocalDate().toString()

    // ------------------------------------------------------------------ file helpers
    internal fun writeAtomic(f: File, text: String) {
        f.parentFile?.mkdirs()
        val tmp = File(f.parentFile, f.name + ".tmp")
        tmp.writeText(text)
        if (!tmp.renameTo(f)) { f.delete(); if (!tmp.renameTo(f)) throw IllegalStateException("RENAME_FAILED:${f.name}") }
    }

    private fun readJson(f: File): JSONObject? = try { if (f.exists()) JSONObject(f.readText()) else null } catch (_: Exception) { null }

    private fun safe(s: String) = s.replace(Regex("[^A-Za-z0-9_.-]"), "_").take(80)

    // ------------------------------------------------------------------ meta / sequence
    fun meta(dir: File): JSONObject = readJson(File(dir, "meta.json")) ?: JSONObject().put("schema", SCHEMA).put("enabled", false)

    fun setEnabled(dir: File, enabled: Boolean, nowMs: Long) = synchronized(lock) {
        val m = meta(dir)
        m.put("enabled", enabled)
        if (enabled && !m.has("collector_start_ms")) {
            m.put("collector_start_ms", nowMs).put("collector_start_session", sessionOf(nowMs))
        }
        m.put("enabled_changed_ms", nowMs)
        writeAtomic(File(dir, "meta.json"), m.toString())
    }

    fun isEnabled(dir: File) = meta(dir).optBoolean("enabled", false)

    fun recordFailure(dir: File, where: String, err: Throwable?) {
        try { synchronized(lock) {
            val m = meta(dir)
            val f = m.optJSONObject("failures") ?: JSONObject()
            f.put(where, f.optInt(where, 0) + 1)
            m.put("failures", f).put("last_failure", JSONObject().put("where", where)
                .put("error", (err?.javaClass?.simpleName ?: "unknown") + ":" + (err?.message ?: "").take(120)))
            writeAtomic(File(dir, "meta.json"), m.toString())
        } } catch (_: Throwable) { /* visible counter is best effort; never propagates */ }
    }

    data class Seq(val epoch: String, val seq: Long) { val id get() = "$epoch:$seq" }

    /** Persist the incremented counter BEFORE handing the number out. */
    fun nextSeq(dir: File): Seq = synchronized(lock) {
        val f = File(dir, "seq.json")
        val cur = readJson(f)
        val epoch = cur?.optString("epoch", "")?.takeIf { it.isNotBlank() } ?: UUID.randomUUID().toString()
        val next = if (cur != null && cur.optString("epoch") == epoch) cur.optLong("next", 1L) else 1L
        writeAtomic(f, JSONObject().put("epoch", epoch).put("next", next + 1).toString())
        Seq(epoch, next)
    }

    // ------------------------------------------------------------------ manifest
    fun slotId(index: String, session: String, ordinal: Int) = "$index|$session|$ordinal"
    private fun manifestFile(dir: File, session: String) = File(dir, "manifest_${safe(session)}.json")

    /** Create (idempotently) the scheduled manifest for one session from the calendar; never from returned data. */
    fun ensureManifest(dir: File, session: String, nowMs: Long, observed: Boolean = true): JSONObject = synchronized(lock) {
        val f = manifestFile(dir, session)
        readJson(f)?.let { return it }
        val m = JSONObject().put("schema", SCHEMA).put("session_date", session).put("created_ms", nowMs)
            .put("calendar_version", ResearchCalendar.VERSION).put("created_by", if (observed) "collector" else "reconcile_missed_session")
        val idx = JSONObject()
        for (index in ResearchCalendar.INDICES) {
            val p = ResearchCalendar.plan(session, index)
            val o = JSONObject().put("available", p.available).put("reason", p.reason ?: JSONObject.NULL)
                .put("is_session", p.isSession).put("expiry_authority", p.expiryAuthority)
                .put("rule_expiry", p.ruleExpiry ?: JSONObject.NULL).put("rule_expiry_day", p.ruleExpiryDay ?: JSONObject.NULL)
                .put("expected", p.slots.size)
            val slots = JSONObject()
            for (s in p.slots) {
                slots.put(s.ordinal.toString(), JSONObject().put("slot_id", slotId(index, session, s.ordinal))
                    .put("nominal", s.hhmm).put("nominal_ms", s.nominalMs).put("phase", s.phase)
                    .put("on_time_deadline_ms", s.nominalMs + ON_TIME_MS).put("missing_after_ms", s.nominalMs + MISSING_AFTER_MS)
                    .put("state", "PENDING").put("reason", JSONObject.NULL).put("attempts", JSONArray()))
            }
            o.put("slots", slots)
            idx.put(index, o)
        }
        m.put("indices", idx)
        writeAtomic(f, m.toString())
        pruneManifests(dir)
        m
    }

    fun readManifest(dir: File, session: String): JSONObject? = readJson(manifestFile(dir, session))

    private fun saveManifest(dir: File, m: JSONObject) = writeAtomic(manifestFile(dir, m.getString("session_date")), m.toString())

    private fun pruneManifests(dir: File) {
        val fs = dir.listFiles { f -> f.name.startsWith("manifest_") && f.name.endsWith(".json") }?.sortedBy { it.name } ?: return
        if (fs.size > MAX_MANIFEST_SESSIONS) fs.take(fs.size - MAX_MANIFEST_SESSIONS).forEach { it.delete() }
    }

    sealed class Begin {
        data class Started(val slotId: String, val attemptId: String, val attemptNo: Int) : Begin()
        data class Unavailable(val reason: String) : Begin()
    }

    /**
     * Persist the scheduled identity and the attempt (with its epoch+sequence) BEFORE the request is sent.
     * [pollSlotKey] is the poll's frozen slot key "yyyy-MM-dd|ordinal"; an unknown index is UNAVAILABLE.
     */
    fun beginAttempt(dir: File, index: String, pollSlotKey: String, nowMs: Long): Begin = synchronized(lock) {
        if (index !in ResearchCalendar.INDICES) return Begin.Unavailable("UNKNOWN_INDEX")
        val session = pollSlotKey.substringBefore("|")
        val ord = pollSlotKey.substringAfterLast("|", "").toIntOrNull() ?: return Begin.Unavailable("BAD_SLOT_KEY")
        val m = ensureManifest(dir, session, nowMs)
        val io = m.getJSONObject("indices").getJSONObject(index)
        if (!io.optBoolean("available") || !io.optBoolean("is_session")) return Begin.Unavailable(io.optString("reason", "NOT_A_SESSION"))
        val slot = io.getJSONObject("slots").optJSONObject(ord.toString()) ?: return Begin.Unavailable("SLOT_NOT_IN_CALENDAR")
        val attempts = slot.getJSONArray("attempts")
        if (attempts.length() >= MAX_ATTEMPTS_PER_SLOT) return Begin.Unavailable("ATTEMPT_CAP")
        val seq = nextSeq(dir)
        val no = attempts.length() + 1
        attempts.put(JSONObject().put("attempt_id", seq.id).put("epoch", seq.epoch).put("seq", seq.seq)
            .put("attempt_no", no).put("started_ms", nowMs).put("status", "STARTED"))
        saveManifest(dir, m)
        Begin.Started(slot.getString("slot_id"), seq.id, no)
    }

    data class Capture(
        val fetchStartMs: Long?, val fetchEndMs: Long?, val receiptMs: Long?, val persistedMs: Long?,
        val persistStatus: String,          // DURABLE_ACKED | LOCAL_ONLY | FAILED | NOT_FETCHED
        val chainRows: Int, val twoSidedRows: Int, val defectRows: Int,
        val liveExpiry: String?, val imapVersion: String?, val feedTimeStatus: String, // KNOWN | UNKNOWN
        val pollTs: String?
    ) {
        fun toJson(): JSONObject = JSONObject().put("fetch_start_ms", fetchStartMs ?: JSONObject.NULL)
            .put("fetch_end_ms", fetchEndMs ?: JSONObject.NULL).put("receipt_ms", receiptMs ?: JSONObject.NULL)
            .put("persisted_ms", persistedMs ?: JSONObject.NULL).put("persist_status", persistStatus)
            .put("chain_rows", chainRows).put("two_sided_rows", twoSidedRows).put("defect_rows", defectRows)
            .put("live_expiry", liveExpiry ?: JSONObject.NULL).put("imap_version", imapVersion ?: JSONObject.NULL)
            .put("feed_time", feedTimeStatus).put("poll_ts", pollTs ?: JSONObject.NULL)
            .put("quote_evidence", "STRUCTURAL_ONLY_NOT_FILL")
    }

    enum class RecordResult { RECORDED, IDEMPOTENT, CONFLICT, UNKNOWN_ATTEMPT }

    /**
     * Record the outcome of an attempt against the ORIGINAL slot named by the attempt id. Identical retries are
     * idempotent; a different payload for the same attempt is a CONFLICT (kept aside, original untouched).
     */
    fun recordCapture(dir: File, slotIdValue: String, attemptId: String, c: Capture, nowMs: Long): RecordResult = synchronized(lock) {
        val (index, session, ordS) = slotIdValue.split("|").let { if (it.size == 3) Triple(it[0], it[1], it[2]) else return RecordResult.UNKNOWN_ATTEMPT }
        val m = readManifest(dir, session) ?: return RecordResult.UNKNOWN_ATTEMPT
        val io = m.getJSONObject("indices").optJSONObject(index) ?: return RecordResult.UNKNOWN_ATTEMPT
        val slot = io.getJSONObject("slots").optJSONObject(ordS) ?: return RecordResult.UNKNOWN_ATTEMPT
        val ruleExpiry = io.optString("rule_expiry", "").takeIf { io.has("rule_expiry") && !io.isNull("rule_expiry") }
        val attempts = slot.getJSONArray("attempts")
        var att: JSONObject? = null
        for (i in 0 until attempts.length()) if (attempts.getJSONObject(i).optString("attempt_id") == attemptId) att = attempts.getJSONObject(i)
        if (att == null) return RecordResult.UNKNOWN_ATTEMPT
        val body = c.toJson()
        val digest = sha256(body.toString())
        if (att.has("capture_sha256")) {
            if (att.getString("capture_sha256") == digest) return RecordResult.IDEMPOTENT
            addConflict(dir, JSONObject().put("kind", "capture").put("slot_id", slotIdValue).put("attempt_id", attemptId)
                .put("kept_sha256", att.getString("capture_sha256")).put("rejected_sha256", digest).put("at_ms", nowMs))
            return RecordResult.CONFLICT
        }
        att.put("capture", body).put("capture_sha256", digest).put("recorded_ms", nowMs).put("status", "RECORDED")
        // Derived slot state from the BEST attempt; the state only ever improves, history stays in attempts.
        val nominal = slot.getLong("nominal_ms")
        val receipt = c.receiptMs
        val state = when {
            c.persistStatus == "NOT_FETCHED" || c.chainRows == 0 -> "PARTIAL" to "chain_not_fetched_or_poll_aborted"
            c.persistStatus != "DURABLE_ACKED" -> "PARTIAL" to "chain_not_durable:${c.persistStatus}"
            c.twoSidedRows == 0 -> "PARTIAL" to "no_two_sided_quotes"
            c.liveExpiry == null -> "PARTIAL" to "expiry_unknown"
            c.liveExpiry.startsWith("MULTIPLE:") -> "PARTIAL" to "multiple_expiries_in_chain"
            ruleExpiry != null && c.liveExpiry != ruleExpiry -> "PARTIAL" to "expiry_calendar_mismatch"
            c.imapVersion == null -> "PARTIAL" to "instrument_map_unavailable"
            receipt != null && receipt > nominal + ON_TIME_MS -> "LATE" to "receipt_after_on_time_deadline"
            receipt == null -> "PARTIAL" to "receipt_unknown"
            else -> "CAPTURED" to null
        }
        val rank = mapOf("PENDING" to 0, "MISSING" to 1, "PARTIAL" to 2, "LATE" to 3, "CAPTURED" to 4)
        val cur = slot.optString("state")
        if ((rank[state.first] ?: 0) > (rank[cur] ?: 0) || cur == "PENDING") {
            if (cur == "MISSING") slot.put("previous_state", "MISSING:" + slot.optString("reason"))
            slot.put("state", state.first).put("reason", state.second ?: JSONObject.NULL).put("state_attempt_id", attemptId)
        }
        saveManifest(dir, m)
        RecordResult.RECORDED
    }

    /**
     * Reconcile after process death, reboot, update or a later launch: enumerate every calendar session from the
     * collector start to [nowMs] (wholly missed sessions are created from the calendar), mark matured PENDING
     * slots MISSING with a reason, and report uncovered years as UNKNOWN coverage.
     */
    fun reconcile(dir: File, nowMs: Long): JSONObject = synchronized(lock) {
        val meta = meta(dir)
        val out = JSONObject().put("reconciled_ms", nowMs)
        val startSession = meta.optString("collector_start_session", "")
        if (startSession.isBlank()) return out.put("status", "NOT_STARTED")
        val start = LocalDate.parse(startSession)
        val today = LocalDate.parse(sessionOf(nowMs))
        val from = maxOf(start, today.minusDays(MAX_RECONCILE_SESSIONS.toLong() * 7 / 5))
        var created = 0; var missing = 0; val unknown = JSONArray()
        for ((d, s) in ResearchCalendar.sessionsBetween(from, today)) {
            if (s == null) { unknown.put(d.toString()); continue }
            val existed = readManifest(dir, d.toString()) != null
            val m = ensureManifest(dir, d.toString(), nowMs, observed = false)
            if (!existed) created++
            var changed = false
            val idx = m.getJSONObject("indices")
            for (index in ResearchCalendar.INDICES) {
                val slots = idx.getJSONObject(index).getJSONObject("slots")
                for (k in slots.keys()) {
                    val sl = slots.getJSONObject(k)
                    if (sl.optString("state") != "PENDING" || nowMs < sl.getLong("missing_after_ms")) continue
                    val atts = sl.getJSONArray("attempts")
                    val reason = when {
                        sl.getLong("nominal_ms") < meta.optLong("collector_start_ms", 0L) -> "before_collector_start"
                        !existed -> "session_not_observed_by_collector"
                        atts.length() == 0 -> "no_attempt"
                        else -> "attempt_abandoned_process_death_or_timeout"
                    }
                    sl.put("state", "MISSING").put("reason", reason).put("missing_marked_ms", nowMs)
                    missing++; changed = true
                }
            }
            if (changed) saveManifest(dir, m)
        }
        out.put("status", "OK").put("sessions_created", created).put("slots_marked_missing", missing)
            .put("unknown_coverage_dates", unknown)
    }

    /**
     * Derive the capture record for one index from the raw chain response the poll already fetched (no extra
     * request, no chain copy: only counts, the live expiry and the instrument-map reference are kept here; the
     * rows themselves stay single-sourced in ml_option_chain_snapshots). The Upstox v2 option-chain response
     * carries no per-option feed timestamp, so feed time is UNKNOWN - never invented.
     */
    fun captureFromChain(dir: File, index: String, chain: JSONObject?, fetchStartMs: Long?, fetchEndMs: Long?,
                         persistTable: String?, persistedMs: Long?, pollTs: String?, nowMs: Long): Capture {
        if (chain == null) return Capture(fetchStartMs, fetchEndMs, null, null, "NOT_FETCHED", 0, 0, 0, null, null, "UNKNOWN", pollTs)
        val data = chain.optJSONArray("data") ?: JSONArray()
        var rows = 0; var two = 0; var defects = 0
        val expiries = HashSet<String>()
        for (i in 0 until data.length()) {
            val item = data.optJSONObject(i) ?: continue
            if (!(item.optDouble("strike_price", 0.0) > 0)) continue
            item.optString("expiry", chain.optString("expiry", "")).takeIf { it.isNotBlank() }?.let { expiries.add(it) }
            for (k in listOf("call_options", "put_options")) {
                val md = item.optJSONObject(k)?.optJSONObject("market_data") ?: continue
                rows++
                val bid = md.optDouble("bid_price", Double.NaN); val ask = md.optDouble("ask_price", Double.NaN)
                if (bid > 0 && ask > 0 && ask >= bid) two++ else defects++
            }
        }
        val map = putInstrumentMap(dir, index, instrumentMapLines(index, chain), nowMs)
        val persist = when {
            persistTable == null -> "FAILED"
            persistTable.startsWith(ChainEvidenceRef.TABLE) -> "DURABLE_ACKED"
            else -> "LOCAL_ONLY"
        }
        return Capture(fetchStartMs, fetchEndMs, fetchEndMs, persistedMs, persist, rows, two, defects,
            if (expiries.size == 1) expiries.first() else if (expiries.isEmpty()) null else "MULTIPLE:" + expiries.sorted().joinToString(","),
            (map as? MapRef.Ok)?.version, "UNKNOWN", pollTs)
    }

    /** Close an attempt that never recorded a capture (fetch failure / early return), against its own slot. */
    fun closeIfOpen(dir: File, slotIdValue: String, attemptId: String, nowMs: Long): RecordResult {
        val session = slotIdValue.split("|").getOrNull(1) ?: return RecordResult.UNKNOWN_ATTEMPT
        val m = readManifest(dir, session) ?: return RecordResult.UNKNOWN_ATTEMPT
        val parts = slotIdValue.split("|")
        val a = m.getJSONObject("indices").optJSONObject(parts[0])?.getJSONObject("slots")?.optJSONObject(parts[2])
            ?.optJSONArray("attempts") ?: return RecordResult.UNKNOWN_ATTEMPT
        for (i in 0 until a.length()) {
            val x = a.getJSONObject(i)
            if (x.optString("attempt_id") == attemptId && !x.has("capture_sha256")) {
                return recordCapture(dir, slotIdValue, attemptId,
                    Capture(null, null, null, null, "NOT_FETCHED", 0, 0, 0, null, null, "UNKNOWN", null), nowMs)
            }
        }
        return RecordResult.IDEMPOTENT
    }

    // ------------------------------------------------------------------ instrument maps (B3)
    /** Canonical, sorted lines "index|expiry|strike|type|instrument_key" from a raw Upstox option-chain response. */
    fun instrumentMapLines(index: String, chain: JSONObject?): List<String> {
        val data = chain?.optJSONArray("data") ?: return emptyList()
        val out = ArrayList<String>()
        for (i in 0 until data.length()) {
            val item = data.optJSONObject(i) ?: continue
            val strike = item.optDouble("strike_price", 0.0)
            if (!(strike > 0)) continue
            val expiry = item.optString("expiry", chain.optString("expiry", ""))
            for ((t, k) in listOf("CE" to "call_options", "PE" to "put_options")) {
                val key = item.optJSONObject(k)?.optString("instrument_key", "")?.trim().orEmpty()
                if (key.isNotEmpty()) out.add("$index|$expiry|${strike.toLong()}|$t|$key")
            }
        }
        return out.sorted()
    }

    sealed class MapRef {
        data class Ok(val version: String, val created: Boolean) : MapRef()
        data class Unavailable(val reason: String) : MapRef()
    }

    /** Durably store (or verify) the immutable map BEFORE returning a reference to it. */
    fun putInstrumentMap(dir: File, index: String, lines: List<String>, nowMs: Long): MapRef = synchronized(lock) {
        if (index !in ResearchCalendar.INDICES) return MapRef.Unavailable("UNKNOWN_INDEX")
        if (lines.isEmpty()) return MapRef.Unavailable("NO_INSTRUMENT_KEYS")
        if (lines.any { !it.startsWith("$index|") }) return MapRef.Unavailable("CROSS_INDEX_ENTRY")
        val text = lines.joinToString("\n")
        val v = sha256(text)
        val f = File(dir, "imap_$v.json")
        val existing = readJson(f)
        if (existing != null) {
            return if (existing.optString("sha256") == v && sha256(existing.optString("lines")) == v) MapRef.Ok(v, false)
            else MapRef.Unavailable("IMAP_STORE_CORRUPT")
        }
        writeAtomic(f, JSONObject().put("schema", SCHEMA).put("index", index).put("sha256", v).put("lines", text)
            .put("entries", lines.size).put("created_ms", nowMs).toString())
        val back = readJson(f)
        if (back?.optString("sha256") != v) return MapRef.Unavailable("IMAP_WRITE_UNVERIFIED")
        pruneMaps(dir)
        MapRef.Ok(v, true)
    }

    fun readInstrumentMap(dir: File, version: String): List<String>? =
        readJson(File(dir, "imap_${safe(version)}.json"))?.optString("lines")?.split("\n")

    private fun referencedMaps(dir: File): Set<String> {
        val out = HashSet<String>()
        dir.listFiles { f -> f.name.startsWith("manifest_") && f.name.endsWith(".json") }?.forEach { mf ->
            val m = readJson(mf) ?: return@forEach
            val idx = m.optJSONObject("indices") ?: return@forEach
            for (i in idx.keys()) { val sl = idx.getJSONObject(i).optJSONObject("slots") ?: continue
                for (k in sl.keys()) { val a = sl.getJSONObject(k).optJSONArray("attempts") ?: continue
                    for (j in 0 until a.length()) a.getJSONObject(j).optJSONObject("capture")?.optString("imap_version")?.let { if (it.isNotBlank()) out.add(it) } } }
        }
        return out
    }

    private fun pruneMaps(dir: File) {
        val fs = dir.listFiles { f -> f.name.startsWith("imap_") && f.name.endsWith(".json") } ?: return
        if (fs.size <= MAX_IMAP_FILES) return
        val ref = referencedMaps(dir)
        fs.filter { it.name.removePrefix("imap_").removeSuffix(".json") !in ref }.sortedBy { it.lastModified() }
            .take(fs.size - MAX_IMAP_FILES).forEach { it.delete() }
    }

    // ------------------------------------------------------------------ forecast envelopes (B4)
    enum class EnvResult { STORED, IDEMPOTENT, CONFLICT, REJECTED }

    /**
     * Build the immutable envelope for the poll slot whose inputs produced [raw] (Python research_forecast_raw,
     * or null when the brain emitted none). The causal check refuses an envelope whose input watermark names a
     * poll other than [sourcePollSlotKey] (later-poll contamination): it is stored as FORECAST_UNAVAILABLE and can
     * never become that slot's forecast. Availability is the brain completion time; it is never back-dated.
     */
    fun buildEnvelope(sourcePollSlotKey: String, raw: JSONObject?, generatedAtMs: Long?, availableAtMs: Long?,
                      appVersion: String, versionCode: Int, policy: JSONObject, failure: String? = null): JSONObject {
        val e = JSONObject().put("schema", SCHEMA + ":envelope").put("source_poll_slot_key", sourcePollSlotKey)
            .put("generated_at_ms", generatedAtMs ?: JSONObject.NULL).put("available_at_ms", availableAtMs ?: JSONObject.NULL)
            .put("app_version", appVersion).put("version_code", versionCode).put("policy", policy)
            .put("calendar_version", ResearchCalendar.VERSION)
        if (raw == null) {
            return e.put("status", "MISSING").put("reason", failure ?: "brain_emitted_no_raw_forecast").put("raw", JSONObject.NULL)
        }
        e.put("raw", raw).put("brain_version", raw.optString("brain_version", "unknown"))
        // A2-15: availability after the next scheduled slot is recorded (the entry rule later uses the first poll
        // strictly after available_at); the envelope still belongs ONLY to its source slot.
        nextSlotNominalMs(sourcePollSlotKey)?.let { nx ->
            e.put("next_slot_nominal_ms", nx)
            if (availableAtMs != null) e.put("available_after_next_slot_nominal", availableAtMs >= nx)
        }
        val wm = raw.optString("input_last_poll_slot_key", "")
        val st = raw.optString("status", "")
        return when {
            wm.isBlank() -> e.put("status", "FORECAST_UNAVAILABLE").put("reason", "input_watermark_unknown")
            wm != sourcePollSlotKey -> e.put("status", "FORECAST_UNAVAILABLE").put("reason", "input_watermark_mismatch:$wm")
            availableAtMs == null -> e.put("status", "FORECAST_UNAVAILABLE").put("reason", "availability_unknown")
            st in setOf("VALID", "NEUTRAL", "MISSING", "MALFORMED") -> e.put("status", st).put("reason", raw.opt("reason") ?: JSONObject.NULL)
            else -> e.put("status", "MALFORMED").put("reason", "unknown_raw_status")
        }
    }

    fun nextSlotNominalMs(pollSlotKey: String): Long? = try {
        val d = LocalDate.parse(pollSlotKey.substringBefore("|"))
        val ord = pollSlotKey.substringAfterLast("|").toInt()
        ResearchCalendar.slotGrid(d).firstOrNull { it.ordinal == ord + 1 }?.nominalMs
    } catch (_: Exception) { null }

    private fun envFile(dir: File, slotKey: String) = File(dir, "env_${safe(slotKey)}.json")

    fun putEnvelope(dir: File, env: JSONObject, nowMs: Long): EnvResult = synchronized(lock) {
        val key = env.optString("source_poll_slot_key", "")
        if (key.isBlank() || !key.contains("|")) return EnvResult.REJECTED
        val body = env.toString()
        val digest = sha256(body)
        val f = envFile(dir, key)
        val cur = readJson(f)
        if (cur != null) {
            if (cur.optString("sha256") == digest) return EnvResult.IDEMPOTENT
            addConflict(dir, JSONObject().put("kind", "envelope").put("slot_key", key).put("kept_sha256", cur.optString("sha256"))
                .put("rejected_sha256", digest).put("rejected_status", env.optString("status")).put("at_ms", nowMs))
            return EnvResult.CONFLICT
        }
        writeAtomic(f, JSONObject().put("sha256", digest).put("stored_ms", nowMs).put("envelope", JSONObject(body)).toString())
        val fs = dir.listFiles { x -> x.name.startsWith("env_") && x.name.endsWith(".json") }
        if (fs != null && fs.size > MAX_ENVELOPE_FILES) fs.sortedBy { it.name }.take(fs.size - MAX_ENVELOPE_FILES).forEach { it.delete() }
        EnvResult.STORED
    }

    fun readEnvelope(dir: File, slotKey: String): JSONObject? = readJson(envFile(dir, slotKey))?.optJSONObject("envelope")

    // ------------------------------------------------------------------ conflicts
    private fun addConflict(dir: File, c: JSONObject) {
        val f = File(dir, "conflicts.json")
        val o = readJson(f) ?: JSONObject().put("total", 0).put("items", JSONArray())
        o.put("total", o.optInt("total") + 1)
        val items = o.getJSONArray("items"); items.put(c)
        while (items.length() > MAX_CONFLICTS) items.remove(0)
        writeAtomic(f, o.toString())
    }

    fun conflicts(dir: File): JSONObject = readJson(File(dir, "conflicts.json")) ?: JSONObject().put("total", 0).put("items", JSONArray())

    // ------------------------------------------------------------------ status (B6)
    fun status(dir: File, nowMs: Long, sessions: Int = 5): JSONObject = synchronized(lock) {
        val meta = meta(dir)
        val enabled = meta.optBoolean("enabled", false)
        val today = sessionOf(nowMs)
        val out = JSONObject().put("schema", SCHEMA + ":status").put("enabled", enabled)
            .put("collection_start", meta.opt("collector_start_session") ?: JSONObject.NULL)
            .put("calendar_version", ResearchCalendar.VERSION)
            .put("claims", "COLLECTION_ONLY: no winner, profitability, validated EV, multiday maturity or trained-model claim")
            .put("observe_note", "EV v1.2 Observe is a separate, narrow calibration label; it is not an execution mode")
            .put("failures", meta.optJSONObject("failures") ?: JSONObject())
        val files = dir.listFiles { f -> f.name.startsWith("manifest_") && f.name.endsWith(".json") }?.sortedByDescending { it.name } ?: emptyList()
        val arr = JSONArray()
        var latestDurable = false
        for (mf in files.take(sessions)) {
            val m = readJson(mf) ?: continue
            val s = JSONObject().put("session_date", m.optString("session_date")).put("created_by", m.optString("created_by"))
            val idx = m.getJSONObject("indices")
            for (index in ResearchCalendar.INDICES) {
                val io = idx.optJSONObject(index) ?: continue
                val c = JSONObject().put("available", io.optBoolean("available")).put("reason", io.opt("reason"))
                    .put("expected", io.optInt("expected"))
                STATES.forEach { c.put(it.lowercase(), 0) }
                var durable = 0; var localOnly = 0
                val slots = io.optJSONObject("slots") ?: JSONObject()
                for (k in slots.keys()) {
                    val sl = slots.getJSONObject(k)
                    val st = sl.optString("state").lowercase()
                    c.put(st, c.optInt(st) + 1)
                    val a = sl.optJSONArray("attempts") ?: JSONArray()
                    var best: String? = null
                    for (j in 0 until a.length()) a.getJSONObject(j).optJSONObject("capture")?.optString("persist_status")?.let {
                        if (it == "DURABLE_ACKED") best = it else if (best == null) best = it }
                    if (best == "DURABLE_ACKED") durable++ else if (best != null) localOnly++
                    if (m.optString("session_date") == today && best == "DURABLE_ACKED" &&
                        nowMs - sl.optLong("nominal_ms") in 0..(3 * ResearchCalendar.SLOT_MINUTES * 60_000L)) latestDurable = true
                }
                c.put("chain_durable_acked", durable).put("chain_local_or_failed", localOnly)
                s.put(index, c)
            }
            arr.put(s)
        }
        out.put("sessions", arr)
        var ev = JSONObject(); listOf("VALID", "NEUTRAL", "MISSING", "MALFORMED", "FORECAST_UNAVAILABLE").forEach { ev.put(it, 0) }
        dir.listFiles { f -> f.name.startsWith("env_${safe(today)}") }?.forEach { f ->
            val st = readJson(f)?.optJSONObject("envelope")?.optString("status") ?: return@forEach
            ev.put(st, ev.optInt(st) + 1)
        }
        out.put("envelopes_today", ev).put("envelopes_upload", "LOCAL_ONLY_UPLOAD_NOT_DEPLOYED")
        out.put("conflicts_total", conflicts(dir).optInt("total"))
        out.put("protection", ResearchProtection.summary(dir, today))
        val todayPlan = ResearchCalendar.plan(today, "NF")
        val label = when {
            !enabled -> "DISABLED"
            !todayPlan.available -> "CALENDAR_UNAVAILABLE"
            latestDurable -> "COLLECTING"
            else -> "ENABLED_NOT_CAPTURING"
        }
        out.put("label", label).put("label_text", when (label) {
            "COLLECTING" -> "Full-day research — collecting"
            "DISABLED" -> "Full-day research — collector off"
            "CALENDAR_UNAVAILABLE" -> "Full-day research — calendar unavailable (coverage unknown)"
            else -> "Full-day research — enabled, not currently capturing"
        })
    }
}

/**
 * B5 bounded staging/protection interface. M0 deletes NOTHING; this ledger records which (index, poll_ts) chain
 * observations a research owner needs, until an archive is acknowledged with a verified sha256 of the actual
 * bytes, or a final state is reached. Limits are explicit: [CAPACITY] entries and [LAG_LIMIT_SESSIONS] sessions.
 * Exceeding either produces a visible final state (CAPACITY_REJECTED / LAG_EXCEEDED), never an unbounded queue or
 * an indefinite pin. Any future pruner must call [isProtected] (hook only; no pruner exists today).
 */
object ResearchProtection {
    const val CAPACITY = 4000
    const val LAG_LIMIT_SESSIONS = 20
    val FINAL = setOf("ARCHIVE_ACKED", "LAG_EXCEEDED", "CAPACITY_REJECTED", "RELEASED")
    private val lock = Any()

    private fun file(dir: File) = File(dir, "protection.json")
    private fun load(dir: File): JSONObject = try { JSONObject(file(dir).readText()) } catch (_: Exception) {
        JSONObject().put("entries", JSONObject()).put("rejected_total", 0) }

    fun request(dir: File, id: String, owner: String, index: String, pollTs: String, session: String,
                roles: List<String>, nowMs: Long, capacity: Int = CAPACITY): String = synchronized(lock) {
        if (index !in ResearchCalendar.INDICES) return "UNKNOWN_INDEX"
        val o = load(dir); val e = o.getJSONObject("entries")
        e.optJSONObject(id)?.let { return it.optString("state") }
        val active = e.keys().asSequence().count { e.getJSONObject(it).optString("state") == "STAGED" }
        if (active >= capacity) {
            o.put("rejected_total", o.optInt("rejected_total") + 1)
            o.put("last_rejected", JSONObject().put("id", id).put("at_ms", nowMs).put("reason", "capacity"))
            ResearchCollector.writeAtomic(file(dir), o.toString())
            return "CAPACITY_REJECTED"
        }
        e.put(id, JSONObject().put("owner", owner).put("index", index).put("poll_ts", pollTs).put("session", session)
            .put("roles", JSONArray(roles)).put("state", "STAGED").put("created_ms", nowMs))
        ResearchCollector.writeAtomic(file(dir), o.toString())
        "STAGED"
    }

    fun acknowledgeArchive(dir: File, id: String, declaredSha: String, archivedBytes: ByteArray, nowMs: Long): String = synchronized(lock) {
        val o = load(dir); val x = o.getJSONObject("entries").optJSONObject(id) ?: return "UNKNOWN"
        if (x.optString("state") in FINAL) return x.optString("state")
        val actual = MessageDigest.getInstance("SHA-256").digest(archivedBytes).joinToString("") { "%02x".format(it) }
        if (actual != declaredSha || archivedBytes.isEmpty()) {
            x.put("last_ack_error", "sha_mismatch_or_empty").put("ack_failures", x.optInt("ack_failures") + 1)
            ResearchCollector.writeAtomic(file(dir), o.toString()); return "STAGED"
        }
        x.put("state", "ARCHIVE_ACKED").put("archive_sha256", actual).put("archive_bytes", archivedBytes.size).put("acked_ms", nowMs)
        ResearchCollector.writeAtomic(file(dir), o.toString()); "ARCHIVE_ACKED"
    }

    /** Sessions-lag check against the dated calendar; overdue STAGED entries become LAG_EXCEEDED (visible, final). */
    fun enforceLag(dir: File, today: String, lagLimit: Int = LAG_LIMIT_SESSIONS): Int = synchronized(lock) {
        val o = load(dir); val e = o.getJSONObject("entries"); var n = 0
        for (k in e.keys()) {
            val x = e.getJSONObject(k)
            if (x.optString("state") != "STAGED") continue
            val lag = ResearchCalendar.sessionsBetween(LocalDate.parse(x.getString("session")).plusDays(1), LocalDate.parse(today)).size
            if (lag > lagLimit) { x.put("state", "LAG_EXCEEDED").put("lag_sessions", lag); n++ }
        }
        if (n > 0) ResearchCollector.writeAtomic(file(dir), o.toString())
        n
    }

    fun isProtected(dir: File, index: String, pollTs: String): Boolean = synchronized(lock) {
        val e = load(dir).getJSONObject("entries")
        e.keys().asSequence().any { val x = e.getJSONObject(it); x.optString("state") == "STAGED" && x.optString("index") == index && x.optString("poll_ts") == pollTs }
    }

    fun entry(dir: File, id: String): JSONObject? = load(dir).getJSONObject("entries").optJSONObject(id)

    fun summary(dir: File, today: String): JSONObject {
        val o = load(dir); val e = o.getJSONObject("entries")
        val c = JSONObject().put("STAGED", 0).put("ARCHIVE_ACKED", 0).put("LAG_EXCEEDED", 0).put("RELEASED", 0)
        for (k in e.keys()) { val s = e.getJSONObject(k).optString("state"); c.put(s, c.optInt(s) + 1) }
        return c.put("CAPACITY_REJECTED", o.optInt("rejected_total")).put("capacity", CAPACITY).put("lag_limit_sessions", LAG_LIMIT_SESSIONS)
            .put("integrity", if (c.optInt("LAG_EXCEEDED") + o.optInt("rejected_total") > 0) "INCOMPLETE_REPLAY" else "OK")
    }
}
