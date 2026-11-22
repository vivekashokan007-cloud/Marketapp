package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.ThreadPoolExecutor
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong

/**
 * Close-to-next-session research capture, version [VERSION].
 *
 * A SEPARATE research path. It does not touch the intraday premium pilot: the pilot's window
 * (PremiumCaptureCohort.WINDOW_OPEN_MIN..WINDOW_CLOSE_MIN, 09:20-15:05), its cohort, its store,
 * its counters and its export section are neither read nor written here, so the pilot's schedule
 * and denominator are unchanged.
 *
 * What it does, using ONLY the option-chain responses the live poll has already fetched (it makes
 * no request of its own):
 *  - near close (scheduler slots [CLOSE_FIRST_MIN]..[CLOSE_LAST_MIN] inclusive, 15:10-15:30, the
 *    matcher's NEAR_CLOSE_FROM..CLOSE_CLOCK) it pins, per index, the three fully paired strikes
 *    nearest that response's spot (six contracts), records each contract's book at every close
 *    slot, and ENQUEUES those exact contracts for the next REGULAR session from [ResearchCalendar];
 *  - at the next session's open (slots [REOPEN_FIRST_MIN]..[REOPEN_LAST_MIN] inclusive,
 *    09:15-09:45, the matcher's OPEN_CLOCK + OPENING_TOLERANCE_MIN) it records the SAME contracts
 *    by instrument key, refusing a roll or substitution rather than pinning a neighbour.
 *
 * The queue is persistent (one file per index), so a restart between close and open loses
 * nothing. NF and BNF are kept apart in every file, counter and export block.
 *
 * Every quote is RECEIPT-ONLY: the poll passes no vendor/exchange timestamp, so exchange
 * freshness is unknown and is labelled [FRESHNESS_RECEIPT_ONLY] rather than assumed.
 */
object CloseOpenCapture {
    const val VERSION = "close_open_capture_v1"
    const val DIR_NAME = "close_open_capture"
    /** The export key Claude's matcher reads for the follow-on stream (`--section premium_followon`). */
    const val SECTION_KEY = "premium_followon"
    const val SECTION_VERSION = "close_open_capture_section_v1"

    const val CLOSE_FIRST_MIN = 15 * 60 + 10
    const val CLOSE_LAST_MIN = 15 * 60 + 30
    const val REOPEN_FIRST_MIN = 9 * 60 + 15
    const val REOPEN_LAST_MIN = 9 * 60 + 45
    const val PAIRED_STRIKES = 3
    const val CONTRACTS_PER_INDEX = 6

    /** Store bound for observations. Counters, slot outcomes and the queue keep a reserve above it. */
    const val MAX_STORE_BYTES = 8L * 1024 * 1024
    const val CONTROL_RESERVE_BYTES = 256L * 1024
    /** One session file per index; observations beyond this are refused and COUNTED. */
    const val MAX_SESSION_FILE_BYTES = 192L * 1024
    /** The exported section must stay under this; above it records are excluded and SAID so. */
    const val MAX_EXPORT_SECTION_BYTES = 768 * 1024
    /** Queue entries kept per index; only terminal entries are ever pruned, oldest first. */
    const val MAX_QUEUE_ENTRIES = 240

    const val FRESHNESS_RECEIPT_ONLY = "RECEIPT_ONLY_UNKNOWN_FRESHNESS"
    const val QUOTE_SOURCE = "UPSTOX_V2_OPTION_CHAIN_LIVE_POLL_RESPONSE"

    val UNDERLYING = linkedMapOf("NF" to "NSE_INDEX|Nifty 50", "BNF" to "NSE_INDEX|Nifty Bank")
    private val IST: ZoneId = ZoneId.of("Asia/Kolkata")

    // queue states
    const val Q_QUEUED = "QUEUED"
    const val Q_OPEN_CAPTURED = "OPEN_CAPTURED"
    const val Q_OPEN_NOT_CAPTURED = "OPEN_NOT_CAPTURED"
    const val Q_REFUSED = "REFUSED_AT_ENQUEUE"
    const val Q_TARGET_PASSED = "TARGET_SESSION_PASSED_WITHOUT_CAPTURE"

    // refusal / outcome reasons
    const val R_NEXT_ON_OR_AFTER_EXPIRY = "next_session_on_or_after_expiry"
    const val R_CAL_NOT_COVERED = "calendar_not_covered"
    const val R_NOT_REGULAR = "session_not_regular"
    const val R_CLOSE_END_NOT_STATED = "close_session_end_not_stated_or_early"
    const val R_CHAIN_MISSING = "chain_missing"
    const val R_CONTRACT_ABSENT = "contract_absent_in_response"
    const val R_ROLL = "contract_identity_changed_roll"
    const val R_CHAIN_EXPIRY_DIFFERS = "response_expiry_differs_from_queued_contract"
    const val R_NO_TWO_SIDED = "no_two_sided_quote_in_reopen_window"
    const val R_NO_ATTEMPT = "no_poll_in_reopen_window"
    const val R_NO_QUALIFYING_CLOSE = "no_qualifying_close_quote"

    /**
     * Quote eligibility, R2. The SLOT is scheduling intent only; the RECEIPT instant decides
     * whether the quote was in hand inside the agreed window. Boundary convention: the receipt,
     * in IST, truncated to the minute (HH:MM), must lie in the window INCLUSIVE at both ends —
     * close [15:10, 15:30] = receipt in [15:10:00.000, 15:31:00.000); reopen [09:15, 09:45] =
     * receipt in [09:15:00.000, 09:46:00.000) — and on the session date. This is the matcher's own
     * minute-truncated receipt convention (`rec.hour * 60 + rec.minute`). A late final poll (a
     * 15:30 slot received at 15:31:00 or later; a 09:45 slot at 09:46:00 or later) is recorded
     * as a diagnostic with its reason and never qualifies; the close then rests on the latest
     * earlier qualifying slot, if any. A qualifying observation additionally needs an exact
     * verified identity (no disagreement), a TWO_SIDED book, and — before it moves the queue —
     * a durable record. Receipt-only quotes keep FRESHNESS_RECEIPT_ONLY.
     */
    const val ELIGIBILITY_RULE = "close_open_eligibility_v2_receipt_minute_inclusive"
    const val LOT_AUTHORITY_STATE = "ABSENT_NOT_CAPTURED_BY_THIS_PATH"

    /** Test seams (never set in production): simulate a crash after the session write, or a failed queue write. */
    @JvmField @Volatile var testHookAfterSessionWrite: ((String, String, String) -> Unit)? = null
    @JvmField @Volatile var testFailQueueWrite: Boolean = false

    data class Acquisition(val index: String, val chain: JSONObject?,
                           val requestStartedMs: Long, val completedAtMs: Long)

    // ───────────────────────────── control (own flag, default follows nothing) ─────────────
    fun root(filesDir: File) = File(filesDir, DIR_NAME)
    private fun controlFile(filesDir: File) = File(root(filesDir), "control.json")

    /** ON, OFF, or UNSET. UNSET is OFF: this capture never turns itself on. */
    fun controlState(filesDir: File): String = try {
        val f = controlFile(filesDir)
        if (!f.isFile) "UNSET" else if (JSONObject(f.readText()).optBoolean("enabled", false)) "ON" else "OFF"
    } catch (t: Throwable) { "UNREADABLE" }

    fun isEnabled(filesDir: File) = controlState(filesDir) == "ON"

    fun setEnabled(filesDir: File, enabled: Boolean): Boolean = synchronized(lock) {
        root(filesDir).mkdirs()
        atomicWrite(controlFile(filesDir), JSONObject().put("enabled", enabled)
            .put("version", VERSION).toString())
    }

    // ───────────────────────────── poll entry: bounded, off-poll ───────────────────────────
    private val lock = Any()
    private val overlapRejected = AtomicLong(0)
    private val submitted = AtomicLong(0)
    /** One running and one waiting job; a third concurrent submission is REFUSED and counted. */
    private val executor: ThreadPoolExecutor by lazy {
        ThreadPoolExecutor(1, 1, 30, TimeUnit.SECONDS, ArrayBlockingQueue(1)) { r ->
            java.lang.Thread(r, "close-open-capture").apply { isDaemon = true }
        }.also { it.allowCoreThreadTimeOut(true) }
    }

    /**
     * Called from the poll with chains already in hand. Returns at once; never throws.
     * Outside both capture windows it does no file I/O at all.
     */
    fun onPoll(filesDir: File, sessionDate: String, declaredSlot: String?,
               acquisitions: List<Acquisition>): JSONObject {
        val out = JSONObject().put("version", VERSION)
        return try {
            // window first, so outside both windows no file is read at all (not even the control)
            val slot = declaredSlot ?: return out.put("status", "NO_DECLARED_SLOT")
            val kind = slotKind(slot) ?: return out.put("status", "OUTSIDE_WINDOWS")
            if (!isEnabled(filesDir)) return out.put("status", "DISABLED")
            val rejectedSoFar = overlapRejected.get()
            try {
                executor.execute {
                    try { process(filesDir, sessionDate, slot, acquisitions, overlapSince = rejectedSoFar) }
                    catch (t: Throwable) { /* recorded as absent work; never escapes */ }
                }
                submitted.incrementAndGet()
                out.put("status", "SUBMITTED").put("kind", kind).put("status_describes", "SUBMISSION_NOT_PERSISTENCE")
            } catch (e: RejectedExecutionException) {
                overlapRejected.incrementAndGet()
                out.put("status", "REFUSED_OVERLAP").put("kind", kind)
            }
        } catch (t: Throwable) {
            out.put("status", "ERROR").put("error", t.javaClass.simpleName)
        }
    }

    fun slotKind(slot: String?): String? {
        val m = minuteOf(slot) ?: return null
        return when (m) {
            in CLOSE_FIRST_MIN..CLOSE_LAST_MIN -> "CLOSE"
            in REOPEN_FIRST_MIN..REOPEN_LAST_MIN -> "REOPEN"
            else -> null
        }
    }

    // ───────────────────────────── the synchronous core ────────────────────────────────────
    /**
     * Process one poll for every index, synchronously. Per index, per stage, the outcome is
     * written into that index's session file: scheduled -> quote received -> durable write.
     * (Export inclusion is computed when the section is built.)
     */
    fun process(filesDir: File, sessionDate: String, slot: String,
                acquisitions: List<Acquisition>, overlapSince: Long = 0L): JSONObject = synchronized(lock) {
        val started = System.nanoTime()
        val res = JSONObject().put("session", sessionDate).put("slot", slot)
        val kind = slotKind(slot)
        res.put("kind", kind ?: JSONObject.NULL)
        if (kind == null) return res.put("status", "OUTSIDE_WINDOWS")
        val date = parseDate(sessionDate) ?: return res.put("status", "SESSION_DATE_INVALID")
        root(filesDir).mkdirs()
        val per = JSONObject()
        // fixed index order; each index is independent and one cannot block the other
        for (ix in UNDERLYING.keys) {
            val acq = acquisitions.firstOrNull { it.index.trim().uppercase() == ix }
            per.put(ix, try {
                if (kind == "CLOSE") closeSlot(filesDir, date, slot, ix, acq, overlapSince)
                else reopenSlot(filesDir, date, slot, ix, acq, overlapSince)
            } catch (t: Throwable) {
                JSONObject().put("status", "INDEX_ERROR").put("error", t.javaClass.simpleName)
            })
        }
        res.put("indices", per).put("status", "PROCESSED")
            .put("elapsed_us", (System.nanoTime() - started) / 1000)
    }

    // ---------------------------------------------------------------- calendar
    data class CalendarView(val isSession: Boolean?, val regular: Boolean, val next: LocalDate?,
                            val reason: String?, val regime: String?, val nextGap: Int?)

    /** Next REGULAR session after [d], or the reason there is none (never guessed). */
    fun nextRegularSession(d: LocalDate, limit: Int = 12): Pair<LocalDate?, String?> {
        var x = d
        for (i in 0 until limit) {
            x = x.plusDays(1)
            val s = ResearchCalendar.isSession(x) ?: return null to R_CAL_NOT_COVERED
            if (s && ResearchCalendar.specialSession(x) == null) return x to null
        }
        return null to "no_regular_session_within_${limit}_days"
    }

    fun calendarFor(d: LocalDate): CalendarView {
        val s = ResearchCalendar.isSession(d)
        val regular = s == true && ResearchCalendar.specialSession(d) == null &&
            d.toString() !in ResearchCalendar.PENDING_SPECIAL_SESSIONS
        val (next, why) = nextRegularSession(d)
        return CalendarView(s, regular, next, why, ResearchCalendar.regimeOf(d)?.id,
            next?.let { (it.toEpochDay() - d.toEpochDay()).toInt() })
    }

    /** The close window must end no later than BOTH declared session ends; unknown -> refuse. */
    fun closeWindowFits(d: LocalDate): Boolean {
        val r = ResearchCalendar.regimeOf(d) ?: return false
        val a = r.nonExpiringEndMinute ?: return false
        val b = r.expiringEndMinute ?: return false
        return CLOSE_LAST_MIN <= minOf(a, b)
    }

    // ---------------------------------------------------------------- enqueue decision
    /**
     * The one enqueue rule, used both at close and when [reconcile] rebuilds a missing entry, so
     * recovery can never bypass the calendar or expiry refusals.
     */
    private fun applyEnqueueDecision(e: JSONObject, d: LocalDate, pin: JSONObject, cal: CalendarView) {
        val expiry = pin.getString("expiry")
        val next = cal.next
        when {
            next == null -> e.put("state", Q_REFUSED).put("reason", cal.reason ?: R_CAL_NOT_COVERED)
            next.toString() >= expiry -> e.put("state", Q_REFUSED).put("reason", R_NEXT_ON_OR_AFTER_EXPIRY)
                .put("target_session", next.toString())
            else -> e.put("state", Q_QUEUED).put("target_session", next.toString())
                .put("calendar_gap_days", cal.nextGap ?: JSONObject.NULL)
        }
    }

    private fun newEntry(ix: String, d: LocalDate, pin: JSONObject, slot: String): JSONObject =
        JSONObject().put("key", d.toString() + "|" + pin.getString("instrument_key"))
            .put("index", ix).put("close_session", d.toString())
            .put("contract", JSONObject(pin.toString())).put("enqueued_slot", slot)

    /** A record that is in a durable session file AND met the receipt-window/identity/book rule. */
    private fun qualifies(rec: JSONObject?): Boolean =
        rec?.optJSONObject("eligibility")?.optBoolean("quote_ready", false) == true

    /** Write the queue and SAY whether it reached disk; a failure is put on the slot outcome and counted. */
    private fun writeQueueReported(filesDir: File, d: LocalDate, ix: String, q: JSONObject,
                                   sess: JSONObject?, so: JSONObject?): Boolean {
        val ok = !testFailQueueWrite && writeQueue(filesDir, ix, q)
        if (so != null && sess != null) {
            // The session file is written BEFORE the queue, so only a FAILURE is re-saved into it
            // (the in-memory outcome and the returned status carry WRITTEN as well).
            so.put("queue_write", if (ok) "WRITTEN" else "FAILED")
            if (!ok) {
                bump(sess, "queue_write_failed")
                // best effort: the session file must carry the failure even though the queue does not
                atomicWrite(sessionFile(filesDir, d, ix), sess.toString())
            }
        }
        return ok
    }

    // ---------------------------------------------------------------- restart recovery
    /** How far back [reconcile] looks for close sessions whose carry may need rebuilding. */
    const val RECONCILE_LOOKBACK_DAYS = 14L

    /**
     * Rebuild the carry queue from DURABLE session evidence, before any window logic runs.
     *
     * The session file and the queue are separate writes, and the phone never replays a slot
     * (dispatch dedup is persisted), so a crash between the two must be repaired from what is
     * on disk, not from a retry:
     *  - a close cohort contract with NO queue entry gets one, through the same enqueue rule
     *    (calendar and expiry refusals preserved) -> recovered MISSING_ENTRY;
     *  - a QUEUED entry whose ready marker is missing or older than the latest durable QUALIFYING
     *    close record is brought up to it -> READY_MARKER;
     *  - an entry refused `no_qualifying_close_quote` while a durable qualifying close exists is
     *    restored to QUEUED -> REFUSED_WITHOUT_DURABLE_VIEW;
     *  - a carried entry whose qualifying opening record is already durable becomes OPEN_CAPTURED
     *    with the EARLIEST such slot, writing no observation -> OPEN_SAVED.
     * Only a qualifying record counts; a cap-refused record is not on disk and so cannot. Running
     * it twice changes nothing the second time.
     */
    fun reconcile(filesDir: File, ix: String, today: LocalDate, queue: JSONObject): JSONObject {
        val out = JSONObject()
        fun note(k: String) { out.put(k, out.optInt(k, 0) + 1) }
        val root = root(filesDir)
        val dates = (root.listFiles() ?: emptyArray())
            .filter { it.isDirectory }.mapNotNull { parseDate(it.name) }
            .filter { !it.isAfter(today) && !it.isBefore(today.minusDays(RECONCILE_LOOKBACK_DAYS)) }
            .sorted()
        val sessions = HashMap<LocalDate, JSONObject>()
        for (d in dates) {
            val f = sessionFile(filesDir, d, ix)
            val s = try { if (f.isFile) JSONObject(f.readText()) else null } catch (t: Throwable) { note("session_unreadable"); null }
            if (s != null) sessions[d] = s
        }
        // close side
        for ((d, s) in sessions.toSortedMap()) {
            val pins = s.optJSONObject("close_cohort")?.optJSONArray("pinned_contracts") ?: continue
            val obs = s.optJSONObject("observations") ?: JSONObject()
            val cal by lazy { calendarFor(d) }
            for (i in 0 until pins.length()) {
                val pin = pins.getJSONObject(i)
                val key = pin.getString("instrument_key")
                var lastQ: String? = null
                for (k in obs.keys()) obs.optJSONObject(k)?.let { r ->
                    if (r.optString("role") == "CLOSE" && r.optString("instrument_key") == key && qualifies(r)) {
                        val sl = r.optString("slot"); if (lastQ == null || sl > lastQ!!) lastQ = sl } }
                val ekey = d.toString() + "|" + key
                var e = findEntry(queue, ekey)
                if (e == null) {
                    e = newEntry(ix, d, pin, s.optJSONObject("close_cohort")?.optString("selected_at_slot") ?: "?")
                    applyEnqueueDecision(e, d, pin, cal)
                    e.put("recovered", "MISSING_ENTRY")
                    queue.getJSONArray("entries").put(e)
                    note("recovered_missing_entry")
                }
                if (lastQ == null) continue
                val st = e.optString("state")
                if (st == Q_REFUSED && e.optString("reason") == R_NO_QUALIFYING_CLOSE) {
                    e.put("state", Q_QUEUED).remove("reason")
                    e.put("recovered", "REFUSED_WITHOUT_DURABLE_VIEW"); note("recovered_refused_without_durable_view")
                }
                if (e.optString("state") == Q_QUEUED || e.optString("state") == Q_OPEN_CAPTURED) {
                    val cur = e.optString("close_last_qualifying_slot", "")
                    if (cur.isEmpty() || cur < lastQ!!) {
                        e.put("close_last_qualifying_slot", lastQ)
                        if (!e.has("recovered")) e.put("recovered", "READY_MARKER")
                        note("recovered_ready_marker")
                    }
                }
            }
        }
        // open side: an opening record already durable for a carried entry
        val ents = queue.getJSONArray("entries")
        for (i in 0 until ents.length()) {
            val e = ents.getJSONObject(i)
            val st = e.optString("state")
            if (st != Q_QUEUED && st != Q_OPEN_NOT_CAPTURED && st != Q_TARGET_PASSED) continue
            if (!e.has("close_last_qualifying_slot")) continue
            val t = parseDate(e.optString("target_session")) ?: continue
            val s = sessions[t] ?: continue
            val key = e.getJSONObject("contract").getString("instrument_key")
            val obs = s.optJSONObject("observations") ?: continue
            var firstQ: String? = null
            for (k in obs.keys()) obs.optJSONObject(k)?.let { r ->
                if (r.optString("role") == "REOPEN" && r.optString("instrument_key") == key &&
                    r.optString("carried_from_session") == e.optString("close_session") && qualifies(r)) {
                    val sl = r.optString("slot"); if (firstQ == null || sl < firstQ!!) firstQ = sl } }
            if (firstQ != null) {
                e.put("state", Q_OPEN_CAPTURED).put("open_first_qualifying_slot", firstQ).remove("reason")
                e.put("recovered_open", "OPEN_SAVED"); note("recovered_open_saved")
            }
        }
        return out
    }

    // ---------------------------------------------------------------- close slot
    private fun closeSlot(filesDir: File, d: LocalDate, slot: String, ix: String,
                          acq: Acquisition?, overlapSince: Long): JSONObject {
        val sess = loadSession(filesDir, d, ix)
        val so = slotOutcome(sess, slot, "CLOSE", overlapSince)
        bump(sess, "scheduled")
        val cal = calendarFor(d)
        val queue = loadQueue(filesDir, ix)
        val rec = reconcile(filesDir, ix, d, queue)
        if (rec.length() > 0) so.put("reconcile", rec)
        finalize(queue, d, minuteOf(slot)!!)
        val out = JSONObject().put("kind", "CLOSE")

        fun done(status: String): JSONObject {
            so.put("result", status)
            writeSession(filesDir, d, ix, sess, so)
            testHookAfterSessionWrite?.invoke("CLOSE", slot, ix)
            // The ready marker moves only for a QUALIFYING observation (receipt inside the close
            // window, exact verified identity, two-sided book) that is durably on disk: newly
            // WRITTEN, or a DUPLICATE_NO_OP of a qualifying one written earlier.
            so.optJSONArray("observations")?.let { a ->
                for (i in 0 until a.length()) a.optJSONObject(i)?.let { o ->
                    val durable = o.optString("durable")
                    if (o.optBoolean("quote_ready") && (durable == "WRITTEN" || durable == "DUPLICATE_NO_OP"))
                        findEntry(queue, d.toString() + "|" + o.optString("instrument_key"))?.let { e ->
                            if (e.optString("state") == Q_QUEUED &&
                                (!e.has("close_last_qualifying_slot") || e.optString("close_last_qualifying_slot") < slot))
                                e.put("close_last_qualifying_slot", slot) } } }
            writeQueueReported(filesDir, d, ix, queue, sess, so)
            return out.put("status", status).put("durable_write", so.optString("durable_write"))
                .put("queue_write", so.optString("queue_write"))
        }
        if (cal.isSession == null) { refuse(sess, R_CAL_NOT_COVERED); return done(R_CAL_NOT_COVERED) }
        if (!cal.regular) { refuse(sess, R_NOT_REGULAR); return done(R_NOT_REGULAR) }
        if (!closeWindowFits(d)) { refuse(sess, R_CLOSE_END_NOT_STATED); return done(R_CLOSE_END_NOT_STATED) }

        val chain = acq?.chain
        if (chain == null) {
            bump(sess, "quote_chain_missing")
            so.put("quote_receipt", R_CHAIN_MISSING)
            return done(R_CHAIN_MISSING)
        }
        val parsed = parseChain(chain, ix)
        if (parsed.refusal != null) {
            bump(sess, "quote_chain_refused")
            so.put("quote_receipt", "CHAIN_REFUSED:${parsed.refusal}")
            return done("CHAIN_REFUSED")
        }
        var cohort = sess.optJSONObject("close_cohort")
        if (cohort == null) {
            val sel = selectCohort(parsed, ix, d, slot)
            if (sel == null) {
                bump(sess, "quote_selection_refused")
                so.put("quote_receipt", "SELECTION_REFUSED:incomplete_paired_strikes")
                return done("SELECTION_REFUSED")
            }
            cohort = sel
            sess.put("close_cohort", cohort)
        }
        val env = envelope(d, ix, slot, acq, parsed)
        val pins = cohort.getJSONArray("pinned_contracts")
        val obsResults = JSONArray()
        var ready = 0
        for (i in 0 until pins.length()) {
            val pin = pins.getJSONObject(i)
            val r = observe(sess, env, pin, parsed, "CLOSE", null, d)
            obsResults.put(r)
            if (r.optBoolean("quote_ready")) ready++
            val key = d.toString() + "|" + pin.getString("instrument_key")
            val e = findEntry(queue, key) ?: newEntry(ix, d, pin, slot).also { queue.getJSONArray("entries").put(it) }
            if (!e.has("state")) {
                applyEnqueueDecision(e, d, pin, cal)
                if (e.optString("state") == Q_REFUSED) bump(sess, "queue_refused_" + e.optString("reason"))
                else bump(sess, "queue_enqueued")
            }
        }
        so.put("quote_receipt", if (ready == pins.length()) "ALL_QUALIFYING" else "QUALIFYING_$ready/${pins.length()}")
            .put("observations", obsResults)
        return done("OBSERVED")
    }

    // ---------------------------------------------------------------- reopen slot
    private fun reopenSlot(filesDir: File, d: LocalDate, slot: String, ix: String,
                           acq: Acquisition?, overlapSince: Long): JSONObject {
        val queue = loadQueue(filesDir, ix)
        val rec = reconcile(filesDir, ix, d, queue)
        val changedByFinalize = finalize(queue, d, minuteOf(slot)!!)
        // only an entry with a durable QUALIFYING close is ever looked up in the morning
        val due = (0 until queue.getJSONArray("entries").length()).map { queue.getJSONArray("entries").getJSONObject(it) }
            .filter { it.optString("target_session") == d.toString() && it.has("close_last_qualifying_slot") &&
                (it.optString("state") == Q_QUEUED || it.optString("state") == Q_OPEN_CAPTURED) }
        val out = JSONObject().put("kind", "REOPEN").put("due", due.size)
        if (due.isEmpty()) {
            if (changedByFinalize || rec.length() > 0) {
                val ok = !testFailQueueWrite && writeQueue(filesDir, ix, queue)
                out.put("queue_write", if (ok) "WRITTEN" else "FAILED")
            }
            if (rec.length() > 0) out.put("reconcile", rec)
            return out.put("status", "NOTHING_DUE")
        }
        val sess = loadSession(filesDir, d, ix)
        val so = slotOutcome(sess, slot, "REOPEN", overlapSince)
        if (rec.length() > 0) so.put("reconcile", rec)
        bump(sess, "scheduled")
        val chain = acq?.chain
        val parsed = chain?.let { parseChain(it, ix) }
        val env = if (parsed != null && parsed.refusal == null) envelope(d, ix, slot, acq, parsed) else null
        val results = JSONArray()
        for (e in due) {
            e.put("open_attempts", e.optInt("open_attempts", 0) + 1)
            val pin = e.getJSONObject("contract")
            val r: JSONObject = when {
                chain == null -> JSONObject().put("present", false).put("reason", R_CHAIN_MISSING)
                parsed!!.refusal != null -> JSONObject().put("present", false).put("reason", "chain_refused:${parsed.refusal}")
                else -> {
                    val carried = sess.optJSONObject("carried_pins") ?: JSONObject().also { sess.put("carried_pins", it) }
                    if (!carried.has(pin.getString("instrument_key")))
                        carried.put(pin.getString("instrument_key"), JSONObject(pin.toString()).put("carried_from_session", e.optString("close_session")))
                    observe(sess, env!!, pin, parsed, "REOPEN", e.optString("close_session"), d)
                }
            }
            if (!r.optBoolean("present")) bump(sess, "quote_contract_missing_" + r.optString("reason"))
            e.put("last_open_outcome", when {
                !r.optBoolean("present") -> r.optString("reason")
                r.optBoolean("quote_ready") -> "QUALIFYING"
                else -> (r.optJSONArray("reasons")?.let { a -> (0 until a.length()).joinToString("+") { a.getString(it) } } ?: "not_qualifying")
            })
            results.put(r.put("instrument_key", pin.getString("instrument_key")))
        }
        so.put("quote_receipt", if (chain == null) R_CHAIN_MISSING else "ATTEMPTED").put("observations", results)
        so.put("result", "OBSERVED")
        writeSession(filesDir, d, ix, sess, so)
        testHookAfterSessionWrite?.invoke("REOPEN", slot, ix)
        // OPEN_CAPTURED only for a QUALIFYING observation that is durably on disk (see closeSlot)
        for (i in 0 until results.length()) results.getJSONObject(i).let { o ->
            val durable = o.optString("durable")
            if (o.optBoolean("quote_ready") && (durable == "WRITTEN" || durable == "DUPLICATE_NO_OP"))
                due.firstOrNull { it.getJSONObject("contract").optString("instrument_key") == o.optString("instrument_key") }
                    ?.let { e -> if (e.optString("state") == Q_QUEUED)
                        e.put("state", Q_OPEN_CAPTURED).put("open_first_qualifying_slot", slot) } }
        writeQueueReported(filesDir, d, ix, queue, sess, so)
        return out.put("status", "OBSERVED").put("durable_write", so.optString("durable_write"))
            .put("queue_write", so.optString("queue_write"))
    }

    /**
     * Terminal states for entries whose window has gone: a later session -> TARGET_PASSED (or
     * OPEN_NOT_CAPTURED if it was attempted); the target session past 09:45 -> OPEN_NOT_CAPTURED.
     * Runs AFTER [reconcile], so it never judges a stale queue.
     */
    fun finalize(queue: JSONObject, today: LocalDate, minute: Int): Boolean {
        var changed = false
        val arr = queue.getJSONArray("entries")
        for (i in 0 until arr.length()) {
            val e = arr.getJSONObject(i)
            if (e.optString("state") != Q_QUEUED) continue
            val t = e.optString("target_session")
            if (t <= today.toString() && !e.has("close_last_qualifying_slot")) {
                // no durable qualifying close: an opening mark could never be paired, so no lookup
                e.put("state", Q_REFUSED).put("reason", R_NO_QUALIFYING_CLOSE); changed = true; continue
            }
            val passed = t < today.toString() || (t == today.toString() && minute > REOPEN_LAST_MIN)
            if (!passed) continue
            if (e.optInt("open_attempts", 0) == 0) e.put("state", Q_TARGET_PASSED).put("reason", R_NO_ATTEMPT)
            else e.put("state", Q_OPEN_NOT_CAPTURED).put("reason", e.optString("last_open_outcome").ifEmpty { "not_qualifying" })
            changed = true
        }
        return changed
    }
    // ---------------------------------------------------------------- observation
    /** One observation per (contract, slot). A retry is a DUPLICATE_NO_OP or a counted conflict. */
    private fun observe(sess: JSONObject, env: JSONObject, pin: JSONObject, parsed: Parsed,
                        role: String, carriedFrom: String?, session: LocalDate): JSONObject {
        val key = pin.getString("instrument_key")
        val r = JSONObject().put("instrument_key", key)
        val row = parsed.byKey[key]
        if (row == null) {
            val sameStrike = parsed.byStrikeSide[pin.optDouble("strike").toString() + "|" + pin.optString("option_type")]
            val reason = when {
                parsed.expiry != pin.optString("expiry") -> R_CHAIN_EXPIRY_DIFFERS
                sameStrike != null -> R_ROLL
                else -> R_CONTRACT_ABSENT
            }
            return r.put("present", false).put("reason", reason)
        }
        val disagreements = JSONArray()
        if (row.strike != pin.optDouble("strike")) disagreements.put("strike")
        if (row.side != pin.optString("option_type")) disagreements.put("option_type")
        if (parsed.expiry != pin.optString("expiry")) disagreements.put("expiry")
        if (parsed.underlying != pin.optString("underlying_key")) disagreements.put("underlying_key")
        val quality = bookQuality(row.bid, row.ask)
        bump(sess, "quote_received_" + quality.lowercase())
        val slot = env.getString("slot")
        val obsKey = "$slot|$key"
        val receiptMs = env.getJSONObject("clocks").optLong("acquisition_response_ms", 0L).takeIf { it > 0L }
        val elig = eligibility(role, session, slot, receiptMs, disagreements, quality)
        val rec = JSONObject()
            .put("instrument_key", key)
            .put("acquisition_id", env.getString("acquisition_id"))
            .put("slot", slot)
            .put("role", role)
            .put("bbo", JSONObject()
                .put("bid_raw", JSONObject().put("raw", row.bid ?: JSONObject.NULL))
                .put("ask_raw", JSONObject().put("raw", row.ask ?: JSONObject.NULL))
                .put("bid_qty", row.bidQty ?: JSONObject.NULL).put("ask_qty", row.askQty ?: JSONObject.NULL)
                .put("ltp", row.ltp ?: JSONObject.NULL)
                .put("book_quality", quality))
            .put("iv", JSONObject().put("iv_greeks_raw", JSONObject()
                .put("raw", row.iv ?: JSONObject.NULL).put("status", if (row.iv == null) "ABSENT" else "VENDOR_GREEK_AS_RECEIVED")))
            .put("identity", JSONObject().put("identity_verified", disagreements.length() == 0)
                .put("identity_disagreements", disagreements))
            .put("freshness", FRESHNESS_RECEIPT_ONLY)
            .put("eligibility", elig)
        if (carriedFrom != null) rec.put("carried_from_session", carriedFrom)
        val ready = elig.getBoolean("quote_ready")
        if (!ready) bump(sess, "quote_not_qualifying")
        val obs = sess.getJSONObject("observations")
        val prior = obs.optJSONObject(obsKey)
        val pending = sess.optJSONObject("_pending") ?: JSONObject().also { sess.put("_pending", it) }
        if (prior != null) {
            val same = prior.getJSONObject("bbo").toString() == rec.getJSONObject("bbo").toString() &&
                prior.optJSONObject("iv").toString() == rec.optJSONObject("iv").toString()
            bump(sess, if (same) "durable_duplicate_no_op" else "durable_duplicate_conflict_kept_first")
            // the KEPT (first) record decides readiness, never the retry
            val pe = prior.optJSONObject("eligibility")
            return r.put("present", true).put("book_quality", quality)
                .put("durable", if (same) "DUPLICATE_NO_OP" else "DUPLICATE_CONFLICT_KEPT_FIRST")
                .put("quote_ready", pe?.optBoolean("quote_ready", false) == true)
                .put("reasons", pe?.optJSONArray("reasons") ?: JSONArray().put("prior_record_has_no_eligibility"))
        }
        pending.put(obsKey, rec)
        val envs = sess.getJSONObject("envelopes")
        val aid = env.getString("acquisition_id")
        if (!envs.has(aid)) pending.put("env|$aid", env)
        return r.put("present", true).put("book_quality", quality).put("durable", "PENDING")
            .put("quote_ready", ready).put("reasons", elig.getJSONArray("reasons"))
    }

    /** The R2 eligibility verdict for one received quote; see [ELIGIBILITY_RULE]. */
    fun eligibility(role: String, session: LocalDate, slot: String, receiptMs: Long?,
                    disagreements: JSONArray, quality: String): JSONObject {
        val reasons = JSONArray()
        val first = if (role == "CLOSE") CLOSE_FIRST_MIN else REOPEN_FIRST_MIN
        val last = if (role == "CLOSE") CLOSE_LAST_MIN else REOPEN_LAST_MIN
        val w = if (role == "CLOSE") "close" else "open"
        var receiptMin: Int? = null
        if (receiptMs == null) reasons.put("receipt_clock_missing")
        else {
            val z = Instant.ofEpochMilli(receiptMs).atZone(IST)
            receiptMin = z.hour * 60 + z.minute
            if (z.toLocalDate() != session) reasons.put("receipt_date_not_session")
            else if (receiptMin < first) reasons.put("receipt_before_${w}_window")
            else if (receiptMin > last) reasons.put("receipt_after_${w}_window")
        }
        if (disagreements.length() > 0) reasons.put("identity_disagreement")
        if (quality != "TWO_SIDED") reasons.put("book_" + quality.lowercase())
        val slotMin = minuteOf(slot)
        return JSONObject().put("rule", ELIGIBILITY_RULE)
            .put("window", "%s %02d:%02d-%02d:%02d receipt minute inclusive".format(w, first / 60, first % 60, last / 60, last % 60))
            .put("receipt_minute", receiptMin?.let { "%02d:%02d".format(it / 60, it % 60) } ?: JSONObject.NULL)
            .put("slot_intent", slot)
            .put("slot_agrees_with_receipt", if (slotMin != null && receiptMin != null) Math.abs(slotMin - receiptMin) <= 5 else JSONObject.NULL)
            .put("freshness", FRESHNESS_RECEIPT_ONLY)
            .put("reasons", reasons).put("quote_ready", reasons.length() == 0)
    }

    fun bookQuality(bid: Double?, ask: Double?): String = when {
        bid == null && ask == null -> "NO_BOOK"
        bid == null || ask == null -> "ONE_SIDED"
        bid <= 0.0 || ask <= 0.0 -> "NON_POSITIVE"
        ask < bid -> "CROSSED"
        else -> "TWO_SIDED"
    }

    private fun envelope(d: LocalDate, ix: String, slot: String, acq: Acquisition?, parsed: Parsed): JSONObject {
        val ms = acq?.completedAtMs ?: 0L
        val receipt = if (ms > 0) isoIst(ms) else null
        val aid = "co1|$d|$ix|$slot|" + sha256(("$d|$ix|$slot|$ms").toByteArray()).take(12)
        return JSONObject()
            .put("acquisition_id", aid)
            .put("session_date", d.toString())
            .put("slot", slot)
            .put("slot_source", "SCHEDULER_SLOT")
            // the chain this capture reads is the live poll's own option-chain response
            // (MarketWatchService: "https://api.upstox.com/v2/option/chain?instrument_key=…")
            .put("quote_source", QUOTE_SOURCE)
            .put("clocks", JSONObject()
                .put("request_started_ms", acq?.requestStartedMs ?: JSONObject.NULL)
                .put("acquisition_response_ms", if (ms > 0) ms else JSONObject.NULL)
                .put("acquisition_response_ts", receipt ?: JSONObject.NULL)
                .put("vendor_source_ts", JSONObject.NULL)
                .put("quote_event_clock_verified", false)
                .put("freshness", FRESHNESS_RECEIPT_ONLY))
            .put("underlying", JSONObject()
                .put("spot_raw", JSONObject().put("raw", parsed.spot ?: JSONObject.NULL)
                    .put("status", if (parsed.spot == null) "ABSENT" else "CHAIN_ROW_UNDERLYING_SPOT_PRICE"))
                .put("basis", "SAME_RESPONSE_UNDERLYING_SPOT_PRICE")
                .put("synchronisation_with_quote", "SAME_RESPONSE")
                .put("asof", JSONObject.NULL)
                .put("receipt_ts", receipt ?: JSONObject.NULL))
            .put("expiry_in_response", parsed.expiry)
    }

    // ---------------------------------------------------------------- chain parsing
    data class Row(val key: String, val strike: Double, val side: String, val bid: Double?, val ask: Double?,
                   val bidQty: Double?, val askQty: Double?, val ltp: Double?, val iv: Double?)
    class Parsed(val refusal: String?, val expiry: String = "", val underlying: String = "", val spot: Double? = null,
                 val byKey: Map<String, Row> = emptyMap(), val byStrikeSide: Map<String, Row> = emptyMap(),
                 val strikes: List<Triple<Double, Row, Row>> = emptyList())

    private fun num(o: JSONObject?, k: String): Double? {
        if (o == null || !o.has(k) || o.isNull(k)) return null
        val v = o.optDouble(k, Double.NaN)
        return if (v.isFinite()) v else null
    }

    fun parseChain(chain: JSONObject, ix: String): Parsed {
        val expected = UNDERLYING[ix] ?: return Parsed("unknown_index")
        val data = chain.optJSONArray("data")
        if (data == null || data.length() == 0) return Parsed("no_data_rows")
        var expiry = ""; var spot: Double? = null
        val byKey = LinkedHashMap<String, Row>(); val bySS = LinkedHashMap<String, Row>()
        val pairs = ArrayList<Triple<Double, Row, Row>>()
        for (i in 0 until data.length()) {
            val row = data.optJSONObject(i) ?: continue
            val u = row.optString("underlying_key", "").trim()
            if (u.isNotEmpty() && u != expected) return Parsed("underlying_mismatch")
            val e = row.optString("expiry", "").trim()
            if (e.isNotEmpty()) { if (expiry.isEmpty()) expiry = e else if (e != expiry) return Parsed("mixed_expiry") }
            if (spot == null) spot = num(row, "underlying_spot_price")?.takeIf { it > 0 }
            val k = num(row, "strike_price")?.takeIf { it > 0 } ?: continue
            var ce: Row? = null; var pe: Row? = null
            for ((side, node) in listOf("CE" to row.optJSONObject("call_options"), "PE" to row.optJSONObject("put_options"))) {
                val key = node?.optString("instrument_key", "")?.trim().orEmpty()
                if (key.isEmpty()) continue
                val md = node!!.optJSONObject("market_data"); val g = node.optJSONObject("option_greeks")
                val r = Row(key, k, side, num(md, "bid_price"), num(md, "ask_price"), num(md, "bid_qty"),
                    num(md, "ask_qty"), num(md, "ltp"), num(g, "iv"))
                val prior = byKey[key]
                if (prior != null && (prior.strike != k || prior.side != side)) return Parsed("contradictory_rows")
                if (prior == null) { byKey[key] = r; bySS["$k|$side"] = r }
                if (side == "CE") ce = r else pe = r
            }
            if (ce != null && pe != null && ce.key != pe.key && pairs.none { it.first == k }) pairs.add(Triple(k, ce, pe))
        }
        if (expiry.isEmpty()) return Parsed("no_expiry")
        return Parsed(null, expiry, expected, spot, byKey, bySS, pairs)
    }

    /** Same rule as the pilot's selection: three fully paired strikes nearest this response's spot. */
    private fun selectCohort(p: Parsed, ix: String, d: LocalDate, slot: String): JSONObject? {
        val s = p.spot ?: return null
        if (p.strikes.size < PAIRED_STRIKES) return null
        val chosen = p.strikes.sortedWith(compareBy({ Math.abs(it.first - s) }, { it.first }))
            .take(PAIRED_STRIKES).sortedBy { it.first }
        val pins = JSONArray()
        for (c in chosen) for (r in listOf(c.second, c.third))
            pins.put(JSONObject().put("index", ix).put("instrument_key", r.key).put("underlying_key", p.underlying)
                .put("expiry", p.expiry).put("strike", r.strike).put("option_type", r.side))
        return JSONObject().put("rule", "THREE_STRIKES_NEAREST_THIS_RESPONSE_SPOT_WITH_BOTH_LEGS")
            .put("selected_at_slot", slot).put("selection_spot", s).put("session_date", d.toString())
            .put("expiry", p.expiry).put("pinned_contracts", pins)
    }

    // ---------------------------------------------------------------- session file
    private fun sessionFile(filesDir: File, d: LocalDate, ix: String) = File(File(root(filesDir), d.toString()), "$ix.json")

    private fun loadSession(filesDir: File, d: LocalDate, ix: String): JSONObject {
        val f = sessionFile(filesDir, d, ix)
        val j = try { if (f.isFile) JSONObject(f.readText()) else null } catch (t: Throwable) { null }
        val s = j ?: JSONObject().put("version", VERSION).put("session_date", d.toString()).put("index", ix)
            .also { if (f.isFile) it.put("prior_file_unreadable_replaced", true) }
        for (k in listOf("observations", "envelopes", "slots", "counters", "refusals"))
            if (s.optJSONObject(k) == null) s.put(k, JSONObject())
        return s
    }

    private fun slotOutcome(sess: JSONObject, slot: String, kind: String, overlapSince: Long): JSONObject {
        val slots = sess.getJSONObject("slots")
        val prior = slots.optJSONObject(slot)
        val so = JSONObject().put("slot", slot).put("kind", kind).put("scheduling", "SCHEDULER_SLOT_POLLED")
            .put("polls_for_slot", (prior?.optInt("polls_for_slot", 0) ?: 0) + 1)
        if (overlapSince > 0) so.put("overlap_refusals_in_process_before_this_job", overlapSince)
        slots.put(slot, so)
        return so
    }

    private fun bump(sess: JSONObject, k: String) {
        val c = sess.getJSONObject("counters"); c.put(k, c.optInt(k, 0) + 1)
    }
    private fun refuse(sess: JSONObject, k: String) {
        val c = sess.getJSONObject("refusals"); c.put(k, c.optInt(k, 0) + 1)
    }

    /**
     * Write the session file. Pending observations are committed only if both bounds allow; the
     * counters and slot outcome are ALWAYS written (they live under the control reserve), so a
     * refused observation still leaves a durable reason — the 13 Nov lesson.
     */
    private fun writeSession(filesDir: File, d: LocalDate, ix: String, sess: JSONObject, so: JSONObject) {
        val pending = sess.optJSONObject("_pending")
        sess.remove("_pending")
        val f = sessionFile(filesDir, d, ix)
        f.parentFile?.mkdirs()
        var committed = 0; var refused = 0
        if (pending != null && pending.length() > 0) {
            val trial = JSONObject(sess.toString())
            for (k in pending.keys()) {
                if (k.startsWith("env|")) trial.getJSONObject("envelopes").put(k.removePrefix("env|"), pending.getJSONObject(k))
                else trial.getJSONObject("observations").put(k, pending.getJSONObject(k))
            }
            val trialBytes = trial.toString().toByteArray().size.toLong()
            val storeNow = dirBytes(root(filesDir)) - (if (f.isFile) f.length() else 0L)
            val obsCount = pending.keys().asSequence().count { !it.startsWith("env|") }
            if (trialBytes <= MAX_SESSION_FILE_BYTES && storeNow + trialBytes <= MAX_STORE_BYTES) {
                for (k in pending.keys()) {
                    if (k.startsWith("env|")) sess.getJSONObject("envelopes").put(k.removePrefix("env|"), pending.getJSONObject(k))
                    else sess.getJSONObject("observations").put(k, pending.getJSONObject(k))
                }
                committed = obsCount
            } else refused = obsCount
        }
        if (committed > 0) sess.getJSONObject("counters").put("durable_written", sess.getJSONObject("counters").optInt("durable_written", 0) + committed)
        if (refused > 0) sess.getJSONObject("counters").put("durable_refused_cap", sess.getJSONObject("counters").optInt("durable_refused_cap", 0) + refused)
        // resolve each per-contract PENDING to what actually happened to it
        so.optJSONArray("observations")?.let { a ->
            for (i in 0 until a.length()) a.optJSONObject(i)?.let { o ->
                if (o.optString("durable") == "PENDING")
                    o.put("durable", if (refused > 0) "REFUSED_CAP" else "WRITTEN") } }
        so.put("durable_write", when {
            refused > 0 -> "OBSERVATIONS_REFUSED_CAP:$refused"
            committed > 0 -> "WRITTEN:$committed"
            else -> "NO_NEW_OBSERVATION"
        })
        // counters + slot outcomes: allowed into the reserve above the observation bound
        val bytes = sess.toString()
        val storeOther = dirBytes(root(filesDir)) - (if (f.isFile) f.length() else 0L)
        val failure = if (storeOther + bytes.length <= MAX_STORE_BYTES + CONTROL_RESERVE_BYTES) {
            if (atomicWrite(f, bytes)) null else "WRITE_FAILED"
        } else "REFUSED_RESERVE_EXHAUSTED"
        if (failure != null) {
            so.put("durable_write", failure)
            so.optJSONArray("observations")?.let { a ->
                for (i in 0 until a.length()) a.optJSONObject(i)?.let { o ->
                    if (o.optString("durable") == "WRITTEN") o.put("durable", failure) } }
        }
    }

    // ---------------------------------------------------------------- queue file
    private fun queueFile(filesDir: File, ix: String) = File(root(filesDir), "queue_$ix.json")

    fun loadQueue(filesDir: File, ix: String): JSONObject {
        val f = queueFile(filesDir, ix)
        val j = try { if (f.isFile) JSONObject(f.readText()) else null } catch (t: Throwable) { null }
        val q = j ?: JSONObject().put("version", VERSION).put("index", ix)
        if (q.optJSONArray("entries") == null) q.put("entries", JSONArray())
        return q
    }

    private fun findEntry(q: JSONObject, key: String): JSONObject? {
        val a = q.getJSONArray("entries")
        for (i in 0 until a.length()) if (a.getJSONObject(i).optString("key") == key) return a.getJSONObject(i)
        return null
    }

    /** @return whether the queue file reached disk (the caller must surface a false). */
    private fun writeQueue(filesDir: File, ix: String, q: JSONObject): Boolean {
        val a = q.getJSONArray("entries")
        if (a.length() > MAX_QUEUE_ENTRIES) {
            val live = ArrayList<JSONObject>(); val term = ArrayList<JSONObject>()
            for (i in 0 until a.length()) a.getJSONObject(i).let { e ->
                if (e.optString("state") == Q_QUEUED) live.add(e) else term.add(e) }
            term.sortBy { it.optString("close_session") }
            val keepTerm = term.drop(maxOf(0, term.size - (MAX_QUEUE_ENTRIES - live.size)))
            val na = JSONArray(); (keepTerm + live).sortedBy { it.optString("key") }.forEach { na.put(it) }
            q.put("entries", na).put("pruned_terminal_entries", q.optInt("pruned_terminal_entries", 0) + (term.size - keepTerm.size))
        }
        return atomicWrite(queueFile(filesDir, ix), q.toString())
    }

    // ---------------------------------------------------------------- export
    /**
     * The `premium_followon` value for [date]: the matcher's reader shape under `section`, plus
     * the additive `close_open` block. Reads only this capture's own files.
     */
    fun exportSection(filesDir: File, date: String): JSONObject {
        val out = JSONObject().put("purpose", VERSION)
            .put("kind", "CLOSE_TO_NEXT_SESSION_RESEARCH_CAPTURE")
            .put("independent_of_intraday_pilot", true)
            .put("capture_windows", JSONObject()
                .put("close", "15:10-15:30 receipt minute inclusive (IST); scheduler slots 15:10-15:30 are when it looks")
                .put("reopen", "09:15-09:45 receipt minute inclusive (IST); scheduler slots 09:15-09:45 are when it looks")
                .put("convention", "receipt instant truncated to the minute, both ends inclusive; the slot is intent only"))
            .put("control_state", controlState(filesDir))
        val d = parseDate(date) ?: return out.put("state", "SESSION_DATE_INVALID").put("has_data", false)
        val indices = JSONObject(); val co = JSONObject().put("version", VERSION).put("session_date", date)
        val cal = calendarFor(d)
        co.put("calendar", JSONObject().put("calendar_version", ResearchCalendar.VERSION)
            .put("is_session", cal.isSession ?: JSONObject.NULL).put("regular", cal.regular)
            .put("regime", cal.regime ?: JSONObject.NULL)
            .put("next_regular_session", cal.next?.toString() ?: JSONObject.NULL)
            .put("next_reason", cal.reason ?: JSONObject.NULL)
            .put("calendar_gap_days", cal.nextGap ?: JSONObject.NULL)
            .put("close_window_fits_declared_session_end", closeWindowFits(d)))
        val stages = JSONObject(); val queues = JSONObject()
        var any = false
        for (ix in UNDERLYING.keys) {
            val f = sessionFile(filesDir, d, ix)
            val sess = try { if (f.isFile) JSONObject(f.readText()) else null } catch (t: Throwable) { null }
            val q = loadQueue(filesDir, ix)
            val qa = JSONArray()
            val ents = q.getJSONArray("entries")
            for (i in 0 until ents.length()) ents.getJSONObject(i).let {
                if (it.optString("close_session") == date || it.optString("target_session") == date) qa.put(it) }
            queues.put(ix, qa)
            if (sess == null) {
                stages.put(ix, JSONObject().put("session_file", if (f.isFile) "UNREADABLE" else "NOT_WRITTEN"))
                continue
            }
            val pins = JSONArray()
            sess.optJSONObject("close_cohort")?.optJSONArray("pinned_contracts")?.let { a ->
                for (i in 0 until a.length()) pins.put(JSONObject(a.getJSONObject(i).toString()).put("role", "CLOSE")) }
            sess.optJSONObject("carried_pins")?.let { c ->
                for (k in c.keys()) if ((0 until pins.length()).none { pins.getJSONObject(it).optString("instrument_key") == k })
                    pins.put(JSONObject(c.getJSONObject(k).toString()).put("role", "REOPEN")) }
            val envs = JSONArray(); sess.getJSONObject("envelopes").let { e -> e.keys().asSequence().sorted().forEach { envs.put(e.get(it)) } }
            val recs = JSONArray(); sess.getJSONObject("observations").let { o -> o.keys().asSequence().sorted().forEach { recs.put(o.get(it)) } }
            if (recs.length() > 0) any = true
            indices.put(ix, JSONObject().put("cohort", JSONObject().put("pinned_contracts", pins)
                .put("close_cohort_rule", sess.optJSONObject("close_cohort")?.optString("rule") ?: JSONObject.NULL))
                .put("slot_envelopes", envs).put("records", recs)
                .put("lot_authority", lotAuthorityBlock()))
            stages.put(ix, JSONObject().put("session_file", "PRESENT")
                .put("counters", sess.getJSONObject("counters"))
                .put("refusals", sess.getJSONObject("refusals"))
                .put("slots", sess.getJSONObject("slots"))
                .put("export_included_records", recs.length())
                .put("export_qualifying_records", (0 until recs.length()).count { qualifies(recs.optJSONObject(it)) }))
        }
        val section = JSONObject().put("section_version", SECTION_VERSION).put("requested_session_date", date)
            .put("state", if (any) "PRESENT" else "NO_CLOSE_OPEN_DATA").put("has_data", any).put("indices", indices)
        co.put("stages", stages).put("queue", queues)
            .put("eligibility_rule", ELIGIBILITY_RULE)
            .put("lot_authority", lotAuthorityBlock())
            .put("labels", JSONObject().put("freshness", FRESHNESS_RECEIPT_ONLY)
                .put("book_quality", JSONArray(listOf("TWO_SIDED", "ONE_SIDED", "NO_BOOK", "CROSSED", "NON_POSITIVE"))))
            .put("bounds", JSONObject().put("max_store_bytes", MAX_STORE_BYTES).put("control_reserve_bytes", CONTROL_RESERVE_BYTES)
                .put("max_session_file_bytes", MAX_SESSION_FILE_BYTES).put("max_export_section_bytes", MAX_EXPORT_SECTION_BYTES)
                .put("store_bytes", dirBytes(root(filesDir))))
        out.put("section", section).put("close_open", co).put("has_data", any)
        val size = out.toString().toByteArray().size
        if (size > MAX_EXPORT_SECTION_BYTES) {
            for (ix in indices.keys()) indices.getJSONObject(ix).let { b ->
                val n = b.getJSONArray("records").length()
                b.put("records", JSONArray()).put("slot_envelopes", JSONArray())
                stages.optJSONObject(ix)?.put("export_included_records", 0)?.put("export_excluded_records_over_cap", n)
            }
            section.put("state", "RECORDS_EXCLUDED_OVER_EXPORT_CAP").put("has_data", false)
            out.put("has_data", false)
        }
        return out
    }

    /** Quantity evidence is NOT captured here, so no pair may become an after-cost result from this section alone. */
    fun lotAuthorityBlock(): JSONObject = JSONObject().put("state", LOT_AUTHORITY_STATE)
        .put("after_cost_use", "REFUSED_UNTIL_QUANTITY_EVIDENCE")
        .put("why", "this path records quotes only; it writes no production lot authority")

    /**
     * Owner-visible status for the opt-in card: control, rule, calendar coverage, today's per-index
     * stage outcomes and the queue by state. Reads only this capture's own files.
     */
    fun statusSummary(filesDir: File, today: String): JSONObject {
        val out = JSONObject().put("version", VERSION).put("control_state", controlState(filesDir))
            .put("enabled", isEnabled(filesDir)).put("default", "OFF")
            .put("producer_key", SECTION_KEY)
            .put("windows", JSONObject().put("close", "15:10-15:30").put("reopen", "09:15-09:45")
                .put("convention", "receipt minute inclusive (IST); slot is scheduling intent only"))
            .put("eligibility_rule", ELIGIBILITY_RULE)
            .put("expiry_scope", "NEAREST_EXPIRY_ONLY_AS_POLLED; refused when the next session is on or after expiry")
            .put("lot_authority", lotAuthorityBlock())
            .put("independent_of", JSONArray(listOf("intraday premium pilot window and denominator", "Paper controls", "Real (disabled)")))
            .put("makes_requests", false)
        val d = parseDate(today)
        out.put("calendar", JSONObject().put("calendar_version", ResearchCalendar.VERSION)
            .put("covered_years", JSONArray(ResearchCalendar.COVERED_YEARS.sorted()))
            .put("today_covered", d?.let { ResearchCalendar.isSession(it) != null } ?: false)
            .put("next_regular_session", d?.let { nextRegularSession(it).first?.toString() } ?: JSONObject.NULL)
            .put("next_reason", d?.let { nextRegularSession(it).second } ?: "SESSION_DATE_INVALID")
            .put("uncovered_dates", "REFUSED calendar_not_covered"))
        val idx = JSONObject()
        for (ix in UNDERLYING.keys) {
            val b = JSONObject()
            val f = d?.let { sessionFile(filesDir, it, ix) }
            val sess = try { if (f != null && f.isFile) JSONObject(f.readText()) else null } catch (t: Throwable) { null }
            b.put("today_session_file", if (sess != null) "PRESENT" else if (f?.isFile == true) "UNREADABLE" else "NOT_WRITTEN")
            if (sess != null) {
                b.put("counters", sess.optJSONObject("counters") ?: JSONObject())
                b.put("refusals", sess.optJSONObject("refusals") ?: JSONObject())
                val slots = sess.optJSONObject("slots")
                val lastSlot = slots?.keys()?.asSequence()?.sorted()?.lastOrNull()
                if (lastSlot != null) slots.optJSONObject(lastSlot)?.let { so ->
                    b.put("last_slot", JSONObject().put("slot", lastSlot).put("result", so.optString("result"))
                        .put("quote_receipt", so.optString("quote_receipt")).put("durable_write", so.optString("durable_write"))
                        .put("queue_write", so.optString("queue_write").ifEmpty { "NO_FAILURE_RECORDED" })) }
            }
            val q = loadQueue(filesDir, ix).getJSONArray("entries")
            val byState = JSONObject(); val reasons = JSONObject()
            for (i in 0 until q.length()) q.getJSONObject(i).let { e ->
                byState.put(e.optString("state"), byState.optInt(e.optString("state"), 0) + 1)
                if (e.has("reason")) reasons.put(e.optString("reason"), reasons.optInt(e.optString("reason"), 0) + 1) }
            b.put("queue", JSONObject().put("entries", q.length()).put("by_state", byState).put("by_reason", reasons))
            idx.put(ix, b)
        }
        return out.put("indices", idx).put("store_bytes", dirBytes(root(filesDir)))
            .put("bounds", JSONObject().put("max_store_bytes", MAX_STORE_BYTES).put("max_session_file_bytes", MAX_SESSION_FILE_BYTES))
    }

    /** Prepend `"premium_followon": …` to a bundle without reparsing it; other keys keep their bytes. */
    fun splice(bundle: String, section: JSONObject): String? {
        val t = bundle.trimStart()
        if (!t.startsWith("{")) return null
        val rest = t.substring(1).trimStart()
        val head = "{\"$SECTION_KEY\":" + section.toString()
        return if (rest.startsWith("}")) "$head}" else "$head,$rest"
    }

    data class SpliceResult(val bytes: ByteArray, val included: Boolean, val reason: String)

    /** Additive and bounded: the final payload must stay under [capBytes] or the section is left out, SAID so. */
    fun spliceWithinCap(bytes: ByteArray, filesDir: File, date: String, capBytes: Int): SpliceResult = try {
        if (!isEnabled(filesDir) && !root(filesDir).isDirectory) SpliceResult(bytes, false, "CAPTURE_NEVER_ENABLED")
        else {
            val sec = exportSection(filesDir, date)
            val merged = splice(String(bytes, Charsets.UTF_8), sec)?.toByteArray(Charsets.UTF_8)
            when {
                merged == null -> SpliceResult(bytes, false, "BUNDLE_NOT_AN_OBJECT")
                merged.size > capBytes -> SpliceResult(bytes, false, "OVER_COMBINED_CAP")
                else -> SpliceResult(merged, true, if (sec.optBoolean("has_data")) "INCLUDED" else "INCLUDED_NO_DATA")
            }
        }
    } catch (t: Throwable) { SpliceResult(bytes, false, "ERROR_" + t.javaClass.simpleName) }

    // ---------------------------------------------------------------- utils
    fun minuteOf(hhmm: String?): Int? {
        val p = hhmm?.trim()?.split(":") ?: return null
        if (p.size < 2) return null
        val h = p[0].toIntOrNull() ?: return null; val m = p[1].toIntOrNull() ?: return null
        if (h !in 0..23 || m !in 0..59) return null
        return h * 60 + m
    }
    private fun parseDate(s: String) = try { LocalDate.parse(s.take(10)) } catch (t: Throwable) { null }
    fun isoIst(ms: Long): String = DateTimeFormatter.ISO_OFFSET_DATE_TIME.format(Instant.ofEpochMilli(ms).atZone(IST))
    private fun sha256(b: ByteArray) = MessageDigest.getInstance("SHA-256").digest(b).joinToString("") { "%02x".format(it) }
    fun dirBytes(f: File): Long = if (!f.exists()) 0L else f.walkTopDown().filter { it.isFile }.sumOf { it.length() }
    private fun atomicWrite(f: File, text: String): Boolean = try {
        f.parentFile?.mkdirs()
        val tmp = File(f.parentFile, f.name + ".tmp")
        tmp.writeText(text)
        if (!tmp.renameTo(f)) { f.delete(); tmp.renameTo(f) } else true
    } catch (t: Throwable) { false }
}
