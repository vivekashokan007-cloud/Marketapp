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
 * Batch 1: every file is a SEALED record written crash-safely by [M0Durable] (W5); corrupt files are reported,
 * never overwritten or read as empty. Retention is guarded by archive acknowledgement (W4).
 *
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
    const val SCHEMA = "research_m0_collector_v2"
    const val ON_TIME_MS = 5L * 60_000
    const val MISSING_AFTER_MS = 15L * 60_000
    @Volatile var MAX_MANIFEST_SESSIONS = 120
    @Volatile var MAX_IMAP_FILES = 400
    @Volatile var MAX_IMAP_BYTES = 40L * 1024 * 1024
    @Volatile var MAX_ENVELOPE_FILES = 120 * 80
    const val MAX_CONFLICTS = 200
    const val MAX_ATTEMPTS_PER_SLOT = 6
    const val MAX_RECONCILE_SESSIONS = 90
    const val RECONCILE_MIN_INTERVAL_MS = 30L * 60_000
    /** A map lease older than this belongs to an attempt that can no longer be live (poll timeout << 1 h). */
    const val LEASE_TTL_MS = 60L * 60_000
    /** Must equal research_forecast_capture.RESULT_KEY (Python); pinned by a test. */
    const val RAW_FORECAST_KEY = "research_forecast_raw"
    const val PROTECTION_OWNER = "m0_collector"
    val INDICES_FOR_POLL = listOf("NF", "BNF")
    val STATES = listOf("PENDING", "CAPTURED", "LATE", "PARTIAL", "MISSING", "UNAVAILABLE")

    internal val lock = Any()

    fun sha256(text: String): String = M0Durable.sha256(text)

    fun sessionOf(ms: Long): String = Instant.ofEpochMilli(ms).atZone(ResearchCalendar.IST).toLocalDate().toString()

    // ------------------------------------------------------------------ file helpers (W5: sealed + crash-safe)
    internal fun writeAtomic(f: File, text: String) = M0Durable.write(f, text)

    /** OK json, null when ABSENT; throws [CorruptEvidence] when CORRUPT (never read as empty). */
    class CorruptEvidence(val file: String, val reason: String) : IllegalStateException("CORRUPT:$file:$reason")

    private fun readJson(f: File): JSONObject? = when (val r = M0Durable.read(f)) {
        is M0Durable.Read.Absent -> null
        is M0Durable.Read.Ok -> r.json
        is M0Durable.Read.Corrupt -> { noteCorrupt(f.parentFile, f.name, r.reason); throw CorruptEvidence(f.name, r.reason) }
    }

    private fun readOrNull(f: File): JSONObject? = try { readJson(f) } catch (_: CorruptEvidence) { null }

    /** Persist (bounded) which files were found corrupt; uses its own file so it never depends on a corrupt one. */
    private fun noteCorrupt(dir: File?, name: String, reason: String) {
        if (dir == null || name == "integrity.json") return
        try {
            val f = File(dir, "integrity.json")
            val o = (M0Durable.read(f) as? M0Durable.Read.Ok)?.json ?: JSONObject().put("corrupt", JSONObject())
            val c = o.getJSONObject("corrupt")
            if (c.optString(name) == reason) return
            c.put(name, reason)
            if (c.length() > 200) return
            M0Durable.write(f, o.toString())
        } catch (_: Throwable) {}
    }

    fun integrity(dir: File): JSONObject = (M0Durable.read(File(dir, "integrity.json")) as? M0Durable.Read.Ok)?.json?.optJSONObject("corrupt") ?: JSONObject()

    private fun safe(s: String) = s.replace(Regex("[^A-Za-z0-9_.-]"), "_").take(80)

    // ------------------------------------------------------------------ meta / sequence
    fun meta(dir: File): JSONObject = readOrNull(File(dir, "meta.json")) ?: JSONObject().put("schema", SCHEMA).put("enabled", false)

    fun setEnabled(dir: File, enabled: Boolean, nowMs: Long) = synchronized(lock) {
        val mf = File(dir, "meta.json")
        val r = M0Durable.read(mf)
        if (r is M0Durable.Read.Corrupt) { noteCorrupt(dir, mf.name, r.reason); throw CorruptEvidence(mf.name, r.reason) }
        val m = meta(dir)
        m.put("enabled", enabled)
        if (enabled && !m.has("collector_start_ms")) {
            m.put("collector_start_ms", nowMs).put("collector_start_session", sessionOf(nowMs))
        }
        m.put("enabled_changed_ms", nowMs)
        writeAtomic(mf, m.toString())
    }

    fun isEnabled(dir: File) = meta(dir).optBoolean("enabled", false)

    fun recordFailure(dir: File, where: String, err: Throwable?) {
        try { synchronized(lock) {
            val mf = File(dir, "meta.json")
            if (M0Durable.read(mf) is M0Durable.Read.Corrupt) return
            val m = meta(dir)
            val f = m.optJSONObject("failures") ?: JSONObject()
            f.put(where, f.optInt(where, 0) + 1)
            m.put("failures", f).put("last_failure", JSONObject().put("where", where)
                .put("error", (err?.javaClass?.simpleName ?: "unknown") + ":" + (err?.message ?: "").take(120)))
            writeAtomic(mf, m.toString())
        } } catch (_: Throwable) { /* visible counter is best effort; never propagates */ }
    }

    data class Seq(val epoch: String, val seq: Long) { val id get() = "$epoch:$seq" }

    /** Persist the incremented counter BEFORE handing the number out. A corrupt/lost seq mints a NEW epoch (no
     *  collision with older ids); the corrupt file is kept aside, not silently reused. */
    fun nextSeq(dir: File): Seq = synchronized(lock) {
        val f = File(dir, "seq.json")
        val r = M0Durable.read(f)
        if (r is M0Durable.Read.Corrupt) { noteCorrupt(dir, f.name, r.reason); f.renameTo(File(dir, "seq.json.corrupt")) }
        val cur = (r as? M0Durable.Read.Ok)?.json
        val epoch = cur?.optString("epoch", "")?.takeIf { it.isNotBlank() } ?: UUID.randomUUID().toString()
        val next = if (cur != null && cur.optString("epoch") == epoch) cur.optLong("next", 1L) else 1L
        writeAtomic(f, JSONObject().put("epoch", epoch).put("next", next + 1).toString())
        Seq(epoch, next)
    }

    // ------------------------------------------------------------------ manifest
    fun slotId(index: String, session: String, ordinal: Int) = "$index|$session|$ordinal"
    private fun manifestFile(dir: File, session: String) = File(dir, "manifest_${safe(session)}.json")
    private fun manifestFiles(dir: File) = dir.listFiles { f -> f.name.startsWith("manifest_") && f.name.endsWith(".json") }?.sortedBy { it.name } ?: emptyList()

    /** Create (idempotently) the scheduled manifest for one session from the calendar; never from returned data.
     *  Throws [CorruptEvidence] for a corrupt manifest (never recreated over it) and [CapacityBacklog] when the
     *  retention cap is reached and no older session is archive-acknowledged (nothing unacknowledged is deleted). */
    class CapacityBacklog(val what: String) : IllegalStateException("CAPACITY_BACKLOG:$what")

    fun ensureManifest(dir: File, session: String, nowMs: Long, observed: Boolean = true): JSONObject = synchronized(lock) {
        val f = manifestFile(dir, session)
        readJson(f)?.let { return it }
        if (!reclaimManifests(dir, MAX_MANIFEST_SESSIONS - 1)) throw CapacityBacklog("manifest")
        val m = JSONObject().put("schema", SCHEMA).put("session_date", session).put("created_ms", nowMs)
            .put("calendar_version", ResearchCalendar.VERSION).put("created_by", if (observed) "collector" else "reconcile_missed_session")
        val idx = JSONObject()
        for (index in ResearchCalendar.INDICES) {
            val p = ResearchCalendar.plan(session, index)
            val o = JSONObject().put("available", p.available).put("reason", p.reason ?: JSONObject.NULL)
                .put("is_session", p.isSession).put("expiry_authority", p.expiryAuthority)
                .put("rule_expiry", p.ruleExpiry ?: JSONObject.NULL).put("rule_expiry_day", p.ruleExpiryDay ?: JSONObject.NULL)
                .put("regime", p.regime).put("session_kind", p.sessionKind).put("special_type", p.specialType ?: JSONObject.NULL)
                .put("expected", p.slots.size)
            val slots = JSONObject()
            val d = LocalDate.parse(session)
            for (s in p.slots) {
                slots.put(s.ordinal.toString(), JSONObject().put("slot_id", slotId(index, session, s.ordinal))
                    .put("nominal", s.hhmm).put("nominal_ms", s.nominalMs).put("kind", s.kind)
                    .put("phase_non_expiring", s.phaseNonExpiring).put("phase_expiring", s.phaseExpiring)
                    .put("live_driver_gap", ResearchCalendar.liveDriverGap(d, s) ?: JSONObject.NULL)
                    .put("on_time_deadline_ms", s.nominalMs + ON_TIME_MS).put("missing_after_ms", s.nominalMs + MISSING_AFTER_MS)
                    .put("state", "PENDING").put("reason", JSONObject.NULL).put("attempts", JSONArray()))
            }
            o.put("slots", slots)
            idx.put(index, o)
        }
        m.put("indices", idx)
        writeAtomic(f, m.toString())
        m
    }

    fun readManifest(dir: File, session: String): JSONObject? = readJson(manifestFile(dir, session))

    private fun saveManifest(dir: File, m: JSONObject) = writeAtomic(manifestFile(dir, m.getString("session_date")), m.toString())

    /**
     * W4: count-pruning is GUARDED. Reduce manifests to <= [keep] by deleting the oldest sessions whose every
     * protection entry is ARCHIVE_ACKED (verified manifest) - their entries are then RELEASED. Returns false when
     * the cap cannot be met without deleting unacknowledged evidence (caller refuses new admission; nothing deleted).
     */
    internal fun reclaimManifests(dir: File, keep: Int): Boolean {
        val fs = manifestFiles(dir)
        if (fs.size <= keep) return true
        var excess = fs.size - keep
        for (mf in fs) {
            if (excess <= 0) break
            val session = mf.name.removePrefix("manifest_").removeSuffix(".json")
            if (!ResearchProtection.sessionReleasable(dir, session)) continue
            mf.delete(); M0Durable.fsyncDir(dir)
            ResearchProtection.releaseSession(dir, session, System.currentTimeMillis())
            excess--
        }
        return excess <= 0
    }

    sealed class Begin {
        data class Started(val slotId: String, val attemptId: String, val attemptNo: Int) : Begin()
        data class Unavailable(val reason: String) : Begin()
    }

    /**
     * Persist the scheduled identity and the attempt (with its epoch+sequence) BEFORE the request is sent.
     * [pollSlotKey] is the poll's frozen slot key "yyyy-MM-dd|ordinal"; an unknown index is UNAVAILABLE.
     * W4: at the protection cap / with a corrupt ledger, further capture is REJECTED visibly.
     */
    fun beginAttempt(dir: File, index: String, pollSlotKey: String, nowMs: Long): Begin = synchronized(lock) {
        if (index !in ResearchCalendar.INDICES) return Begin.Unavailable("UNKNOWN_INDEX")
        val session = pollSlotKey.substringBefore("|")
        val ord = pollSlotKey.substringAfterLast("|", "").toIntOrNull() ?: return Begin.Unavailable("BAD_SLOT_KEY")
        when (val p = ResearchProtection.admission(dir)) { null -> {}; else -> return Begin.Unavailable(p) }
        val m = try { ensureManifest(dir, session, nowMs) } catch (e: CorruptEvidence) { return Begin.Unavailable("MANIFEST_CORRUPT") }
            catch (e: CapacityBacklog) { return Begin.Unavailable("MANIFEST_CAPACITY_BACKLOG") }
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
        val pollTs: String?, val imapUnavailable: String? = null,
        /** B1-R1: canonical digest of the captured chain rows, computed from the fetched response at capture time. */
        val sourceRowsSha256: String? = null
    ) {
        fun toJson(): JSONObject = JSONObject().put("fetch_start_ms", fetchStartMs ?: JSONObject.NULL)
            .put("fetch_end_ms", fetchEndMs ?: JSONObject.NULL).put("receipt_ms", receiptMs ?: JSONObject.NULL)
            .put("persisted_ms", persistedMs ?: JSONObject.NULL).put("persist_status", persistStatus)
            .put("chain_rows", chainRows).put("two_sided_rows", twoSidedRows).put("defect_rows", defectRows)
            .put("live_expiry", liveExpiry ?: JSONObject.NULL).put("imap_version", imapVersion ?: JSONObject.NULL)
            .put("imap_unavailable", imapUnavailable ?: JSONObject.NULL)
            .put("feed_time", feedTimeStatus).put("poll_ts", pollTs ?: JSONObject.NULL)
            .put("quote_evidence", "STRUCTURAL_ONLY_NOT_FILL")
            .put("source_rows_sha256", sourceRowsSha256 ?: JSONObject.NULL)
    }

    enum class RecordResult { RECORDED, IDEMPOTENT, CONFLICT, UNKNOWN_ATTEMPT, CORRUPT }

    /**
     * Record the outcome of an attempt against the ORIGINAL slot named by the attempt id. Identical retries are
     * idempotent; a different payload for the same attempt is a CONFLICT (kept aside, original untouched).
     * W2: the contract phase (expiring vs non-expiring) is recorded; post-stop observations are never tradable evidence.
     * W4: a durable capture requests protection for its (index, poll_ts) chain observation.
     */
    fun recordCapture(dir: File, slotIdValue: String, attemptId: String, c: Capture, nowMs: Long): RecordResult = synchronized(lock) {
        val (index, session, ordS) = slotIdValue.split("|").let { if (it.size == 3) Triple(it[0], it[1], it[2]) else return RecordResult.UNKNOWN_ATTEMPT }
        val m = try { readManifest(dir, session) } catch (_: CorruptEvidence) { return RecordResult.CORRUPT } ?: return RecordResult.UNKNOWN_ATTEMPT
        val io = m.getJSONObject("indices").optJSONObject(index) ?: return RecordResult.UNKNOWN_ATTEMPT
        val slot = io.getJSONObject("slots").optJSONObject(ordS) ?: return RecordResult.UNKNOWN_ATTEMPT
        val ruleExpiry = io.optString("rule_expiry", "").takeIf { io.has("rule_expiry") && !io.isNull("rule_expiry") }
        val attempts = slot.getJSONArray("attempts")
        var att: JSONObject? = null
        for (i in 0 until attempts.length()) if (attempts.getJSONObject(i).optString("attempt_id") == attemptId) att = attempts.getJSONObject(i)
        if (att == null) return RecordResult.UNKNOWN_ATTEMPT
        val body = c.toJson()
        val expiring: Boolean? = c.liveExpiry?.takeIf { !it.startsWith("MULTIPLE:") }?.let { it == session }
        val contractPhase = when (expiring) {
            true -> slot.optString("phase_expiring"); false -> slot.optString("phase_non_expiring"); null -> "UNKNOWN_CONTRACT_CLASS"
        }
        body.put("contract_class", when (expiring) { true -> "EXPIRING"; false -> "NON_EXPIRING"; null -> "UNKNOWN" })
            .put("contract_phase", contractPhase)
            .put("tradable_contract_evidence", contractPhase == "CONTINUOUS_TRADING")
        val digest = sha256(body.toString())
        if (att.has("capture_sha256")) {
            if (att.getString("capture_sha256") == digest) return RecordResult.IDEMPOTENT
            addConflict(dir, JSONObject().put("kind", "capture").put("slot_id", slotIdValue).put("attempt_id", attemptId)
                .put("kept_sha256", att.getString("capture_sha256")).put("rejected_sha256", digest).put("at_ms", nowMs))
            return RecordResult.CONFLICT
        }
        att.put("capture", body).put("capture_sha256", digest).put("recorded_ms", nowMs).put("status", "RECORDED")
        val nominal = slot.getLong("nominal_ms")
        val receipt = c.receiptMs
        val state = when {
            c.persistStatus == "NOT_FETCHED" || c.chainRows == 0 -> "PARTIAL" to "chain_not_fetched_or_poll_aborted"
            c.persistStatus != "DURABLE_ACKED" -> "PARTIAL" to "chain_not_durable:${c.persistStatus}"
            c.twoSidedRows == 0 -> "PARTIAL" to "no_two_sided_quotes"
            c.liveExpiry == null -> "PARTIAL" to "expiry_unknown"
            c.liveExpiry.startsWith("MULTIPLE:") -> "PARTIAL" to "multiple_expiries_in_chain"
            ruleExpiry != null && c.liveExpiry != ruleExpiry -> "PARTIAL" to "expiry_calendar_mismatch"
            c.imapVersion == null -> "PARTIAL" to ("instrument_map_unavailable" + (c.imapUnavailable?.let { ":$it" } ?: ""))
            receipt != null && receipt > nominal + ON_TIME_MS -> "LATE" to "receipt_after_on_time_deadline"
            receipt == null -> "PARTIAL" to "receipt_unknown"
            else -> "CAPTURED" to null
        }
        val rank = mapOf("PENDING" to 0, "MISSING" to 1, "PARTIAL" to 2, "LATE" to 3, "CAPTURED" to 4)
        val cur = slot.optString("state")
        if ((rank[state.first] ?: 0) > (rank[cur] ?: 0) || cur == "PENDING") {
            if (cur == "MISSING") slot.put("previous_state", "MISSING:" + slot.optString("reason"))
            slot.put("state", state.first).put("reason", state.second ?: JSONObject.NULL).put("state_attempt_id", attemptId)
                .put("contract_phase", contractPhase)
        }
        // W4: protection request BEFORE the manifest commit is visible as protected-or-flagged.
        // B1-R2: EVERY distinct durable attempt is protected under its own durable id (no first-attempt gate).
        if (c.persistStatus == "DURABLE_ACKED" && !c.pollTs.isNullOrBlank()) {
            val pid = chainProtectionId(slotIdValue, attemptId)
            val pr = ResearchProtection.request(dir, pid, PROTECTION_OWNER, index, c.pollTs, session,
                listOf("CHAIN_POLL"), nowMs, expectedRows = c.chainRows, sourceSha = c.sourceRowsSha256)
            att.put("protection_id", pid).put("protection", pr)
            slot.put("protection", pr)   // latest attempt's protection state (summary only)
            if (pr != "STAGED") slot.put("protection_flag", "attempt:$attemptId:$pr")
        }
        saveManifest(dir, m)
        c.imapVersion?.let { InstrumentMaps.releaseLease(dir, it, attemptId) }
        RecordResult.RECORDED
    }

    /**
     * Reconcile after process death, reboot, update or a later launch: recover leftover temp files, enumerate every
     * collection date (regular + special) from the collector start to [nowMs], mark matured PENDING slots MISSING
     * with a reason (live-driver gaps such as special_session_not_polled / poll_driver_unavailable_after_1540 are
     * named), enforce protection lag, and report uncovered years as UNKNOWN coverage. Denominators never shrink.
     */
    fun reconcile(dir: File, nowMs: Long): JSONObject = synchronized(lock) {
        val out = JSONObject().put("reconciled_ms", nowMs)
        if (dir.exists()) out.put("recovery", M0Durable.recover(dir))
        if (dir.exists()) out.put("envelopes_protection_repaired", reconcileEnvelopes(dir, nowMs))
        val meta = meta(dir)
        val startSession = meta.optString("collector_start_session", "")
        if (startSession.isBlank()) return out.put("status", "NOT_STARTED")
        val start = LocalDate.parse(startSession)
        val today = LocalDate.parse(sessionOf(nowMs))
        val from = maxOf(start, today.minusDays(MAX_RECONCILE_SESSIONS.toLong() * 7 / 5))
        var created = 0; var missing = 0; val unknown = JSONArray(); val corrupt = JSONArray(); val backlog = JSONArray()
        for ((d, s) in ResearchCalendar.sessionsBetween(from, today)) {
            if (s == null) { unknown.put(d.toString()); continue }
            val existed = try { readManifest(dir, d.toString()) != null } catch (_: CorruptEvidence) { corrupt.put(d.toString()); continue }
            val m = try { ensureManifest(dir, d.toString(), nowMs, observed = false) } catch (_: CapacityBacklog) { backlog.put(d.toString()); continue }
            if (!existed) created++
            var changed = false
            val idx = m.getJSONObject("indices")
            for (index in ResearchCalendar.INDICES) {
                val slots = idx.getJSONObject(index).getJSONObject("slots")
                for (k in slots.keys()) {
                    val sl = slots.getJSONObject(k)
                    if (sl.optString("state") != "PENDING" || nowMs < sl.getLong("missing_after_ms")) continue
                    val atts = sl.getJSONArray("attempts")
                    val gap = sl.optString("live_driver_gap", "").takeIf { !sl.isNull("live_driver_gap") && it.isNotBlank() }
                    val reason = when {
                        sl.getLong("nominal_ms") < meta.optLong("collector_start_ms", 0L) -> "before_collector_start"
                        gap != null && atts.length() == 0 -> gap
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
        // W4: past session manifests are protected until a verified archive of the CURRENT manifest is acknowledged.
        for (mf in manifestFiles(dir)) {
            val sd = mf.name.removePrefix("manifest_").removeSuffix(".json")
            if (sd < today.toString() && M0Durable.read(mf) is M0Durable.Read.Ok)
                ResearchProtection.request(dir, ResearchProtection.sessionManifestId(sd), PROTECTION_OWNER, "SESSION", sd, sd,
                    listOf("SESSION_MANIFEST"), nowMs)
        }
        InstrumentMaps.expireLeases(dir, nowMs - LEASE_TTL_MS)
        val lag = ResearchProtection.enforceLag(dir, today.toString())
        out.put("status", "OK").put("sessions_created", created).put("slots_marked_missing", missing)
            .put("unknown_coverage_dates", unknown).put("corrupt_manifests", corrupt).put("capacity_backlog_dates", backlog)
            .put("protection_lag_marked", lag)
    }

    /** Bounded runtime point for reconcile/enforceLag inside the poll loop: at most once per interval. */
    fun maybeReconcile(dir: File, nowMs: Long): JSONObject? = synchronized(lock) {
        val m = meta(dir)
        if (nowMs - m.optLong("last_reconcile_ms", 0L) < RECONCILE_MIN_INTERVAL_MS) return null
        val r = reconcile(dir, nowMs)
        if (M0Durable.read(File(dir, "meta.json")) !is M0Durable.Read.Corrupt) {
            val mm = meta(dir); mm.put("last_reconcile_ms", nowMs); writeAtomic(File(dir, "meta.json"), mm.toString())
        }
        r
    }

    /**
     * Derive the capture record for one index from the raw chain response the poll already fetched (no extra
     * request, no chain copy). [leaseId] (the attempt id) leases a newly admitted instrument map until the capture
     * that references it is committed, so the map cannot be pruned in between (W5a).
     */
    fun captureFromChain(dir: File, index: String, chain: JSONObject?, fetchStartMs: Long?, fetchEndMs: Long?,
                         persistTable: String?, persistedMs: Long?, pollTs: String?, nowMs: Long, leaseId: String? = null): Capture {
        if (chain == null) return Capture(fetchStartMs, fetchEndMs, null, null, "NOT_FETCHED", 0, 0, 0, null, null, "UNKNOWN", pollTs)
        val data = chain.optJSONArray("data") ?: JSONArray()
        var rows = 0; var two = 0; var defects = 0
        val expiries = HashSet<String>()
        val canon = ArrayList<String>()
        for (i in 0 until data.length()) {
            val item = data.optJSONObject(i) ?: continue
            if (!(item.optDouble("strike_price", 0.0) > 0)) continue
            val itemExpiry = item.optString("expiry", chain.optString("expiry", ""))
            itemExpiry.takeIf { it.isNotBlank() }?.let { expiries.add(it) }
            for ((t, k) in listOf("CE" to "call_options", "PE" to "put_options")) {
                val leg = item.optJSONObject(k) ?: continue
                val md = leg.optJSONObject("market_data") ?: continue
                rows++
                canon.add(canonicalRow(itemExpiry, item.opt("strike_price"), t, leg.opt("instrument_key"),
                    md.opt("bid_price"), md.opt("ask_price"), md.opt("ltp")))
                val bid = md.optDouble("bid_price", Double.NaN); val ask = md.optDouble("ask_price", Double.NaN)
                if (bid > 0 && ask > 0 && ask >= bid) two++ else defects++
            }
        }
        val map = putInstrumentMap(dir, index, instrumentMapLines(index, chain), nowMs, leaseId)
        val persist = when {
            persistTable == null -> "FAILED"
            persistTable.startsWith(ChainEvidenceRef.TABLE) -> "DURABLE_ACKED"
            else -> "LOCAL_ONLY"
        }
        return Capture(fetchStartMs, fetchEndMs, fetchEndMs, persistedMs, persist, rows, two, defects,
            if (expiries.size == 1) expiries.first() else if (expiries.isEmpty()) null else "MULTIPLE:" + expiries.sorted().joinToString(","),
            (map as? MapRef.Ok)?.version, "UNKNOWN", pollTs, (map as? MapRef.Unavailable)?.reason,
            if (rows > 0) canonicalRowsDigest(index, canon) else null)
    }

    // ------------------------------------------------------------------ B1-R1 canonical chain-row source proof
    /** Canonical number: plain decimal without trailing zeros; absent/null -> "null"; non-numeric -> "invalid:<text>". */
    internal fun canonNum(v: Any?): String = when {
        v == null || v == JSONObject.NULL -> "null"
        else -> try { java.math.BigDecimal(v.toString().trim()).stripTrailingZeros().toPlainString() } catch (_: Exception) { "invalid:" + v.toString() }
    }
    internal fun canonText(v: Any?): String = if (v == null || v == JSONObject.NULL) "null" else v.toString().trim()
    internal fun canonType(v: Any?): String = when (canonText(v).uppercase()) { "CE", "CALL", "C" -> "CE"; "PE", "PUT", "P" -> "PE"; else -> "invalid:" + canonText(v) }
    /** One captured leg: expiry|strike|type|instrument_key|bid|ask|ltp (every field is bound). */
    internal fun canonicalRow(expiry: Any?, strike: Any?, type: Any?, instrumentKey: Any?, bid: Any?, ask: Any?, ltp: Any?): String =
        listOf(canonText(expiry), canonNum(strike), canonType(type), canonText(instrumentKey), canonNum(bid), canonNum(ask), canonNum(ltp)).joinToString("|")
    /** Digest over the index, row count and the sorted canonical rows (order-independent, field-exact). */
    fun canonicalRowsDigest(index: String, rows: List<String>): String =
        sha256("m0-chain-rows-v1\n$index\n${rows.size}\n" + rows.sorted().joinToString("\n"))
    /** Archive rows schema: [{expiry, strike, option_type, instrument_key, bid, ask, ltp}]; null if any row is not an object. */
    fun canonicalArchiveRows(rows: JSONArray): List<String>? = (0 until rows.length()).map { i ->
        val r = rows.optJSONObject(i) ?: return null
        canonicalRow(r.opt("expiry"), r.opt("strike"), r.opt("option_type"), r.opt("instrument_key"), r.opt("bid"), r.opt("ask"), r.opt("ltp"))
    }
    /** B1-R2: one protection entry per durable attempt; stable across retries and restarts (attempt ids are durable). */
    fun chainProtectionId(slotIdValue: String, attemptId: String) = "$slotIdValue@$attemptId"

    /** Close an attempt that never recorded a capture (fetch failure / early return), against its own slot. */
    fun closeIfOpen(dir: File, slotIdValue: String, attemptId: String, nowMs: Long): RecordResult {
        val session = slotIdValue.split("|").getOrNull(1) ?: return RecordResult.UNKNOWN_ATTEMPT
        val m = try { readManifest(dir, session) } catch (_: CorruptEvidence) { return RecordResult.CORRUPT } ?: return RecordResult.UNKNOWN_ATTEMPT
        val parts = slotIdValue.split("|")
        val a = m.getJSONObject("indices").optJSONObject(parts[0])?.getJSONObject("slots")?.optJSONObject(parts[2])
            ?.optJSONArray("attempts") ?: return RecordResult.UNKNOWN_ATTEMPT
        InstrumentMaps.releaseAllLeases(dir, attemptId)
        for (i in 0 until a.length()) {
            val x = a.getJSONObject(i)
            if (x.optString("attempt_id") == attemptId && !x.has("capture_sha256")) {
                return recordCapture(dir, slotIdValue, attemptId,
                    Capture(null, null, null, null, "NOT_FETCHED", 0, 0, 0, null, null, "UNKNOWN", null), nowMs)
            }
        }
        return RecordResult.IDEMPOTENT
    }

    // ------------------------------------------------------------------ instrument maps (B3 + W5a)
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

    fun putInstrumentMap(dir: File, index: String, lines: List<String>, nowMs: Long, leaseId: String? = null): MapRef =
        synchronized(lock) { InstrumentMaps.put(dir, index, lines, nowMs, leaseId) }

    fun readInstrumentMap(dir: File, version: String): List<String>? =
        readOrNull(File(dir, "imap_${safe(version)}.json"))?.optString("lines")?.split("\n")

    /** B1-R4: null when ANY manifest is unreadable - the reference set is then unknown and nothing may be pruned. */
    internal fun referencedMaps(dir: File): Set<String>? {
        val out = HashSet<String>()
        for (mf in manifestFiles(dir)) {
            val m = try { readJson(mf) } catch (_: CorruptEvidence) { return null } ?: continue
            val idx = m.optJSONObject("indices") ?: continue
            for (i in idx.keys()) { val sl = idx.getJSONObject(i).optJSONObject("slots") ?: continue
                for (k in sl.keys()) { val a = sl.getJSONObject(k).optJSONArray("attempts") ?: continue
                    for (j in 0 until a.length()) a.getJSONObject(j).optJSONObject("capture")?.optString("imap_version")?.let { if (it.isNotBlank() && it != "null") out.add(it) } } }
        }
        return out
    }

    // ------------------------------------------------------------------ forecast envelopes (B4)
    /** STORED_UNPROTECTED: bytes committed but protection was refused/unavailable - flagged, never reported as STORED. */
    enum class EnvResult { STORED, STORED_UNPROTECTED, IDEMPOTENT, CONFLICT, REJECTED, CAPACITY_BACKLOG, CORRUPT }

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
    fun envelopeEntryId(slotKey: String) = "env|$slotKey"

    /** W4: envelopes are protected (role FORECAST_ENVELOPE) and only removed after a verified acknowledgement. */
    fun putEnvelope(dir: File, env: JSONObject, nowMs: Long): EnvResult = synchronized(lock) {
        val key = env.optString("source_poll_slot_key", "")
        if (key.isBlank() || !key.contains("|")) return EnvResult.REJECTED
        val body = env.toString()
        val digest = sha256(body)
        val f = envFile(dir, key)
        val cur = try { readJson(f) } catch (_: CorruptEvidence) { return EnvResult.CORRUPT }
        if (cur != null) {
            if (cur.optString("sha256") == digest) {
                // B1-R3: an idempotent retry repairs (or visibly flags) missing protection for the committed bytes.
                return when (ensureEnvelopeProtected(dir, key, digest, nowMs)) {
                    "STAGED", "LAG_EXCEEDED", "ARCHIVE_ACKED", "RELEASED" -> EnvResult.IDEMPOTENT
                    else -> EnvResult.STORED_UNPROTECTED
                }
            }
            addConflict(dir, JSONObject().put("kind", "envelope").put("slot_key", key).put("kept_sha256", cur.optString("sha256"))
                .put("rejected_sha256", digest).put("rejected_status", env.optString("status")).put("at_ms", nowMs))
            return EnvResult.CONFLICT
        }
        if (!reclaimEnvelopes(dir, MAX_ENVELOPE_FILES - 1)) return EnvResult.CAPACITY_BACKLOG
        // B1-R3: admission is checked for entry count AND sealed ledger bytes before anything is committed.
        if (ResearchProtection.admission(dir, envelopeEntryId(key), key, digest) != null) return EnvResult.CAPACITY_BACKLOG
        // The exact envelope text is stored once (the archive/protection digest is over these bytes).
        writeAtomic(f, JSONObject().put("sha256", digest).put("stored_ms", nowMs).put("envelope_text", body).toString())
        M0Durable.crashPoint("env_committed_before_protection")
        when (ensureEnvelopeProtected(dir, key, digest, nowMs)) {
            "STAGED" -> EnvResult.STORED
            else -> EnvResult.STORED_UNPROTECTED   // bytes preserved, refusal flagged and counted; retry/reconcile repairs
        }
    }

    private fun envelopeRequest(dir: File, key: String, digest: String, nowMs: Long) =
        ResearchProtection.request(dir, envelopeEntryId(key), PROTECTION_OWNER, "COMBINED", key, key.substringBefore("|"),
            listOf("FORECAST_ENVELOPE"), nowMs, contentSha = digest)

    /** Returns the entry state after ensuring one exists; on refusal the envelope is recorded as UNPROTECTED (flag file). */
    internal fun ensureEnvelopeProtected(dir: File, key: String, digest: String, nowMs: Long): String {
        val existing = ResearchProtection.entry(dir, envelopeEntryId(key))
        val st = if (existing != null) {
            if (existing.optString("content_sha256") == digest) existing.optString("state") else "IDENTITY_CONFLICT"
        } else envelopeRequest(dir, key, digest, nowMs)
        val flags = readOrNull(unprotectedFile(dir)) ?: JSONObject()
        val ok = st in setOf("STAGED", "LAG_EXCEEDED", "ARCHIVE_ACKED", "RELEASED")
        if (ok && flags.has(key)) { flags.remove(key); writeAtomic(unprotectedFile(dir), flags.toString()) }
        if (!ok && flags.optJSONObject(key)?.optString("reason") != st) {
            flags.put(key, JSONObject().put("reason", st).put("sha256", digest).put("at_ms", nowMs))
            writeAtomic(unprotectedFile(dir), flags.toString())
        }
        return st
    }
    private fun unprotectedFile(dir: File) = File(dir, "envunprotected.json")
    fun unprotectedEnvelopes(dir: File): JSONObject = readOrNull(unprotectedFile(dir)) ?: JSONObject()

    /** B1-R3 reconcile: every committed envelope must have a protection entry (repair) or a visible flag. */
    internal fun reconcileEnvelopes(dir: File, nowMs: Long): Int {
        var n = 0
        val fs = dir.listFiles { x -> x.name.startsWith("env_") && x.name.endsWith(".json") } ?: return 0
        for (f in fs.sortedBy { it.name }) {
            val rec = readOrNull(f) ?: continue
            val text = rec.optString("envelope_text").takeIf { it.isNotBlank() } ?: continue
            val key = try { JSONObject(text).optString("source_poll_slot_key") } catch (_: Exception) { null } ?: continue
            if (ResearchProtection.entry(dir, envelopeEntryId(key)) != null) continue
            ensureEnvelopeProtected(dir, key, sha256(text), nowMs); n++
        }
        return n
    }

    internal fun reclaimEnvelopes(dir: File, keep: Int): Boolean {
        val fs = dir.listFiles { x -> x.name.startsWith("env_") && x.name.endsWith(".json") }?.sortedBy { it.name } ?: return true
        if (fs.size <= keep) return true
        var excess = fs.size - keep
        for (f in fs) {
            if (excess <= 0) break
            val key = readOrNull(f)?.optString("envelope_text")?.let { try { JSONObject(it).optString("source_poll_slot_key") } catch (_: Exception) { null } } ?: continue
            val id = envelopeEntryId(key)
            if (ResearchProtection.entry(dir, id)?.optString("state") != "ARCHIVE_ACKED") continue
            f.delete(); M0Durable.fsyncDir(dir)
            ResearchProtection.release(dir, id, System.currentTimeMillis())
            excess--
        }
        return excess <= 0
    }

    fun readEnvelope(dir: File, slotKey: String): JSONObject? = readJson(envFile(dir, slotKey))?.optString("envelope_text")
        ?.takeIf { it.isNotBlank() }?.let { JSONObject(it) }
    /** Exact archived bytes of an envelope (the digest the protection entry is bound to). */
    fun envelopeArchiveBytes(dir: File, slotKey: String): ByteArray? = readOrNull(envFile(dir, slotKey))?.optString("envelope_text")?.toByteArray(Charsets.UTF_8)

    /** Exact bytes of the current session manifest body (SESSION_MANIFEST archive content) and its sha256. */
    fun manifestArchive(dir: File, session: String): Pair<ByteArray, String>? =
        (M0Durable.read(manifestFile(dir, session)) as? M0Durable.Read.Ok)?.body?.let { it.toByteArray(Charsets.UTF_8) to M0Durable.sha256(it) }

    fun readEnvelopeRecordSha(dir: File, slotKey: String): String? = readOrNull(envFile(dir, slotKey))?.optString("sha256")

    // ------------------------------------------------------------------ conflicts
    private fun addConflict(dir: File, c: JSONObject) {
        val f = File(dir, "conflicts.json")
        val o = readOrNull(f) ?: JSONObject().put("total", 0).put("items", JSONArray())
        o.put("total", o.optInt("total") + 1)
        val items = o.getJSONArray("items"); items.put(c)
        while (items.length() > MAX_CONFLICTS) items.remove(0)
        writeAtomic(f, o.toString())
    }

    fun conflicts(dir: File): JSONObject = readOrNull(File(dir, "conflicts.json")) ?: JSONObject().put("total", 0).put("items", JSONArray())

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
        val files = manifestFiles(dir).sortedByDescending { it.name }
        val arr = JSONArray()
        var latestDurable = false
        for (mf in files.take(sessions)) {
            val m = readOrNull(mf) ?: continue
            val s = JSONObject().put("session_date", m.optString("session_date")).put("created_by", m.optString("created_by"))
            val idx = m.getJSONObject("indices")
            for (index in ResearchCalendar.INDICES) {
                val io = idx.optJSONObject(index) ?: continue
                val c = JSONObject().put("available", io.optBoolean("available")).put("reason", io.opt("reason"))
                    .put("expected", io.optInt("expected")).put("regime", io.optString("regime", "UNKNOWN"))
                    .put("session_kind", io.optString("session_kind", "REGULAR"))
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
        val ev = JSONObject(); listOf("VALID", "NEUTRAL", "MISSING", "MALFORMED", "FORECAST_UNAVAILABLE").forEach { ev.put(it, 0) }
        dir.listFiles { f -> f.name.startsWith("env_${safe(today)}") && f.name.endsWith(".json") }?.forEach { f ->
            val st = readOrNull(f)?.optString("envelope_text")?.let { try { JSONObject(it).optString("status") } catch (_: Exception) { null } } ?: return@forEach
            ev.put(st, ev.optInt(st) + 1)
        }
        out.put("envelopes_today", ev).put("envelopes_upload", "LOCAL_ONLY_UPLOAD_NOT_DEPLOYED")
        out.put("conflicts_total", conflicts(dir).optInt("total"))
        val prot = ResearchProtection.summary(dir, today)
        out.put("protection", prot)
        out.put("envelopes_unprotected", unprotectedEnvelopes(dir).length())
        val integ = integrity(dir)
        out.put("corrupt_files", integ)
        val todayPlan = ResearchCalendar.plan(today, "NF")
        val label = when {
            prot.optString("integrity") == "LEDGER_CORRUPT" || integ.length() > 0 -> "INTEGRITY_FAULT"
            !enabled -> "DISABLED"
            prot.optString("integrity") == "BACKLOG_AT_CAPACITY" -> "BACKLOG_AT_CAPACITY"
            !todayPlan.available -> "CALENDAR_UNAVAILABLE"
            latestDurable -> "COLLECTING"
            else -> "ENABLED_NOT_CAPTURING"
        }
        out.put("label", label).put("label_text", when (label) {
            "COLLECTING" -> "Full-day research — collecting"
            "DISABLED" -> "Full-day research — collector off"
            "INTEGRITY_FAULT" -> "Full-day research — evidence integrity fault (corrupt ledger/file; not healthy)"
            "BACKLOG_AT_CAPACITY" -> "Full-day research — archive backlog at capacity; capture paused"
            "CALENDAR_UNAVAILABLE" -> "Full-day research — calendar unavailable (coverage unknown)"
            else -> "Full-day research — enabled, not currently capturing"
        })
    }
}

/**
 * W5a: instrument-map admission. A map version is ADMITTED only when it is listed in the sealed "imapindex.json"
 * together with a lease (the attempt that will reference it). Pruning removes only maps with no lease and no
 * manifest reference; when the cap (count AND bytes) cannot be met, the new map is rejected explicitly
 * (IMAP_CAPACITY) - a successful MapRef therefore always resolves.
 */
object InstrumentMaps {
    private fun indexFile(dir: File) = File(dir, "imapindex.json")
    private fun mapFile(dir: File, v: String) = File(dir, "imap_$v.json")

    private fun marker(dir: File) = File(dir, "imap.established")
    private fun mapFiles(dir: File) = dir.listFiles { x -> x.name.startsWith("imap_") && x.name.endsWith(".json") }?.toList() ?: emptyList()

    /**
     * B1-R4: a CORRUPT index fails closed (null; nothing is deleted). An ABSENT index is first-use only when no
     * marker and no map file exist; otherwise the index was lost and is rebuilt by VALIDATED recovery from the
     * committed map files (each sealed record re-verified; nothing deleted; unverifiable files kept and counted).
     */
    private fun loadIndex(dir: File): JSONObject? = when (val r = M0Durable.read(indexFile(dir))) {
        is M0Durable.Read.Ok -> r.json
        is M0Durable.Read.Corrupt -> null
        is M0Durable.Read.Absent -> {
            val ix = JSONObject().put("maps", JSONObject())
            if (M0Durable.read(marker(dir)) is M0Durable.Read.Absent && mapFiles(dir).isEmpty()) ix
            else { adoptUnindexed(dir, ix); ix.put("rebuilt_ms", System.currentTimeMillis()).put("rebuild_reason", "index_lost"); saveIndex(dir, ix); ix }
        }
    }

    private fun saveIndex(dir: File, ix: JSONObject) {
        M0Durable.write(indexFile(dir), ix.toString())
        if (M0Durable.read(marker(dir)) is M0Durable.Read.Absent)
            M0Durable.write(marker(dir), JSONObject().put("established_ms", System.currentTimeMillis()).toString())
    }

    /** Adopt committed-but-unindexed map files (crash before admission / index loss); never delete them. */
    private fun adoptUnindexed(dir: File, ix: JSONObject) {
        val maps = ix.getJSONObject("maps")
        val unverified = JSONObject()
        for (f in mapFiles(dir)) {
            val v = f.name.removePrefix("imap_").removeSuffix(".json")
            if (maps.has(v)) continue
            val r = M0Durable.read(f)
            if (r is M0Durable.Read.Ok && r.json.optString("sha256") == v && M0Durable.sha256(r.json.optString("lines")) == v)
                maps.put(v, JSONObject().put("index", r.json.optString("index")).put("bytes", f.length())
                    .put("created_ms", r.json.optLong("created_ms")).put("leases", JSONObject()).put("adopted", true))
            else unverified.put(f.name, f.length())
        }
        ix.put("unverified", unverified)
    }

    private fun unverifiedBytes(ix: JSONObject): Long = ix.optJSONObject("unverified")?.let { u -> u.keys().asSequence().sumOf { u.optLong(it) } } ?: 0L

    fun admitted(dir: File): Set<String> = loadIndex(dir)?.getJSONObject("maps")?.keys()?.asSequence()?.toSet() ?: emptySet()

    fun put(dir: File, index: String, lines: List<String>, nowMs: Long, leaseId: String?): ResearchCollector.MapRef {
        if (index !in ResearchCalendar.INDICES) return ResearchCollector.MapRef.Unavailable("UNKNOWN_INDEX")
        if (lines.isEmpty()) return ResearchCollector.MapRef.Unavailable("NO_INSTRUMENT_KEYS")
        if (lines.any { !it.startsWith("$index|") }) return ResearchCollector.MapRef.Unavailable("CROSS_INDEX_ENTRY")
        val ix = loadIndex(dir) ?: return ResearchCollector.MapRef.Unavailable("IMAP_INDEX_CORRUPT")
        val maps = ix.getJSONObject("maps")
        val text = lines.joinToString("\n")
        val v = M0Durable.sha256(text)
        val f = mapFile(dir, v)
        maps.optJSONObject(v)?.let { e ->
            val r = M0Durable.read(f)
            if (r !is M0Durable.Read.Ok || r.json.optString("sha256") != v || M0Durable.sha256(r.json.optString("lines")) != v)
                return ResearchCollector.MapRef.Unavailable("IMAP_STORE_CORRUPT")
            if (leaseId != null) { e.getJSONObject("leases").put(leaseId, nowMs); saveIndex(dir, ix) }
            return ResearchCollector.MapRef.Ok(v, false)
        }
        // B1-R5: the cap is measured in the exact sealed UTF-8 bytes this map will occupy on disk.
        val body = JSONObject().put("schema", ResearchCollector.SCHEMA).put("index", index).put("sha256", v).put("lines", text)
            .put("entries", lines.size).put("created_ms", nowMs).toString()
        val bytes = M0Durable.sealedSize(f.name, body)
        if (bytes > ResearchCollector.MAX_IMAP_BYTES) return ResearchCollector.MapRef.Unavailable("IMAP_CAPACITY")
        when (reclaim(dir, ix, ResearchCollector.MAX_IMAP_FILES - 1, ResearchCollector.MAX_IMAP_BYTES - bytes)) {
            Reclaim.OK -> {}
            Reclaim.FULL -> return ResearchCollector.MapRef.Unavailable("IMAP_CAPACITY")
            Reclaim.REFERENCES_UNKNOWN -> return ResearchCollector.MapRef.Unavailable("IMAP_CAPACITY_REFERENCES_UNKNOWN")
        }
        // Map bytes first (adopted, never deleted, if a crash precedes admission), then the admission record.
        M0Durable.write(f, body)
        val back = M0Durable.read(f)
        if (back !is M0Durable.Read.Ok || back.json.optString("sha256") != v) return ResearchCollector.MapRef.Unavailable("IMAP_WRITE_UNVERIFIED")
        maps.put(v, JSONObject().put("index", index).put("bytes", f.length()).put("created_ms", nowMs)
            .put("leases", JSONObject().apply { if (leaseId != null) put(leaseId, nowMs) }))
        saveIndex(dir, ix)
        return ResearchCollector.MapRef.Ok(v, true)
    }

    enum class Reclaim { OK, FULL, REFERENCES_UNKNOWN }

    private fun reclaim(dir: File, ix: JSONObject, keepCount: Int, keepBytes: Long): Reclaim {
        val maps = ix.getJSONObject("maps")
        // B1-R4: unindexed files are adopted after validation (or kept as unverified), never deleted by absence.
        if (mapFiles(dir).any { !maps.has(it.name.removePrefix("imap_").removeSuffix(".json")) && ix.optJSONObject("unverified")?.has(it.name) != true }) {
            val keepUnverified = ix.optJSONObject("unverified")
            adoptUnindexed(dir, ix)
            keepUnverified?.let { k -> val u = ix.getJSONObject("unverified"); for (n in k.keys()) if (!u.has(n)) u.put(n, k.optLong(n)) }
            saveIndex(dir, ix)
        }
        fun total() = maps.keys().asSequence().sumOf { maps.getJSONObject(it).optLong("bytes") } + unverifiedBytes(ix)
        fun fits() = maps.length() <= keepCount && total() <= keepBytes
        if (fits()) return Reclaim.OK
        val referenced = ResearchCollector.referencedMaps(dir) ?: return Reclaim.REFERENCES_UNKNOWN
        val candidates = maps.keys().asSequence().toList()
            .filter { maps.getJSONObject(it).getJSONObject("leases").length() == 0 && it !in referenced }
            .sortedBy { maps.getJSONObject(it).optLong("created_ms") }
        for (v in candidates) {
            if (maps.length() <= keepCount && total() <= keepBytes) break
            maps.remove(v)
            saveIndex(dir, ix)   // un-admit first, then delete bytes
            mapFile(dir, v).delete()
        }
        return if (fits()) Reclaim.OK else Reclaim.FULL
    }

    fun releaseLease(dir: File, v: String, leaseId: String) {
        val ix = loadIndex(dir) ?: return
        val e = ix.getJSONObject("maps").optJSONObject(v) ?: return
        if (e.getJSONObject("leases").remove(leaseId) != null) saveIndex(dir, ix)
    }

    fun expireLeases(dir: File, olderThanMs: Long) {
        val ix = loadIndex(dir) ?: return
        var changed = false
        val maps = ix.getJSONObject("maps")
        for (k in maps.keys()) { val l = maps.getJSONObject(k).getJSONObject("leases")
            for (a in l.keys().asSequence().toList()) if (l.optLong(a) < olderThanMs) { l.remove(a); changed = true } }
        if (changed) saveIndex(dir, ix)
    }

    fun releaseAllLeases(dir: File, leaseId: String) {
        val ix = loadIndex(dir) ?: return
        var changed = false
        val maps = ix.getJSONObject("maps")
        for (k in maps.keys()) if (maps.getJSONObject(k).getJSONObject("leases").remove(leaseId) != null) changed = true
        if (changed) saveIndex(dir, ix)
    }
}

/**
 * B5 + Batch 1 W4/W5 protection ledger. Records which evidence a research owner needs until an archive is
 * acknowledged against a VERIFIED MANIFEST of exactly that evidence (index, poll_ts, roles, digest, size, rows).
 *
 * States: STAGED -> (LAG_EXCEEDED: persistent incomplete, still protected) -> ARCHIVE_ACKED -> RELEASED (local
 * copy deleted). Only ARCHIVE_ACKED evidence may be deleted. Bounded by entries AND bytes: active entries
 * <= [CAPACITY]; RELEASED entries beyond [FINAL_RETAIN] are compacted into a durable bounded summary with a hash
 * chain. At the cap new requests are CAPACITY_REJECTED and capture admission is refused (visible backlog).
 *
 * N8: a first-use ABSENT ledger (no "protection.established" marker) is distinguished from an unreadable or lost
 * ESTABLISHED ledger. Corruption is persisted as LEDGER_CORRUPT ("ledgerstatus.json"), is never reported as an
 * empty healthy history, and no request/ack ever overwrites the corrupt file.
 */
object ResearchProtection {
    @Volatile var CAPACITY = 4000
    @Volatile var FINAL_RETAIN = 200
    @Volatile var MAX_BYTES = 2_500_000
    const val LAG_LIMIT_SESSIONS = 20
    const val REJECT_RESERVE_BYTES = 512
    /** B1-R5: bounded growth a still-protected entry may add later (ack fields / ack-failure / lag bookkeeping). */
    const val ENTRY_GROWTH_RESERVE_BYTES = 448
    /** Exact on-disk (sealed, UTF-8) size of the ledger body. A replacement write transiently needs one more copy
     *  (temp file) of at most MAX_BYTES - so the peak footprint is bounded by 2 * MAX_BYTES. */
    fun sealedBytes(o: JSONObject): Long = M0Durable.sealedSize("protection.json", o.toString())
    val ACTIVE = setOf("STAGED", "LAG_EXCEEDED", "ARCHIVE_ACKED")
    val PROTECTED = setOf("STAGED", "LAG_EXCEEDED")
    val INDEX_OK = setOf("NF", "BNF", "COMBINED", "SESSION")
    private val lock = Any()

    private fun file(dir: File) = File(dir, "protection.json")
    private fun marker(dir: File) = File(dir, "protection.established")
    private fun statusFile(dir: File) = File(dir, "ledgerstatus.json")

    sealed class Load {
        data class Fresh(val o: JSONObject) : Load()
        data class Ok(val o: JSONObject) : Load()
        data class Corrupt(val reason: String) : Load()
    }

    private fun empty() = JSONObject().put("entries", JSONObject()).put("rejected_total", 0)
        .put("compacted", JSONObject().put("total", 0).put("by_state", JSONObject()).put("chain", ""))

    fun load(dir: File): Load {
        (M0Durable.read(statusFile(dir)) as? M0Durable.Read.Ok)?.json?.let {
            if (it.optString("state") == "LEDGER_CORRUPT") return Load.Corrupt(it.optString("reason"))
        }
        val established = M0Durable.read(marker(dir))
        val r = M0Durable.read(file(dir))
        val res: Load = when {
            r is M0Durable.Read.Ok -> Load.Ok(r.json)
            r is M0Durable.Read.Corrupt -> Load.Corrupt("ledger_unreadable:" + r.reason)
            established is M0Durable.Read.Absent -> Load.Fresh(empty())
            else -> Load.Corrupt("established_ledger_lost")
        }
        if (res is Load.Corrupt) persistCorrupt(dir, res.reason)
        return res
    }

    private fun persistCorrupt(dir: File, reason: String) {
        try {
            val cur = (M0Durable.read(statusFile(dir)) as? M0Durable.Read.Ok)?.json
            if (cur?.optString("state") == "LEDGER_CORRUPT") return
            M0Durable.write(statusFile(dir), JSONObject().put("state", "LEDGER_CORRUPT").put("reason", reason)
                .put("detected_ms", System.currentTimeMillis()).toString())
        } catch (_: Throwable) {}
    }

    /** Ledger first, then the "established" marker: a crash before the first ledger commit leaves a genuine
     *  first-use state (reported NO_EVIDENCE, never OK); once established, a missing ledger is LEDGER_CORRUPT. */
    private fun save(dir: File, o: JSONObject, fresh: Boolean) {
        M0Durable.write(file(dir), o.toString())
        if (fresh || M0Durable.read(marker(dir)) is M0Durable.Read.Absent)
            M0Durable.write(marker(dir), JSONObject().put("established_ms", System.currentTimeMillis()).toString())
    }

    private inline fun <T> withLedger(dir: File, onCorrupt: (String) -> T, block: (JSONObject, Boolean) -> T): T {
        return when (val l = load(dir)) {
            is Load.Corrupt -> onCorrupt(l.reason)
            is Load.Fresh -> block(l.o, true)
            is Load.Ok -> block(l.o, false)
        }
    }

    private fun activeCount(e: JSONObject) = e.keys().asSequence().count { e.getJSONObject(it).optString("state") in ACTIVE }

    /** null when capture may proceed, else the visible refusal reason. */
    fun admission(dir: File, probeId: String? = null, probePoll: String = "", probeSha: String? = null): String? = synchronized(lock) {
        withLedger(dir, { "LEDGER_CORRUPT" }) { o, _ ->
            val e = o.getJSONObject("entries")
            when {
                probeId != null && e.has(probeId) -> null
                activeCount(e) >= CAPACITY -> "PROTECTION_BACKLOG_AT_CAPACITY"
                !fitsBytes(o, probeId ?: "probe", newEntry("research_m0", "COMBINED", probePoll, probePoll.substringBefore("|"),
                    listOf("FORECAST_ENVELOPE"), 0L, null, probeSha ?: "0".repeat(64), null)) -> "PROTECTION_BACKLOG_BYTES"
                else -> null
            }
        }
    }

    private fun newEntry(owner: String, index: String, pollTs: String, session: String, roles: List<String>, nowMs: Long,
                         expectedRows: Int?, contentSha: String?, sourceSha: String?): JSONObject =
        JSONObject().put("owner", owner).put("index", index).put("poll_ts", pollTs).put("session", session)
            .put("roles", JSONArray(roles.sorted())).put("state", "STAGED").put("created_ms", nowMs)
            .put("expected_rows", expectedRows ?: JSONObject.NULL).put("content_sha256", contentSha ?: JSONObject.NULL)
            .put("source_rows_sha256", sourceSha ?: JSONObject.NULL)

    /** B1-R5: would the sealed ledger (with [x] added) plus growth and rejection reserves stay within MAX_BYTES? */
    private fun fitsBytes(o: JSONObject, id: String, x: JSONObject): Boolean {
        val e = o.getJSONObject("entries")
        val had = e.opt(id)
        e.put(id, x)
        val size = sealedBytes(o)
        val protectedN = e.keys().asSequence().count { e.getJSONObject(it).optString("state") in PROTECTED }
        if (had == null) e.remove(id) else e.put(id, had)
        return size + protectedN.toLong() * ENTRY_GROWTH_RESERVE_BYTES <= MAX_BYTES - REJECT_RESERVE_BYTES
    }

    private val IDENTITY_FIELDS = listOf("owner", "index", "poll_ts", "session", "roles", "expected_rows", "content_sha256", "source_rows_sha256")

    fun request(dir: File, id: String, owner: String, index: String, pollTs: String, session: String,
                roles: List<String>, nowMs: Long, capacity: Int = CAPACITY, expectedRows: Int? = null, contentSha: String? = null,
                sourceSha: String? = null): String = synchronized(lock) {
        if (index !in INDEX_OK) return "UNKNOWN_INDEX"
        withLedger(dir, { "LEDGER_CORRUPT" }) { o, fresh ->
            val e = o.getJSONObject("entries")
            val x = newEntry(owner, index, pollTs, session, roles, nowMs, expectedRows, contentSha, sourceSha)
            e.optJSONObject(id)?.let { cur ->
                // B1-R2: an existing id is only reused for IDENTICAL evidence identity; anything else is a conflict.
                val diff = IDENTITY_FIELDS.filter { f -> cur.opt(f)?.toString() != x.opt(f)?.toString() }
                if (diff.isEmpty()) return@withLedger cur.optString("state")
                o.put("identity_conflicts_total", o.optInt("identity_conflicts_total") + 1)
                o.put("last_identity_conflict", JSONObject().put("id", id.take(120)).put("fields", JSONArray(diff)).put("at_ms", nowMs))
                if (sealedBytes(o) > MAX_BYTES) o.remove("last_identity_conflict")
                save(dir, o, fresh)
                return@withLedger "IDENTITY_CONFLICT"
            }
            val reject = when {
                activeCount(e) >= capacity -> "capacity_entries"
                // B1-R5: sealed UTF-8 bytes + growth reserve + rejection-bookkeeping reserve
                !fitsBytes(o, id, x) -> "capacity_bytes"
                else -> null
            }
            if (reject != null) {
                o.put("rejected_total", o.optInt("rejected_total") + 1)
                o.put("last_rejected", JSONObject().put("id", id.take(120)).put("at_ms", nowMs).put("reason", reject))
                if (sealedBytes(o) > MAX_BYTES) o.remove("last_rejected")   // the counter alone always fits the reserve
                save(dir, o, fresh)
                return@withLedger "CAPACITY_REJECTED"
            }
            e.put(id, x)
            save(dir, o, fresh)
            "STAGED"
        }
    }

    /**
     * Acknowledge an archive. [manifest] must name exactly this entry (entry_id, index, poll_ts, roles) and the
     * archived [content]'s sha256 and size; role-specific checks bind the content to the evidence:
     *  - CHAIN_POLL: content is JSON {index, poll_ts, rows:[...]} with rows == the captured chain_rows;
     *  - FORECAST_ENVELOPE / SESSION_MANIFEST: content digest equals the stored evidence digest.
     * Arbitrary bytes are rejected ("REJECTED:<reason>") and the entry stays protected.
     */
    fun acknowledgeArchive(dir: File, id: String, manifest: JSONObject, content: ByteArray, nowMs: Long,
                           currentEvidenceSha: String? = null): String = synchronized(lock) {
        withLedger(dir, { "LEDGER_CORRUPT" }) { o, fresh ->
            val x = o.getJSONObject("entries").optJSONObject(id) ?: return@withLedger "UNKNOWN"
            val st = x.optString("state")
            if (st !in PROTECTED) return@withLedger st
            val actual = M0Durable.sha256(content)
            val roles = (0 until x.getJSONArray("roles").length()).map { x.getJSONArray("roles").getString(it) }
            val mRoles = manifest.optJSONArray("roles")?.let { a -> (0 until a.length()).map { a.optString(it) }.sorted() }
            val err: String? = when {
                content.isEmpty() -> "empty_content"
                manifest.optString("entry_id") != id -> "manifest_entry_mismatch"
                manifest.optString("index") != x.optString("index") -> "manifest_index_mismatch"
                manifest.optString("poll_ts") != x.optString("poll_ts") -> "manifest_poll_mismatch"
                mRoles != roles -> "manifest_roles_mismatch"
                manifest.optString("content_sha256") != actual -> "content_sha_mismatch"
                manifest.optLong("content_bytes", -1) != content.size.toLong() -> "content_size_mismatch"
                "CHAIN_POLL" in roles -> {
                    val j = try { JSONObject(String(content, Charsets.UTF_8)) } catch (_: Exception) { null }
                    val rows = j?.optJSONArray("rows")
                    when {
                        j == null || rows == null -> "chain_content_unparseable"
                        j.optString("index") != x.optString("index") || j.optString("poll_ts") != x.optString("poll_ts") -> "chain_content_identity_mismatch"
                        x.isNull("expected_rows") -> "expected_rows_unknown"
                        rows.length() != x.optInt("expected_rows") || manifest.optInt("row_count", -1) != rows.length() -> "row_count_mismatch"
                        // B1-R1: rows must reproduce the independently captured source digest field-for-field.
                        x.isNull("source_rows_sha256") || x.optString("source_rows_sha256").isBlank() -> "source_proof_unavailable"
                        else -> {
                            val canon = ResearchCollector.canonicalArchiveRows(rows)
                            when {
                                canon == null -> "chain_rows_not_canonical"
                                ResearchCollector.canonicalRowsDigest(x.optString("index"), canon) != x.optString("source_rows_sha256") -> "source_rows_mismatch"
                                else -> null
                            }
                        }
                    }
                }
                "FORECAST_ENVELOPE" in roles -> if (x.isNull("content_sha256") || actual != x.optString("content_sha256")) "envelope_digest_mismatch" else null
                "SESSION_MANIFEST" in roles -> if (currentEvidenceSha == null || actual != currentEvidenceSha) "session_manifest_not_current" else null
                else -> "unknown_role"
            }
            if (err != null) {
                x.put("last_ack_error", err).put("ack_failures", x.optInt("ack_failures") + 1)
                save(dir, o, fresh); return@withLedger "REJECTED:$err"
            }
            x.put("state", "ARCHIVE_ACKED").put("archive_sha256", actual).put("archive_bytes", content.size).put("acked_ms", nowMs)
                .put("archive_manifest_sha256", M0Durable.sha256(manifest.toString()))
            save(dir, o, fresh); "ARCHIVE_ACKED"
        }
    }

    /** Local copy deleted after an ack: ARCHIVE_ACKED -> RELEASED, then bounded compaction. */
    fun release(dir: File, id: String, nowMs: Long): String = synchronized(lock) {
        withLedger(dir, { "LEDGER_CORRUPT" }) { o, fresh ->
            val x = o.getJSONObject("entries").optJSONObject(id) ?: return@withLedger "UNKNOWN"
            if (x.optString("state") != "ARCHIVE_ACKED") return@withLedger "NOT_ACKED:" + x.optString("state")
            x.put("state", "RELEASED").put("released_ms", nowMs)
            compact(o); save(dir, o, fresh); "RELEASED"
        }
    }

    fun sessionEntries(dir: File, session: String): List<Pair<String, JSONObject>> = synchronized(lock) {
        withLedger(dir, { emptyList() }) { o, _ ->
            val e = o.getJSONObject("entries")
            e.keys().asSequence().map { it to e.getJSONObject(it) }.filter { it.second.optString("session") == session && it.second.optString("index") != "COMBINED" }.toList()
        }
    }

    fun sessionManifestId(session: String) = "manifest|$session"

    /** A session manifest may be deleted only when its SESSION_MANIFEST entry and every chain entry are acked. */
    fun sessionReleasable(dir: File, session: String): Boolean {
        val es = sessionEntries(dir, session)
        val man = es.firstOrNull { it.first == sessionManifestId(session) } ?: return false
        if (man.second.optString("state") != "ARCHIVE_ACKED") return false
        return es.all { it.second.optString("state") in setOf("ARCHIVE_ACKED", "RELEASED") }
    }

    fun releaseSession(dir: File, session: String, nowMs: Long) {
        for ((id, x) in sessionEntries(dir, session)) if (x.optString("state") == "ARCHIVE_ACKED") release(dir, id, nowMs)
    }

    private fun compact(o: JSONObject) {
        val e = o.getJSONObject("entries")
        val rel = e.keys().asSequence().filter { e.getJSONObject(it).optString("state") == "RELEASED" }.toList()
            .sortedBy { e.getJSONObject(it).optLong("released_ms") }
        if (rel.size <= FINAL_RETAIN) return
        val c = o.optJSONObject("compacted") ?: JSONObject().put("total", 0).put("by_state", JSONObject()).put("chain", "")
        val bs = c.getJSONObject("by_state")
        for (id in rel.take(rel.size - FINAL_RETAIN)) {
            val x = e.getJSONObject(id)
            c.put("chain", M0Durable.sha256(c.optString("chain") + "|" + id + "|" + x.optString("state") + "|" + x.optString("archive_sha256")))
            bs.put(x.optString("state"), bs.optInt(x.optString("state")) + 1)
            c.put("total", c.optInt("total") + 1)
            e.remove(id)
        }
        o.put("compacted", c)
    }

    /** Sessions-lag check against the dated calendar; overdue STAGED entries become LAG_EXCEEDED (still protected). */
    fun enforceLag(dir: File, today: String, lagLimit: Int = LAG_LIMIT_SESSIONS): Int = synchronized(lock) {
        withLedger(dir, { 0 }) { o, fresh ->
            if (fresh) return@withLedger 0
            val e = o.getJSONObject("entries"); var n = 0
            for (k in e.keys()) {
                val x = e.getJSONObject(k)
                if (x.optString("state") != "STAGED") continue
                val lag = ResearchCalendar.regularSessionsAfter(LocalDate.parse(x.getString("session")), LocalDate.parse(today)) ?: continue
                if (lag > lagLimit) { x.put("state", "LAG_EXCEEDED").put("lag_sessions", lag); n++ }
            }
            if (n > 0) save(dir, o, false)
            n
        }
    }

    fun isProtected(dir: File, index: String, pollTs: String): Boolean = synchronized(lock) {
        withLedger(dir, { true }) { o, _ ->   // corrupt ledger: everything is treated as protected
            val e = o.getJSONObject("entries")
            e.keys().asSequence().any { val x = e.getJSONObject(it); x.optString("state") in PROTECTED && x.optString("index") == index && x.optString("poll_ts") == pollTs }
        }
    }

    fun entry(dir: File, id: String): JSONObject? = synchronized(lock) {
        withLedger(dir, { null }) { o, _ -> o.getJSONObject("entries").optJSONObject(id) }
    }

    fun summary(dir: File, today: String): JSONObject = synchronized(lock) {
        val base = JSONObject().put("capacity", CAPACITY).put("max_bytes", MAX_BYTES).put("final_retain", FINAL_RETAIN)
            .put("lag_limit_sessions", LAG_LIMIT_SESSIONS)
        withLedger(dir, { r -> base.put("integrity", "LEDGER_CORRUPT").put("corrupt_reason", r) }) { o, fresh ->
            val e = o.getJSONObject("entries")
            val c = JSONObject().put("STAGED", 0).put("LAG_EXCEEDED", 0).put("ARCHIVE_ACKED", 0).put("RELEASED", 0)
            for (k in e.keys()) { val s = e.getJSONObject(k).optString("state"); c.put(s, c.optInt(s) + 1) }
            base.put("counts", c).put("CAPACITY_REJECTED", o.optInt("rejected_total"))
                .put("compacted", o.optJSONObject("compacted") ?: JSONObject()).put("ledger_bytes", sealedBytes(o))
                .put("IDENTITY_CONFLICT", o.optInt("identity_conflicts_total"))
            val integrity = when {
                fresh || (e.length() == 0 && (o.optJSONObject("compacted")?.optInt("total") ?: 0) == 0) -> "NO_EVIDENCE"
                activeCount(e) >= CAPACITY -> "BACKLOG_AT_CAPACITY"
                c.optInt("LAG_EXCEEDED") + o.optInt("rejected_total") > 0 -> "INCOMPLETE_REPLAY"
                c.optInt("STAGED") > 0 -> "BACKLOG"
                else -> "OK"
            }
            base.put("integrity", integrity)
            // legacy flat keys for the PWA card
            for (k in c.keys()) base.put(k, c.optInt(k))
            base
        }
    }
}
