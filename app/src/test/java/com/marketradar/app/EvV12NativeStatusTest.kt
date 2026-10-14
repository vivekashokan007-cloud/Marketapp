package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/** 2.6.74 (Codex revised A): bounded native EV status, Paper-only Off/Observe setter, honest cache/fetch fields. */
class EvV12NativeStatusTest {
    private val pub = JSONObject().put("schema", "ev_calibration_publication_v1")
        .put("calibration_version", "ev_k_v1_2_live:p2026-10-13:3e2a3f7ed322")
        .put("available_at", "2026-10-14T17:28:01.478722+05:30")
        .put("observations", "x".repeat(4000)).put("R", 0.123).toString()

    private fun paper() = FakePrefs().also { it.edit().putString("execution_mode", "paper").commit() }
    private fun st(p: FakePrefs) = EvExperiment.status(p, EvAcceptance.executionMode(p), "2.6.74")

    @Test
    fun defaultAndUnknownModesResolveOffAndAreReportedHonestly() {
        val p = paper()
        var s = st(p)
        assertEquals("off", s.getString("mode")); assertEquals("UNSET", s.getString("mode_stored"))
        assertFalse(s.getBoolean("experiment_applies"))
        for (raw in listOf("", "ON", "garbage", "true")) {
            p.edit().putString(EvExperiment.PREF_MODE, raw).commit()
            s = st(p)
            assertEquals(raw, "off", s.getString("mode")); assertEquals(raw, "UNRECOGNIZED", s.getString("mode_stored"))
        }
        p.edit().putString(EvExperiment.PREF_MODE, " Observe ").commit()
        assertEquals("observe", st(p).getString("mode")); assertEquals("RECOGNIZED", st(p).getString("mode_stored"))
    }

    @Test
    fun statusMatchesNativePreferencesAndIsBounded() {
        val p = paper()
        p.edit().putString(EvExperiment.PREF_MODE, "observe").putString(EvExperiment.PREF_CALIBRATION, pub)
            .putLong(EvExperiment.PREF_CALIBRATION_FETCHED_MS, 2000L).putLong(EvExperiment.PREF_CALIBRATION_STORED_MS, 1500L)
            .commit()
        val s = st(p)
        assertEquals(EvExperiment.STATUS_SCHEMA, s.getString("schema"))
        assertEquals(EvExperiment.mode(p), s.getString("mode"))
        assertEquals(EvAcceptance.executionMode(p), s.getString("execution_mode"))
        assertTrue(s.getBoolean("experiment_applies")); assertTrue(s.getBoolean("can_change"))
        assertTrue(s.getBoolean("cache_present")); assertEquals("PARSED", s.getString("cache_parse"))
        assertEquals("ev_k_v1_2_live:p2026-10-13:3e2a3f7ed322", s.getString("cached_calibration_version"))
        assertEquals("2026-10-14T17:28:01.478722+05:30", s.getString("cached_available_at"))
        assertEquals(2000L, s.getLong("last_fetch_attempt_ms")); assertEquals(1500L, s.getLong("cache_stored_ms"))
        assertEquals("NOT_PERFORMED", s.getString("native_validation"))
        val txt = s.toString()
        assertTrue(txt.length < 1024)
        for (bad in listOf("observations", "xxxx", "\"R\"", "key", "token")) assertFalse(bad, txt.contains(bad))
    }

    @Test
    fun missingCacheRetainedOldCacheAndRecentAttemptAreHonest() {
        val p = paper()
        var s = st(p)
        assertFalse(s.getBoolean("cache_present")); assertEquals("ABSENT", s.getString("cache_parse"))
        assertTrue(s.isNull("cached_calibration_version")); assertTrue(s.isNull("last_fetch_attempt_ms"))
        assertFalse(s.getBoolean("fetch_success_recorded"))
        // A 2.6.73 device: an old cache row retained, a recent failed attempt, and no store time recorded ->
        // the attempt is reported as an attempt only; nothing claims a successful (fresh) fetch.
        p.edit().putString(EvExperiment.PREF_CALIBRATION, pub).putLong(EvExperiment.PREF_CALIBRATION_FETCHED_MS, 9_000_000L).commit()
        s = st(p)
        assertTrue(s.getBoolean("cache_present")); assertEquals(9_000_000L, s.getLong("last_fetch_attempt_ms"))
        assertTrue(s.isNull("cache_stored_ms")); assertFalse(s.getBoolean("fetch_success_recorded"))
        // Old store time + newer attempt: the two stay distinct.
        p.edit().putLong(EvExperiment.PREF_CALIBRATION_STORED_MS, 1_000L).commit()
        s = st(p)
        assertEquals(1_000L, s.getLong("cache_stored_ms")); assertEquals(9_000_000L, s.getLong("last_fetch_attempt_ms"))
        // Unreadable / hostile cache content is never echoed.
        p.edit().putString(EvExperiment.PREF_CALIBRATION, "{\"calibration_version\":\"<img src=x onerror=1>\"}").commit()
        s = st(p)
        assertEquals("UNREADABLE", s.getString("cache_parse")); assertTrue(s.isNull("cached_calibration_version"))
        p.edit().putString(EvExperiment.PREF_CALIBRATION, "not json").commit()
        assertEquals("UNREADABLE", st(p).getString("cache_parse"))
    }

    @Test
    fun setterIsPaperOnlyOffObserveAndNeverRewritesActive() {
        val p = paper()
        var r = EvExperiment.setMode(p, "observe", "paper")
        assertTrue(r.written); assertEquals("observe", r.mode); assertEquals("observe", EvExperiment.mode(p))
        r = EvExperiment.setMode(p, "active", "paper")
        assertFalse(r.written); assertEquals("ACTIVE_NOT_ENABLED", r.reason); assertEquals("observe", EvExperiment.mode(p))
        r = EvExperiment.setMode(p, "bogus", "paper")
        assertFalse(r.written); assertEquals("INVALID_MODE", r.reason); assertEquals("observe", EvExperiment.mode(p))
        for (exec in listOf("live", "sandbox", "")) {
            r = EvExperiment.setMode(p, "off", exec)
            assertFalse(exec, r.written); assertEquals("NOT_PAPER", r.reason); assertEquals("observe", EvExperiment.mode(p))
        }
        r = EvExperiment.setMode(p, "off", "paper")
        assertTrue(r.written); assertEquals("off", EvExperiment.mode(p))
        // An existing Active value is reported as Active (not Off) and stays until the user explicitly changes it.
        p.edit().putString(EvExperiment.PREF_MODE, "active").commit()
        assertEquals("active", st(p).getString("mode"))
        assertFalse(EvExperiment.setMode(p, "active", "paper").written)
        assertEquals("active", EvExperiment.mode(p))
        // Real: status still readable, changes disabled.
        val real = FakePrefs().also { it.edit().putString("execution_mode", "live").putString(EvExperiment.PREF_MODE, "observe").commit() }
        val s = st(real)
        assertEquals("live", s.getString("execution_mode")); assertFalse(s.getBoolean("can_change"))
        assertFalse(s.getBoolean("experiment_applies"))
    }

    /** Boundary: the poll path's ctx gating + refresh decision (the exact calls MarketWatchService makes). */
    @Test
    fun offNeverFetchesAndObserveStaysPaperOnly() {
        val dir = Files.createTempDirectory("evs").toFile()
        val now = 10L * EvExperiment.CALIBRATION_REFRESH_MS
        fun poll(mode: String, exec: String, last: Long): Pair<Boolean, Boolean> {
            val c = JSONObject().put("executionMode", exec)
            val applied = EvExperiment.applyContext(c, mode, exec, dir, now, now - 1, pub, "2.6.74")
            assertEquals(applied, c.has("evExperimentMode"))
            return applied to EvExperiment.shouldRefreshCalibration(applied, now, last)
        }
        assertEquals(false to false, poll("off", "paper", 0L))
        assertEquals(false to false, poll("observe", "live", 0L))
        assertEquals(false to false, poll("observe", "sandbox", 0L))
        assertEquals(true to true, poll("observe", "paper", 0L))
        assertEquals(true to false, poll("observe", "paper", now - 1000L))           // existing 30-min cadence kept
        assertEquals(true to true, poll("observe", "paper", now - EvExperiment.CALIBRATION_REFRESH_MS))
        // Wiring: the service gates the refresh on evApplied and records the store time only for a returned row.
        val src = listOf(File("."), File("app")).map { File(it, "src/main/java/com/marketradar/app/MarketWatchService.kt") }
            .first { it.isFile }.readText()
        assertTrue(src.contains("evRefreshCalibrationIfDue(evApplied, System.currentTimeMillis())"))
        assertTrue(src.contains("EvExperiment.shouldRefreshCalibration(applied, nowMs, last)"))
        assertTrue(Regex("if \\(pub != null\\) ed\\.putString\\(EvExperiment\\.PREF_CALIBRATION, pub\\)\\s*\\n\\s*\\.putLong\\(EvExperiment\\.PREF_CALIBRATION_STORED_MS").containsMatchIn(src))
        val nb = listOf(File("."), File("app")).map { File(it, "src/main/java/com/marketradar/app/NativeBridge.kt") }
            .first { it.isFile }.readText()
        assertTrue(nb.contains("@JavascriptInterface\n    fun evGetStatus(): String"))
        assertTrue(nb.contains("EvExperiment.setMode(prefs, mode, EvAcceptance.executionMode(prefs))"))
    }
}
