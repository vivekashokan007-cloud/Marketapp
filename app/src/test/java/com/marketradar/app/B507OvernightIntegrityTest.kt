package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.time.LocalDateTime
import java.time.ZoneOffset

/** b507 - Codex overnight-integrity conditions 1-7 and 10 on the native side (Android-free). */
class B507OvernightIntegrityTest {
    companion object {
        val IST: ZoneOffset = ZoneOffset.ofHoursMinutes(5, 30)
        fun ist(y: Int, mo: Int, d: Int, h: Int, mi: Int) = LocalDateTime.of(y, mo, d, h, mi).toInstant(IST).toEpochMilli()
        const val S = "2026-10-15"
        val NOW = ist(2026, 10, 15, 9, 30)
        fun eve(dow: String = "46000", crude: String = "62.5", gift: String = "25200") =
            JSONObject().put("dow", dow).put("crude", crude).put("gift", gift)
        fun mor(dow: String = "46300", crude: String = "61.0", gift: String = "25300") =
            JSONObject().put("dowClose", dow).put("crudeSettle", crude).put("giftSpot", gift).put("fiiCash", "-1200")
                .put("fiiShortPct", "71").put("diiCash", "900")
        /** The deterministic pair used by the real-chain goldens. */
        fun goldenState(): JSONObject {
            var st = SessionInputs.withEvening(JSONObject(), ist(2026, 10, 14, 20, 0), eve()).state!!
            st = SessionInputs.withMorning(st, ist(2026, 10, 15, 9, 5), mor()).state!!
            return st
        }
        /** A persisted context polluted exactly like 15 Oct: the 14 Oct inputs were inherited. */
        fun pollutedContext(): JSONObject = JSONObject()
            .put("tradeMode", "positional")
            .put("morning_input", JSONObject().put("dowClose", 46900).put("_session", "2026-10-14"))
            .put("eveningClose", JSONObject().put("dow", 47100).put("crude", 60.1).put("gift", 25550))
            .put("globalDirection", JSONObject().put("dowClose", 46900).put("crudeSettle", 59.0))
            .put("overnightDelta", JSONObject().put("summary", "🔴 OVERNIGHT BEARISH"))
            .put("morningBias", JSONObject().put("bias", "BEAR"))
            .put("fiiCash", -9999)
        fun repoFile(rel: String): File = listOf(File("../$rel"), File(rel)).first { it.parentFile?.isDirectory == true }
        fun lockObj(session: String) = JSONObject(mor().toString()).put("_session", session)
    }

    @Test fun canonicalNumbersAreCrossLanguageStable() {
        assertEquals("46000.5", SessionInputs.canonical("46000.50"))
        assertEquals("62.5", SessionInputs.canonical(62.5))
        assertEquals("25200", SessionInputs.canonical("25200.0"))
        assertNull(SessionInputs.canonical(" 0 ")); assertNull(SessionInputs.canonical("-1"))
        assertNull(SessionInputs.canonical("NaN")); assertNull(SessionInputs.canonical("abc")); assertNull(SessionInputs.canonical(null))
    }

    @Test fun referenceConvention() {
        assertEquals("2026-10-15", SessionInputs.intendedSession(ist(2026, 10, 14, 20, 0)))
        assertEquals("2026-10-15", SessionInputs.intendedSession(ist(2026, 10, 15, 7, 45)))
        assertEquals("2026-10-16", SessionInputs.intendedSession(ist(2026, 10, 15, 15, 30)))
        assertEquals("2026-10-19", SessionInputs.intendedSession(ist(2026, 10, 17, 11, 0)))
        assertEquals("2026-10-21", SessionInputs.intendedSession(ist(2026, 10, 19, 20, 0)))  // 20 Oct holiday
    }

    @Test fun assemblyScrubsInheritedInputsAndInjectsOnlyTheCurrentPair() {
        val ctx = SessionInputs.assemble(pollutedContext(), goldenState(), S, NOW, lockObj(S))
        assertEquals("positional", ctx.getString("tradeMode"))
        assertFalse(ctx.has("eveningClose")); assertFalse(ctx.has("overnightDelta")); assertFalse(ctx.has("morningBias"))
        assertEquals("46300", ctx.getJSONObject("morning_input").getString("dowClose"))
        assertEquals("-1200", ctx.get("fiiCash"))
        val gd = ctx.getJSONObject("globalDirection")
        assertEquals(46300.0, gd.getDouble("dowClose"), 0.0); assertEquals(S, gd.getString("session"))
        val ov = ctx.getJSONObject("overnightInputs")
        assertEquals(S, ov.getString("session")); assertTrue(ov.has("pair_revision"))
        assertEquals("OK", SessionInputs.status(goldenState(), S, NOW))
    }

    @Test fun serviceOnlyRolloverCannotReuseYesterdaysPair() {
        // 15 Oct service polls with only the 14 Oct lock + 13 Oct evening stored (no PWA involvement)
        var st = SessionInputs.withEvening(JSONObject(), ist(2026, 10, 13, 20, 0), eve()).state!!
        st = SessionInputs.withMorning(st, ist(2026, 10, 14, 9, 5), mor()).state!!
        val ctx = SessionInputs.assemble(pollutedContext(), st, S, NOW, lockObj("2026-10-14"))
        assertFalse(ctx.has("morning_input")); assertFalse(ctx.has("globalDirection")); assertFalse(ctx.has("fiiCash"))
        assertEquals("MORNING_WRONG_SESSION", SessionInputs.status(st, S, NOW))
        // mixed pair: fresh morning, yesterday's evening
        val mixed = SessionInputs.withMorning(st, ist(2026, 10, 15, 9, 5), mor()).state!!
        assertEquals("REFERENCE_WRONG_SESSION", SessionInputs.status(mixed, S, NOW))
    }

    @Test fun staleFutureWrongSessionAndCalendar() {
        assertEquals("FUTURE_CAPTURE", SessionInputs.status(goldenState(), S, ist(2026, 10, 15, 9, 0)))
        assertEquals("WRONG_SESSION", SessionInputs.status(goldenState(), "bad", NOW))
        assertEquals("NON_TRADING_SESSION", SessionInputs.status(goldenState(), "2026-10-17", NOW))
        assertEquals("NON_TRADING_SESSION", SessionInputs.status(goldenState(), "2026-10-20", NOW))
        assertEquals("CALENDAR_UNAVAILABLE", SessionInputs.status(goldenState(), "2027-01-04", NOW))
        assertEquals("NON_TRADING_SESSION", SessionInputs.withMorning(JSONObject(), ist(2026, 10, 17, 9, 5), mor()).error)
        assertEquals("SESSION_CLOSED", SessionInputs.withMorning(JSONObject(), ist(2026, 10, 15, 15, 31), mor()).error)
        assertEquals("INVALID_VALUES", SessionInputs.withEvening(JSONObject(), NOW, eve(dow = "0")).error)
    }

    @Test fun sameMorningReferenceIsValid() {
        var st = SessionInputs.withEvening(JSONObject(), ist(2026, 10, 15, 7, 45), eve()).state!!
        st = SessionInputs.withMorning(st, ist(2026, 10, 15, 9, 5), mor()).state!!
        assertEquals("OK", SessionInputs.status(st, S, NOW))
    }

    @Test fun legacyMalformedOrUndatedInputsRequireReentry() {
        assertEquals("MORNING_NOT_LOCKED", SessionInputs.status(SessionInputs.read("not json"), S, NOW))
        val legacy = JSONObject().put("morning", JSONObject().put("dowClose", "1")).put("evening", JSONObject().put("dow", "1"))
        assertEquals("MISSING_IDENTITY", SessionInputs.status(legacy, S, NOW))
        // a legacy undated morning_input is never accepted as today's lock
        val ctx = SessionInputs.assemble(JSONObject().put("morning_input", JSONObject().put("fiiCash", 1)), JSONObject(), S, NOW,
            JSONObject().put("fiiCash", 1))
        assertFalse(ctx.has("morning_input"))
        assertEquals("NO_EVENING_REFERENCE",
            SessionInputs.status(SessionInputs.withMorning(JSONObject(), ist(2026, 10, 15, 9, 5), mor()).state!!, S, NOW))
    }

    @Test fun restartRestoreKeepsIdentityAndRestoredDraftsCannotLock() {
        val st = goldenState()
        val restored = SessionInputs.read(st.toString())
        assertEquals(SessionInputs.inputRevision(st, S), SessionInputs.inputRevision(restored, S))
        assertEquals("OK", SessionInputs.status(restored, S, NOW))
        // restoring form values only reaches setGlobalDirection -> live quotes; it never creates a morning record
        val live = SessionInputs.liveQuotes(JSONObject().put("dowClose", 46300).put("dowNow", 46350), S, NOW)
        assertFalse(live.has("dowClose"))
        assertNull(SessionInputs.globalDirection(JSONObject(), S, live))
        assertEquals("MORNING_NOT_LOCKED", SessionInputs.status(JSONObject(), S, NOW))
    }

    @Test fun persistenceFailureAndReadback() {
        val prior = JSONObject()
        val built = SessionInputs.withEvening(prior, ist(2026, 10, 14, 20, 0), eve())
        val rec = built.record!!
        val failed = SessionInputs.acknowledge(false, "evening", rec, built.state.toString(), S, NOW)
        assertFalse(failed.getBoolean("ok")); assertTrue(failed.getString("error").startsWith("SAVE_NOT_PERSISTED"))
        val notStored = SessionInputs.acknowledge(true, "evening", rec, prior.toString(), S, NOW)
        assertFalse(notStored.getBoolean("ok"))
        val ok = SessionInputs.acknowledge(true, "evening", rec, built.state.toString(), S, NOW)
        assertTrue(ok.getBoolean("ok"))
        assertEquals(rec.getString("revision"), ok.getString("revision"))
        assertEquals("46000", ok.getJSONObject("record").getString("dow"))
        assertEquals("MORNING_NOT_LOCKED", ok.getJSONObject("state").getString("status"))
    }

    @Test fun saveDuringAnalysisCannotPublishOldRevision() {
        val st = goldenState()
        val start = SessionInputs.inputRevision(st, S)
        val resaved = SessionInputs.withEvening(st, ist(2026, 10, 15, 9, 20), eve(dow = "46050")).state!!
        val current = SessionInputs.inputRevision(resaved, S)
        assertNotEquals(start, current)
        assertFalse(AnalysisRefreshCoordinator.mayPublish(start, current))
        assertTrue(AnalysisRefreshCoordinator.mayPublish(current, current))
    }

    @Test fun rapidSavesSettleOnTheNewestRevision() {
        val c = AnalysisRefreshCoordinator()
        assertTrue(c.request("r1"))
        assertEquals("r1", c.take())
        assertFalse(c.request("r2")); assertFalse(c.request("r3"))      // coalesced while running
        assertTrue(c.finish())
        assertEquals("r3", c.take())                                      // r2 never computed
        assertFalse(c.finish()); assertFalse(c.isRunning())
        assertTrue(c.request("r4"))
    }

    @Test fun serviceWiringGatesSideEffectsAndChecksRevisionBeforePublish() {
        val svc = repoFile("app/src/main/java/com/marketradar/app/MarketWatchService.kt").readText()
        for (needle in listOf(
            "SessionInputs.assemble(ctxObj", "val startInputRevision", "AnalysisRefreshCoordinator.mayPublish(startInputRevision",
            "val publishNow = publish &&", "publishNow && BrainResultStore.save(", "if (sideEffects && publish) processUnifiedBrainNotifications",
            "if (build3Ab != null && sideEffects && publish)", "if (isRefresh) (lastAnalysisStrikeOiJson", "paperMenuAttemptSlot = if (isRefresh) null",
            "val evApplied = if (isRefresh) false", "recordPaperMenuAttemptFailure(slot, \"superseded_input_revision\")",
            "intent?.action == ACTION_ANALYSIS_REFRESH",
            "ANALYSIS_REFRESH_WAITING_FOR_POLL")) assertTrue(needle, svc.contains(needle))
        assertFalse(svc.contains("prefs.getString(\"evening_close_baseline\""))
        assertFalse(svc.contains("prefs.getString(\"global_direction\""))
        val nb = repoFile("app/src/main/java/com/marketradar/app/NativeBridge.kt").readText()
        assertFalse(nb.contains("BASELINE_HEALED_WITH_DATE")); assertFalse(nb.contains("put(\"date\", today).toString()"))
        assertTrue(nb.contains("SessionInputs.acknowledge(committed"))
    }

    /** Real chain, stage 1: native context assembly -> committed golden consumed by Python and the PWA tests. */
    @Test fun realChainContextGolden() {
        val ctx = SessionInputs.assemble(pollutedContext(), goldenState(), S, NOW, lockObj(S))
        val unavailable = SessionInputs.assemble(pollutedContext(), SessionInputs.withMorning(JSONObject(),
            ist(2026, 10, 15, 9, 5), mor()).state!!, S, NOW, lockObj(S))
        val rollover = run {
            var st = SessionInputs.withEvening(JSONObject(), ist(2026, 10, 13, 20, 0), eve()).state!!
            st = SessionInputs.withMorning(st, ist(2026, 10, 14, 9, 5), mor()).state!!
            SessionInputs.assemble(pollutedContext(), st, S, NOW, lockObj("2026-10-14"))
        }
        val golden = JSONObject()
            .put("ok", ctx).put("no_evening", unavailable).put("rollover", rollover)
            .put("native_state_ok", SessionInputs.publicState(goldenState(), S, NOW))
            .put("native_state_no_evening", SessionInputs.publicState(SessionInputs.withMorning(JSONObject(),
                ist(2026, 10, 15, 9, 5), mor()).state!!, S, NOW))
            .put("native_state_rollover", SessionInputs.publicState(run {
                var st = SessionInputs.withEvening(JSONObject(), ist(2026, 10, 13, 20, 0), eve()).state!!
                SessionInputs.withMorning(st, ist(2026, 10, 14, 9, 5), mor()).state!!
            }, S, NOW))
            .put("native_state_resaved", SessionInputs.publicState(SessionInputs.withEvening(goldenState(),
                ist(2026, 10, 15, 9, 20), eve(dow = "46050")).state!!, S, NOW))
        val file = repoFile("tools/b507_chain/native_context_golden.json")
        if (System.getenv("B507_UPDATE_GOLDEN") == "1") file.writeText(golden.toString(2) + "\n")
        assertTrue("golden missing: ${file.path}", file.isFile)
        assertEquals(JSONObject(file.readText()).toString(), golden.toString())
    }
}
