package com.marketradar.app

import org.json.JSONObject
import java.io.File

/**
 * D9 repair, acquisition bound: decides whether a poll must fetch the next-expiry option chain for one
 * index's direction scale.
 *
 * Without this, an own-expiry-day session fetched the extra chain on every poll (~78 requests), even
 * after a usable scale had already been frozen at the open reference and the brain had stopped reading
 * the chain at all. The decision is taken from the authority's OWN persisted state
 * (`<filesDir>/index_authority_v1/<session>.json` -> `scales[index]`), not from a second mirror in
 * SharedPreferences, so the two can never drift apart.
 *
 * Fetch only when the scale is actually needed:
 *   - not this index's own expiry day      -> no fetch (the front/trading chain prices the scale)
 *   - no scale recorded yet this session   -> fetch (the open reference may be this poll)
 *   - a usable scale is frozen             -> no fetch (the brain ignores the chain from here on)
 *   - a RECOVERABLE failure is stored      -> fetch (a later poll may recover it)
 *   - an UNRECOVERABLE failure is stored   -> no fetch (a chain cannot help)
 *
 * [RECOVERABLE_REASONS] must stay equal to `index_authority_v1.SCALE_RECOVERABLE_REASONS`; a test
 * reads the Python source and asserts that, so the two cannot silently diverge.
 */
object DirectionScaleNeed {
    const val NOT_EXPIRY_DAY = "NOT_EXPIRY_DAY"
    const val NO_SCALE_YET = "NO_SCALE_YET"
    const val SCALE_FROZEN_OK = "SCALE_FROZEN_OK"
    const val RECOVERABLE_FAILURE = "RECOVERABLE_FAILURE"
    const val UNRECOVERABLE_FAILURE = "UNRECOVERABLE_FAILURE"
    const val STATE_UNREADABLE = "STATE_UNREADABLE"

    /** Mirrors index_authority_v1.SCALE_RECOVERABLE_REASONS. */
    val RECOVERABLE_REASONS: Set<String> = setOf(
        "EXPIRY_DAY_CHAIN_ONLY",
        "OPEN_REF_CHAIN_NOT_OBSERVED",
        "SCALE_STATE_UNAVAILABLE",
        "NEXT_EXPIRY_CHAIN_MISSING",
        "NEXT_EXPIRY_CHAIN_EMPTY",
        "NEXT_EXPIRY_INVALID"
    )

    data class Decision(val fetch: Boolean, val reason: String)

    /** True when this index's front (trading) expiry cannot price a forward session move. */
    fun isOwnExpiryDay(tradeExpiry: String?, today: String): Boolean {
        val t = (tradeExpiry ?: "").trim()
        return t.isNotEmpty() && t <= today
    }

    /**
     * @param stateJson contents of the authority's session state file, or null when absent/unreadable.
     *                  An absent file means no scale has been recorded yet, which is a fetch; an
     *                  unreadable one is also a fetch, because failing to read must not silently
     *                  suppress the acquisition that closes the outage.
     */
    fun decide(index: String, tradeExpiry: String?, today: String, stateJson: String?): Decision {
        if (!isOwnExpiryDay(tradeExpiry, today)) return Decision(false, NOT_EXPIRY_DAY)
        if (stateJson == null || stateJson.isBlank()) return Decision(true, NO_SCALE_YET)
        val scale = try {
            JSONObject(stateJson).optJSONObject("scales")?.optJSONObject(index)
        } catch (e: Exception) {
            return Decision(true, STATE_UNREADABLE)
        }
        val status = scale?.optString("status", "")?.takeIf { it.isNotEmpty() }
            ?: return Decision(true, NO_SCALE_YET)
        if (status == "OK") return Decision(false, SCALE_FROZEN_OK)
        val reason = scale.optString("reason", "")
        return if (reason in RECOVERABLE_REASONS) Decision(true, RECOVERABLE_FAILURE)
        else Decision(false, UNRECOVERABLE_FAILURE)
    }

    /** Reads the authority's session state file; null when it does not exist or cannot be read. */
    fun readState(filesDir: File, session: String): String? = try {
        val f = File(IndexAuthorityContext.stateDir(filesDir), "$session.json")
        if (f.isFile) f.readText() else null
    } catch (e: Exception) {
        null
    }

    /** The ctx keys this repair owns, for both indices. Cleared every poll before fresh values go in. */
    val CTX_KEYS: List<String> = listOf("nfScaleChain", "nfScaleExpiry", "bnfScaleChain", "bnfScaleExpiry")

    /**
     * Removes every scale ctx key. The brain context is persisted to SharedPreferences and reloaded on
     * the next poll, so a key left behind by an earlier poll (or an earlier SESSION) would otherwise
     * survive: a fetch failure that wrote only a fresh expiry could pair that new expiry with a stale
     * chain from a previous expiry-day session. Clearing both keys for both indices first makes the
     * published pair always come from one poll, or not be published at all.
     */
    fun clearCtx(ctx: JSONObject) {
        for (k in CTX_KEYS) ctx.remove(k)
    }
}
