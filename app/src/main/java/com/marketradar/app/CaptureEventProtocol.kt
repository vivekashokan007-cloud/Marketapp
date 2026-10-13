package com.marketradar.app

import org.json.JSONObject

/** b503: client classification of capture_event_record responses (pure; unit-tested). */
object CaptureEventProtocol {
    fun classify(code: Int?, body: String?): FrozenEvidenceOutbox.Outcome {
        if (code != null && code in 200..299) {
            val status = runCatching { JSONObject(body ?: "").optString("status") }.getOrDefault("")
            return when (status) {
                "INSERTED", "DUPLICATE" -> FrozenEvidenceOutbox.Outcome.ACKNOWLEDGED
                "REJECTED_CONFLICT" -> FrozenEvidenceOutbox.Outcome.QUARANTINE
                else -> FrozenEvidenceOutbox.Outcome.RETRY // unproven acknowledgement is not an ACK
            }
        }
        val pg = body?.let { Pc2TelemetryOutbox.errorCodeOf(it) }
        return when {
            code == null -> FrozenEvidenceOutbox.Outcome.RETRY
            pg == "PT403" || pg == "PT429" -> FrozenEvidenceOutbox.Outcome.RETRY       // not designated yet / quota
            pg == "PGRST202" || code == 404 -> FrozenEvidenceOutbox.Outcome.RETRY     // capability absent: keep queued
            pg == "23514" || pg == "22023" || pg == "22P02" -> FrozenEvidenceOutbox.Outcome.QUARANTINE
            code in 400..499 && code !in listOf(401, 403, 408, 429) -> FrozenEvidenceOutbox.Outcome.QUARANTINE
            else -> FrozenEvidenceOutbox.Outcome.RETRY
        }
    }
}
