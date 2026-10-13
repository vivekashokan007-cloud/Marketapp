package com.marketradar.app

import org.json.JSONObject
import java.security.MessageDigest
import java.util.UUID

/**
 * b503 capture reliability: immutable identity and inputs of ONE snapshot capture.
 *
 * Built on the poll thread, before any worker submission and before the caller
 * mutates its result/context objects again. Only immutable Strings cross threads.
 *
 * Identity comes from the recorded poll, never from the finisher's wall clock:
 *  - captureKey == poll.slot_key ("<IST date>|<numeric ordinal>"), the exact shape the
 *    poll path already writes (see MarketWatchService savePoll). A poll without a
 *    slot (outside the scheduled grid) keeps the historical "<date>|closed" key,
 *    with the date taken from ctx.today_ist frozen at poll time.
 *  - originPollTs is the poll's own recorded "t" (HH:mm IST at chain fetch) on the
 *    origin date. It is reconciliation metadata; the Python output's poll_ts is
 *    preserved byte-for-byte and never rewritten.
 */
enum class CaptureDelivery { ON_TIME, LATE }

class CaptureIdentityException(val reason: String) : IllegalArgumentException(reason)

class FrozenCaptureRequest internal constructor(
    val captureKey: String,
    val sessionDate: String,
    /** null for an unscheduled ("closed") poll. */
    val slotOrdinal: Int?,
    val originPollTs: String?,
    val slotScheduledIst: String?,
    val attemptId: String,
    val runId: String,
    val appVersion: String,
    val versionCode: Int,
    val brainVersion: String,
    val requestedMonoMs: Long,
    val requestedWallMs: Long,
    val resultJson: String,
    val ctxJson: String,
    val pollsJson: String,
    val factPackJson: String?,
    val persistenceMode: String,
    val freezeMs: Long
) {
    /** Small, payload-free metadata (journal intent, events, poll_summary provenance). */
    fun identityJson(): JSONObject = JSONObject()
        .put("capture_key", captureKey)
        .put("session_date", sessionDate)
        .put("slot_ordinal", slotOrdinal ?: JSONObject.NULL)
        .put("origin_poll_ts", originPollTs ?: JSONObject.NULL)
        .put("slot_scheduled_ist", slotScheduledIst ?: JSONObject.NULL)
        .put("attempt_id", attemptId)
        .put("run_id", runId)
        .put("app_version", appVersion)
        .put("version_code", versionCode)
        .put("brain_version", brainVersion)
        .put("requested_wall_ms", requestedWallMs)

    companion object {
        const val MARKET_OPEN_MINUTE = 9 * 60 + 15
        const val POLL_SLOT_MINUTES = 5
        const val MARKET_CLOSE_MINUTE = 15 * 60 + 40
        const val FULL_DAY_SLOTS = ((MARKET_CLOSE_MINUTE - MARKET_OPEN_MINUTE) / POLL_SLOT_MINUTES) + 1
        private val DATE = Regex("^20\\d{2}-\\d{2}-\\d{2}$")
        private val SLOT_KEY = Regex("^(20\\d{2}-\\d{2}-\\d{2})\\|(\\d{1,3})$")
        private val POLL_TIME = Regex("^([01]\\d|2[0-3]):([0-5]\\d)$")

        /** "HH:mm" scheduled start of a 1-based slot ordinal. */
        fun slotStartHm(ordinal: Int): String {
            val m = MARKET_OPEN_MINUTE + (ordinal - 1) * POLL_SLOT_MINUTES
            return "%02d:%02d".format(m / 60, m % 60)
        }

        /** Normalizes legacy reservation keys ("d|d|n" from the b502 wall-clock key) to slot keys. */
        fun normalizeLegacyKey(key: String): String {
            val parts = key.split('|')
            return if (parts.size == 3 && parts[0] == parts[1]) "${parts[1]}|${parts[2]}" else key
        }

        /**
         * Validates identity and freezes inputs. [result]/[ctx] are serialized HERE, on the
         * calling thread; nothing mutable is retained. Throws [CaptureIdentityException]
         * on inconsistent date/slot identity instead of guessing.
         */
        fun freeze(
            pollSlotKey: String?,
            ctxTodayIst: String?,
            pollTime: String?,
            result: JSONObject,
            ctx: JSONObject,
            pollsJson: String,
            appVersion: String,
            versionCode: Int,
            brainVersion: String,
            runId: String,
            persistenceMode: String = "android_compact_v1",
            monoClock: () -> Long = { System.nanoTime() / 1_000_000L },
            wallClock: () -> Long = { System.currentTimeMillis() },
            attemptId: String = UUID.randomUUID().toString()
        ): FrozenCaptureRequest {
            val started = monoClock()
            val date = ctxTodayIst?.trim().orEmpty()
            if (!DATE.matches(date)) throw CaptureIdentityException("origin_date_invalid")
            val slot = pollSlotKey?.trim().orEmpty()
            val ordinal: Int?
            val key: String
            if (slot.isEmpty()) {
                ordinal = null
                key = "$date|closed"
            } else {
                val m = SLOT_KEY.matchEntire(slot) ?: throw CaptureIdentityException("slot_key_malformed")
                if (m.groupValues[1] != date) throw CaptureIdentityException("slot_date_mismatch")
                ordinal = m.groupValues[2].toInt()
                if (ordinal !in 1..FULL_DAY_SLOTS) throw CaptureIdentityException("slot_ordinal_out_of_range")
                key = slot
            }
            val t = pollTime?.trim().orEmpty()
            val originPollTs = if (POLL_TIME.matches(t)) "${date}T$t:00+0530" else null
            val scheduled = ordinal?.let { "${date}T${slotStartHm(it)}:00+0530" }
            // Freeze: immutable Strings, produced before submission.
            val resultJson = result.toString()
            val factPack = result.optJSONObject("elephant_fact_pack")?.toString()
            val ctxJson = ctx.toString()
            val done = monoClock()
            return FrozenCaptureRequest(
                captureKey = key, sessionDate = date, slotOrdinal = ordinal,
                originPollTs = originPollTs, slotScheduledIst = scheduled,
                attemptId = attemptId, runId = runId,
                appVersion = appVersion, versionCode = versionCode, brainVersion = brainVersion,
                requestedMonoMs = done, requestedWallMs = wallClock(),
                resultJson = resultJson, ctxJson = ctxJson, pollsJson = pollsJson,
                factPackJson = factPack, persistenceMode = persistenceMode,
                freezeMs = (done - started).coerceAtLeast(0)
            )
        }

        private val CLOSED_KEY = Regex("^(20\\d{2}-\\d{2}-\\d{2})\\|closed$")

        /**
         * b503 R1/R6: the validated origin session of a capture key ("date|ordinal" with ordinal 1..78, or
         * "date|closed"), or null. ISO poll timestamps, summaries ("pending=N") and b502 "d|d|n" keys are NOT
         * capture keys and return null (callers must treat them as unattributed, never as a session change).
         */
        fun sessionOfKey(key: String?): String? {
            if (key == null) return null
            val slot = SLOT_KEY.matchEntire(key)
            val date = when {
                slot != null -> slot.groupValues[1].takeIf { slot.groupValues[2].toInt() in 1..FULL_DAY_SLOTS }
                else -> CLOSED_KEY.matchEntire(key)?.groupValues?.get(1)
            } ?: return null
            return date.takeIf { runCatching { java.time.LocalDate.parse(it) }.isSuccess }
        }

        fun sha256Hex(text: String): String = MessageDigest.getInstance("SHA-256")
            .digest(text.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }
    }
}

/**
 * Origin-slot reconciliation for a persisted snapshot. Prefers the frozen capture
 * provenance (b503+) over the Python poll_ts bucket, because a LATE result can
 * complete after its own five-minute bucket ended. Older snapshots (no provenance)
 * fall back to the historical poll_ts bucket, which is what existing audits use.
 *
 * b503 R6: [validate] is the single provenance contract used by the operator completeness audit
 * (Pc2ParityAudit) over the exporter's `capture_provenance` column. Provenance that is present but
 * inconsistent is an explicit failure, never a silent fallback.
 */
object CaptureSlotReconciler {
    data class Origin(val sessionDate: String, val slotOrdinal: Int?, val source: String)

    /** Validated b503 origin of one snapshot. [slotStartUtcEpochSec] is the scheduled slot start (null for "closed"). */
    data class Provenance(val captureKey: String, val sessionDate: String, val slotOrdinal: Int?,
                          val slotStartEpochSec: Long?, val attemptId: String, val delivery: String,
                          /** C2: validated origin poll instant (inside the slot), null when not recorded. */
                          val originEpochSec: Long? = null,
                          /** C2: true when origin_poll_ts carries real seconds; false = minute precision (":00"). */
                          val originHasSeconds: Boolean = false)

    sealed class Validation {
        object Absent : Validation()
        data class Valid(val provenance: Provenance) : Validation()
        data class Invalid(val reason: String) : Validation()
    }

    private val IST_TS = Regex("^(20\\d{2}-\\d{2}-\\d{2})T(\\d{2}):(\\d{2}):(\\d{2})\\+0530$")
    /** A late result may legitimately complete long after its slot (stuck call); beyond this it is not that slot's capture. */
    const val MAX_COMPLETION_AFTER_SLOT_SEC = 6L * 3600

    private fun istEpochSec(text: String?): Long? {
        val m = IST_TS.matchEntire(text ?: return null) ?: return null
        return runCatching {
            java.time.LocalDateTime.of(java.time.LocalDate.parse(m.groupValues[1]),
                java.time.LocalTime.of(m.groupValues[2].toInt(), m.groupValues[3].toInt(), m.groupValues[4].toInt()))
                .toEpochSecond(java.time.ZoneOffset.ofHoursMinutes(5, 30))
        }.getOrNull()
    }

    /**
     * [provenance] is poll_summary_json.capture_provenance (null/JSON null = absent), [snapshotSessionDate] the stored
     * snapshot session_date, [completionEpochSec] the snapshot's own (original Python) poll_ts instant.
     */
    fun validate(provenance: Any?, snapshotSessionDate: String?, completionEpochSec: Long?): Validation {
        if (provenance == null || provenance == JSONObject.NULL) return Validation.Absent
        val p = provenance as? JSONObject ?: return Validation.Invalid("not_an_object")
        if (p.optString("schema") != "capture_provenance_v1") return Validation.Invalid("schema")
        val key = p.optString("capture_key")
        val session = FrozenCaptureRequest.sessionOfKey(key) ?: return Validation.Invalid("capture_key")
        if (p.optString("session_date") != session) return Validation.Invalid("session_date_vs_key")
        if (snapshotSessionDate != null && snapshotSessionDate != session) return Validation.Invalid("snapshot_session_vs_key")
        val ordinal = key.substringAfter('|').toIntOrNull()
        val ordinalField = if (p.isNull("slot_ordinal")) null else p.opt("slot_ordinal") as? Int
        if (ordinal != ordinalField) return Validation.Invalid("slot_ordinal_vs_key")
        val delivery = p.optString("delivery")
        if (delivery != "ON_TIME" && delivery != "LATE") return Validation.Invalid("delivery")
        val attempt = p.optString("attempt_id")
        if (attempt.isBlank() || attempt.length > 64) return Validation.Invalid("attempt_id")
        var slotStart: Long? = null
        var originSec: Long? = null
        var originSeconds = false
        if (ordinal != null) {
            val expected = "${session}T${FrozenCaptureRequest.slotStartHm(ordinal)}:00+0530"
            if (p.optString("slot_scheduled_ist") != expected) return Validation.Invalid("slot_scheduled_ist")
            slotStart = istEpochSec(expected)
            val origin = if (p.isNull("origin_poll_ts")) null else istEpochSec(p.optString("origin_poll_ts"))
                ?: return Validation.Invalid("origin_poll_ts")
            if (origin != null && (origin < slotStart!! || origin >= slotStart + FrozenCaptureRequest.POLL_SLOT_MINUTES * 60))
                return Validation.Invalid("origin_poll_ts_outside_slot")
            originSec = origin
            originSeconds = origin != null && !p.optString("origin_poll_ts").substring(17, 19).equals("00")
            if (completionEpochSec != null &&
                (completionEpochSec < slotStart!! || completionEpochSec - slotStart > MAX_COMPLETION_AFTER_SLOT_SEC))
                return Validation.Invalid("completion_outside_capture_window")
        }
        return Validation.Valid(Provenance(key, session, ordinal, slotStart, attempt, delivery, originSec, originSeconds))
    }

    fun originOf(snapshot: JSONObject): Origin? {
        val summary = snapshot.optJSONObject("poll_summary_json")
            ?: runCatching { JSONObject(snapshot.optString("poll_summary_json", "")) }.getOrNull()
        val prov = summary?.optJSONObject("capture_provenance")
        if (prov != null) {
            val key = prov.optString("capture_key")
            val date = FrozenCaptureRequest.sessionOfKey(key)
            if (date != null) return Origin(date, key.substringAfter('|', "").toIntOrNull(), "capture_provenance")
            return null // present but invalid provenance is never silently re-bucketed
        }
        val ts = snapshot.optString("poll_ts")
        val m = Regex("^(20\\d{2}-\\d{2}-\\d{2})T(\\d{2}):(\\d{2})").find(ts) ?: return null
        val minutes = m.groupValues[2].toInt() * 60 + m.groupValues[3].toInt()
        val ord = if (minutes < FrozenCaptureRequest.MARKET_OPEN_MINUTE ||
            minutes > FrozenCaptureRequest.MARKET_CLOSE_MINUTE) null
        else (minutes - FrozenCaptureRequest.MARKET_OPEN_MINUTE) / FrozenCaptureRequest.POLL_SLOT_MINUTES + 1
        return Origin(m.groupValues[1], ord, "poll_ts_bucket")
    }
}
