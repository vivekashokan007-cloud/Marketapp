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

        fun sha256Hex(text: String): String = MessageDigest.getInstance("SHA-256")
            .digest(text.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }
    }
}

/**
 * Origin-slot reconciliation for a persisted snapshot. Prefers the frozen capture
 * provenance (b503+) over the Python poll_ts bucket, because a LATE result can
 * complete after its own five-minute bucket ended. Older snapshots (no provenance)
 * fall back to the historical poll_ts bucket, which is what existing audits use.
 */
object CaptureSlotReconciler {
    data class Origin(val sessionDate: String, val slotOrdinal: Int?, val source: String)

    fun originOf(snapshot: JSONObject): Origin? {
        val summary = snapshot.optJSONObject("poll_summary_json")
            ?: runCatching { JSONObject(snapshot.optString("poll_summary_json", "")) }.getOrNull()
        val prov = summary?.optJSONObject("capture_provenance")
        if (prov != null) {
            val key = prov.optString("capture_key")
            val date = key.substringBefore('|')
            val ord = key.substringAfter('|', "").toIntOrNull()
            if (date.isNotBlank()) return Origin(date, ord, "capture_provenance")
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
