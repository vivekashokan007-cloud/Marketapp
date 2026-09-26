package com.marketradar.app

import android.content.SharedPreferences
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** Minimal in-memory SharedPreferences for JVM tests (commit/apply are synchronous). */
internal class FakePrefs : SharedPreferences {
    val map = mutableMapOf<String, Any?>()
    override fun getAll(): MutableMap<String, *> = map.toMutableMap()
    override fun getString(key: String?, defValue: String?): String? = map[key] as? String ?: defValue
    override fun getStringSet(key: String?, defValues: MutableSet<String>?): MutableSet<String>? = defValues
    override fun getInt(key: String?, defValue: Int): Int = map[key] as? Int ?: defValue
    override fun getLong(key: String?, defValue: Long): Long = map[key] as? Long ?: defValue
    override fun getFloat(key: String?, defValue: Float): Float = map[key] as? Float ?: defValue
    override fun getBoolean(key: String?, defValue: Boolean): Boolean = map[key] as? Boolean ?: defValue
    override fun contains(key: String?): Boolean = map.containsKey(key)
    override fun registerOnSharedPreferenceChangeListener(l: SharedPreferences.OnSharedPreferenceChangeListener?) {}
    override fun unregisterOnSharedPreferenceChangeListener(l: SharedPreferences.OnSharedPreferenceChangeListener?) {}
    override fun edit(): SharedPreferences.Editor = object : SharedPreferences.Editor {
        private val pending = mutableMapOf<String, Any?>()
        private val removed = mutableSetOf<String>()
        private var clear = false
        override fun putString(k: String?, v: String?) = apply { pending[k!!] = v }
        override fun putStringSet(k: String?, v: MutableSet<String>?) = apply { pending[k!!] = v }
        override fun putInt(k: String?, v: Int) = apply { pending[k!!] = v }
        override fun putLong(k: String?, v: Long) = apply { pending[k!!] = v }
        override fun putFloat(k: String?, v: Float) = apply { pending[k!!] = v }
        override fun putBoolean(k: String?, v: Boolean) = apply { pending[k!!] = v }
        override fun remove(k: String?) = apply { removed.add(k!!) }
        override fun clear() = apply { clear = true }
        override fun commit(): Boolean { flush(); return true }
        override fun apply() { flush() }
        private fun flush() {
            if (clear) map.clear()
            removed.forEach { map.remove(it) }
            map.putAll(pending)
        }
    }
}

class B3PositionMarkStoreTrustTest {
    private val now = 1_790_000_000_000L // arbitrary fixed epoch ms

    private fun row(
        tradeId: String,
        pnl: Double,
        trust: JSONObject? = null,
        quality: String = "OK"
    ) = JSONObject().apply {
        put("trade_id", tradeId)
        put("tick_ts", "2026-09-28T04:00:00.000Z")
        put("valuation_quality", quality)
        put("policy_action", "HOLD")
        put("policy_reason", "no shadow exit rule matched")
        put("source", "P1_REST_60S")
        put("current_pnl", pnl)
        put("executable_mark", 700.0)
        put("leg_count", 4)
        put("mark_basis", "EXECUTABLE")
        put("auth_source", "DAILY")
        put("index_key", "NF")
        put("strategy_type", "IRON_BUTTERFLY")
        put("legs_json", JSONArray())
        put("policy_trace_json", JSONObject().apply { if (trust != null) put("mark_store_trust", trust) })
    }

    private fun paperTrust(state: String, srcMs: Long?, fp: String = "fp1", cause: String? = null) = JSONObject().apply {
        put("paper", true)
        put("trust_state", state)
        put("trust_cause", cause ?: JSONObject.NULL)
        put("quote_validity_state", if (state == TRUST_TRUSTED) QV_VALID else QV_INVALID)
        put("earliest_source_ms", srcMs ?: JSONObject.NULL)
        put("earliest_source_ts", "2026-09-28T09:30:00+05:30")
        put("latest_source_ms", srcMs ?: JSONObject.NULL)
        put("book_fingerprint", fp)
    }

    private fun realTrust() = paperTrust(TRUST_UNTRUSTED, now - 1_000L).apply { put("paper", false) }

    private fun present(prefs: FakePrefs, id: String, at: Long = now): JSONObject =
        JSONObject(PositionMarkStore.presentationJson(prefs, at)).getJSONObject(id)

    @Test
    fun untrustedPaperMarkDoesNotBecomeLastValidOrLiveFull() {
        val prefs = FakePrefs()
        PositionMarkStore.recordRows(prefs, JSONArray().put(row("276", -22092.0,
            paperTrust(TRUST_UNTRUSTED, now - 1_000L, cause = CAUSE_WIDE_LIQUIDATION_BOOK))), now)
        val p = present(prefs, "276")
        assertEquals(PositionMarkStore.UNAVAILABLE, p.getString("display_state"))
        assertTrue(p.isNull("last_valid_current_pnl"))
        assertEquals(TRUST_UNTRUSTED, p.getString("mark_trust_state"))
        assertEquals(CAUSE_WIDE_LIQUIDATION_BOOK, p.getString("mark_trust_cause"))
        assertTrue(p.getBoolean("untrusted_pending_revalidation"))
        assertFalse(p.getBoolean("display_is_actionable"))
        // Recovery state exposed to the tick service.
        val rec = PositionMarkStore.trustRecoveryState(prefs, "276")
        assertEquals(now - 1_000L, rec.first)
        assertEquals("fp1", rec.second)
    }

    @Test
    fun untrustedAfterTrustedKeepsLastValidButDropsLiveFull_thenRecovers() {
        val prefs = FakePrefs()
        PositionMarkStore.recordRows(prefs, JSONArray().put(row("274", -7000.0,
            paperTrust(TRUST_TRUSTED, now - 2_000L))), now - 1_000L)
        assertEquals(PositionMarkStore.LIVE_FULL, present(prefs, "274").getString("display_state"))
        PositionMarkStore.recordRows(prefs, JSONArray().put(row("274", -22000.0,
            paperTrust(TRUST_UNTRUSTED, now - 500L, fp = "fpX", cause = CAUSE_UNRESOLVED))), now)
        val stale = present(prefs, "274")
        assertEquals(PositionMarkStore.STALE_LAST_VALID, stale.getString("display_state"))
        assertEquals(-7000.0, stale.getDouble("last_valid_current_pnl"), 0.0)
        PositionMarkStore.recordRows(prefs, JSONArray().put(row("274", -6900.0,
            paperTrust(TRUST_TRUSTED, now + 59_000L, fp = "fpY"))), now + 60_000L)
        val back = present(prefs, "274", now + 60_000L)
        assertEquals(PositionMarkStore.LIVE_FULL, back.getString("display_state"))
        assertEquals(-6900.0, back.getDouble("last_valid_current_pnl"), 0.0)
        assertFalse(back.getBoolean("untrusted_pending_revalidation"))
        assertEquals(null to null, PositionMarkStore.trustRecoveryState(prefs, "274"))
    }

    @Test
    fun trustedPaperMarkWithOldSourceTimeIsStaleEvenIfRecordedNow() {
        val prefs = FakePrefs()
        PositionMarkStore.recordRows(prefs, JSONArray().put(row("280", 500.0,
            paperTrust(TRUST_TRUSTED, now - 200_000L))), now)
        val p = present(prefs, "280")
        assertEquals(PositionMarkStore.STALE_LAST_VALID, p.getString("display_state"))
        assertTrue(p.getLong("last_valid_age_ms") >= 200_000L)
        assertEquals(PositionMarkStore.FRESHNESS_BASIS_SOURCE, p.getString("freshness_basis"))
    }

    @Test
    fun clockRollbackNeverMakesPaperMarkFresh() {
        val prefs = FakePrefs()
        // Recorded at 'now', then the device clock is rolled back 10 minutes.
        PositionMarkStore.recordRows(prefs, JSONArray().put(row("282", 500.0,
            paperTrust(TRUST_TRUSTED, now - 1_000L))), now)
        val p = present(prefs, "282", now - 600_000L)
        assertEquals(PositionMarkStore.STALE_LAST_VALID, p.getString("display_state"))
        assertEquals(Long.MAX_VALUE, p.getLong("latest_age_ms"))
    }

    @Test
    fun realAndLegacyRowsKeepByteIdenticalStoreAndPresentation() {
        // Pre-B3 row (no trust object) vs B3 Real row (paper=false trust object).
        val legacy = FakePrefs()
        val real = FakePrefs()
        PositionMarkStore.recordRows(legacy, JSONArray().put(row("900", -22092.0)), now)
        PositionMarkStore.recordRows(real, JSONArray().put(row("900", -22092.0, realTrust())), now)
        assertEquals(legacy.getString("position_mark_state_v1", ""), real.getString("position_mark_state_v1", ""))
        for (at in listOf(now, now + 200_000L, now - 600_000L)) {
            assertEquals(
                PositionMarkStore.presentationJson(legacy, at),
                PositionMarkStore.presentationJson(real, at)
            )
        }
        val p = present(real, "900")
        // Legacy rule: an untrusted-by-A1 Real mark still becomes last-valid (unchanged).
        assertEquals(PositionMarkStore.LIVE_FULL, p.getString("display_state"))
        assertFalse(p.has("mark_trust_state"))
        assertFalse(p.has("freshness_basis"))
        // Legacy clock-rollback behaviour is deliberately unchanged for Real.
        assertEquals(PositionMarkStore.LIVE_FULL, present(real, "900", now - 600_000L).getString("display_state"))
    }

    @Test
    fun mixedPaperAndRealPositionsAreIsolated() {
        val prefs = FakePrefs()
        PositionMarkStore.recordRows(prefs, JSONArray()
            .put(row("P1", -22092.0, paperTrust(TRUST_UNTRUSTED, now - 1_000L, cause = CAUSE_WIDE_LIQUIDATION_BOOK)))
            .put(row("R1", -22092.0, realTrust())), now)
        val all = JSONObject(PositionMarkStore.presentationJson(prefs, now))
        assertEquals(PositionMarkStore.UNAVAILABLE, all.getJSONObject("P1").getString("display_state"))
        assertEquals(PositionMarkStore.LIVE_FULL, all.getJSONObject("R1").getString("display_state"))
        assertNotEquals(all.getJSONObject("P1").toString(), all.getJSONObject("R1").toString())
    }
}
