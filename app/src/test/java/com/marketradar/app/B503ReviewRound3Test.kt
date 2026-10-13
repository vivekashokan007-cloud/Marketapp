package com.marketradar.app

import com.marketradar.app.B503Harness.Companion.pythonSnapshot
import com.marketradar.app.B503Harness.Companion.request
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.time.Instant

/** Codex round-3 review: C1 (skip durability), C2 (actual-origin matching), C3 (generated cohort manifest + audit). */
class B503ReviewRound3Test {
    // ------------------------------------------------------------------ C1
    private fun coordinator(h: B503Harness, control: CaptureFinisher) = CaptureCoordinator(ManualExecutor(), ManualFinisher("fin", 2), control, h.engine,
        waiter = CaptureCoordinator.Waiter { _, _ -> throw java.util.concurrent.TimeoutException() }, scheduleStuckCheck = { _, _ -> })
    private val skipped get() = request(slot = "2026-10-13|9", pollTime = "09:55", attempt = "skipped-slot")

    @Test fun c1_normalQueueAdmission_processDeath_restartDeliversOnce_withNfBnfExpectation() {
        val h = B503Harness(); val control = ManualFinisher("control", 64)
        val c = coordinator(h, control)
        c.submit(request(attempt = "running"), 0L) { pythonSnapshot("2026-10-13T09:50:42+0530") }
        val queued = control.jobs.size
        assertEquals(CaptureCoordinator.Outcome.SKIPPED_INFLIGHT, c.submit(skipped, 0L) { error("never") })
        assertEquals("the control job was accepted, not run", queued + 1, control.jobs.size)
        val marker = h.journal.read(skipped.attemptId); assertNotNull(marker)
        assertEquals("TERMINAL", marker!!.getString("stage"))
        val b = B503Harness(root = h.root, runId = "run-B")
        b.engine.reconcile(emptySet()); b.engine.reconcile(emptySet())
        assertEquals(listOf("INFLIGHT_SKIPPED_SLOT"), b.eventTypes().filter { it == "INFLIGHT_SKIPPED_SLOT" })
        val ev = b.files(CaptureRuntime.EVENTS_DIR).map { JSONObject(JSONObject(it.readText()).getString("payload")) }
            .single { it.getString("event_type") == "INFLIGHT_SKIPPED_SLOT" }
        assertEquals("2026-10-13|9", ev.getString("capture_key"))
        assertEquals("not_computed", ev.getJSONObject("detail").getJSONObject("generated_cohort").getString("NF"))
        assertEquals("not_computed", ev.getJSONObject("detail").getJSONObject("generated_cohort").getString("BNF"))
        assertEquals(null, b.journal.read(skipped.attemptId))
    }

    @Test fun c1_controlJobRunsAfterCheckpoint_sameBytes_noDuplicate() {
        val h = B503Harness(); val control = ManualFinisher("control", 64)
        val c = coordinator(h, control)
        c.submit(request(attempt = "running"), 0L) { pythonSnapshot("2026-10-13T09:50:42+0530") }
        c.submit(skipped, 0L) { error("never") }
        control.runAll()
        h.engine.reconcile { c.isLive(it) }
        assertEquals(1, h.eventTypes().count { it == "INFLIGHT_SKIPPED_SLOT" })
        assertEquals(null, h.journal.read(skipped.attemptId))
        assertEquals(0, h.events.status().quarantinedTotal) // identical bytes, no collision
    }

    @Test fun c1_storageRefusal_isReportedAndRecordedInline() {
        val fs = FaultyFs().apply { failWriteMatching = Regex("skipped") }
        val h = B503Harness(journalFs = fs); val control = ManualFinisher("control", 64)
        val c = coordinator(h, control)
        c.submit(request(attempt = "running"), 0L) { pythonSnapshot("2026-10-13T09:50:42+0530") }
        val queued = control.jobs.size
        assertEquals(CaptureCoordinator.Outcome.SKIPPED_INFLIGHT, c.submit(skipped, 0L) { error("never") })
        assertEquals("not trusted to the queue", queued, control.jobs.size)
        assertEquals(listOf("INFLIGHT_SKIPPED_SLOT"), h.eventTypes().filter { it == "INFLIGHT_SKIPPED_SLOT" })
        assertTrue(h.logs.any { it.contains("CAPTURE_SKIP_CHECKPOINT_FAILED") })
        assertTrue(h.count("journal_fault") >= 1)
    }

    @Test fun c1_fullMarkerCapacity_refusal_isReportedAndRecordedInline() {
        val h = B503Harness(); val control = ManualFinisher("control", 64)
        // Fill the journal to its hard marker capacity with unrelated markers.
        var i = 0
        while (true) {
            val r = request(slot = "2026-10-13|${(i % 70) + 2}", attempt = "fill-$i")
            try { h.journal.markTerminal(r.attemptId, r.identityJson(), "fill", "cev_fill$i", "{}") } catch (_: java.io.IOException) { break }
            i++; if (i > 10_000) error("no capacity limit")
        }
        val c = coordinator(h, control)
        c.submit(request(attempt = "running"), 0L) { pythonSnapshot("2026-10-13T09:50:42+0530") }
        val queued = control.jobs.size
        c.submit(skipped, 0L) { error("never") }
        assertEquals(queued, control.jobs.size)
        assertTrue(h.logs.any { it.contains("CAPTURE_SKIP_CHECKPOINT_FAILED") && it.contains("capacity") })
        assertEquals(1, h.eventTypes().count { it == "INFLIGHT_SKIPPED_SLOT" }) // delivered straight to the ledger
    }

    @Test fun c1_exactlyOneOwner_preserved() {
        val h = B503Harness(); val control = ManualFinisher("control", 64)
        val c = coordinator(h, control)
        c.submit(request(attempt = "running"), 0L) { pythonSnapshot("2026-10-13T09:50:42+0530") }
        repeat(5) { c.submit(request(slot = "2026-10-13|${10 + it}", pollTime = "10:${String.format("%02d", it * 5)}", attempt = "s$it"), 0L) { error("never") } }
        assertTrue(c.isComputationOutstanding()); assertEquals(1, c.pendingAdmissions())
        assertEquals(6, h.journal.inventory().intents) // 1 running intent + 5 durable skip markers
    }

    // ------------------------------------------------------------------ C2
    private fun prov(origin: String, slotStart: String = "09:50", ordinal: Int = 8) = JSONObject().put("schema", "capture_provenance_v1")
        .put("capture_key", "2026-10-13|$ordinal").put("session_date", "2026-10-13").put("slot_ordinal", ordinal).put("delivery", "LATE")
        .put("attempt_id", "a$ordinal-$origin").put("slot_scheduled_ist", "2026-10-13T$slotStart:00+0530").put("origin_poll_ts", "2026-10-13T$origin+0530")
    private fun ist(hms: String) = Instant.parse("2026-10-13T${hms}Z").minusSeconds(19800)
    private fun audit(inv: List<String>, vararg snaps: Pair<String, JSONObject?>) = Pc2ParityAudit.audit(
        Pc2ParityAudit.SessionEvidence("2026-10-13", inv.map { ist(it).toString() }),
        snaps.map { (t, p) -> JSONObject().put("poll_ts_utc", ist(t).toString()).put("session_date", "2026-10-13").put("capture_provenance", p ?: JSONObject.NULL) },
        emptyList(), emptyList())
    private val GAPS = listOf("MISSING_INVENTORY_FOR_ORIGIN", "MISSING_SNAPSHOT_FOR_POLL", "SNAPSHOT_WITHOUT_INVENTORY", "AMBIGUOUS_INVENTORY_MATCH", "DUPLICATE_ORIGIN_CLAIM", "INVALID_CAPTURE_PROVENANCE")
    private fun clean(r: Pc2ParityAudit.Report) = GAPS.all { r.count(it) == 0 }

    @Test fun c2_originPrecisionBounds_at119_120_151_299() {
        assertTrue("+119", clean(audit(listOf("09:51:59"), "09:53:40" to prov("09:51:00"))))
        assertTrue("+120", clean(audit(listOf("09:52:00"), "09:53:40" to prov("09:52:00"))))
        assertTrue("+151", clean(audit(listOf("09:52:31"), "09:54:00" to prov("09:52:00"))))
        assertTrue("+299", clean(audit(listOf("09:54:59"), "09:56:10" to prov("09:54:00"))))
        // exact-seconds origin: no minute-precision term
        assertTrue("exact", clean(audit(listOf("09:52:17"), "09:54:00" to prov("09:52:12"))))
        assertFalse("exact origin, chain 40 s later is outside lag bound", clean(audit(listOf("09:52:52"), "09:54:00" to prov("09:52:12"))))
    }

    @Test fun c2_unrelatedInventory_duplicatePolls_duplicateClaims_corruptProvenance_fail() {
        // chain poll unrelated to the recorded origin (slot start, but origin says 09:52)
        val r1 = audit(listOf("09:50:04"), "09:54:00" to prov("09:52:00"))
        assertEquals(1, r1.count("MISSING_INVENTORY_FOR_ORIGIN")); assertEquals(1, r1.count("MISSING_SNAPSHOT_FOR_POLL"))
        // two chain polls inside one origin window
        assertEquals(1, audit(listOf("09:52:03", "09:52:40"), "09:54:00" to prov("09:52:00")).count("AMBIGUOUS_INVENTORY_MATCH"))
        // two snapshots claiming one origin poll
        val p2 = prov("09:52:00").put("attempt_id", "other")
        assertEquals(1, audit(listOf("09:52:03"), "09:54:00" to prov("09:52:00"), "09:54:30" to p2).count("DUPLICATE_ORIGIN_CLAIM"))
        // corrupt: origin outside its own slot
        assertEquals(1, audit(listOf("09:56:03"), "09:57:00" to prov("09:56:00")).count("INVALID_CAPTURE_PROVENANCE"))
        // never reaches into the next slot: origin 09:54, next slot polled 09:55:02 -> origin missing, next slot unaffected
        val r5 = audit(listOf("09:55:02"), "09:56:10" to prov("09:54:00"), "09:55:30" to null)
        assertEquals(1, r5.count("MISSING_INVENTORY_FOR_ORIGIN")); assertEquals(0, r5.count("AMBIGUOUS_INVENTORY_MATCH"))
    }

    @Test fun c2_preB503Fallback_nextSlotCompletion_midnight_versionCompat() {
        // pre-b503 rows keep the strict 60 s rule
        assertTrue(clean(audit(listOf("09:50:04"), "09:50:40" to null)))
        assertEquals(1, audit(listOf("09:50:04"), "09:51:40" to null).count("MISSING_SNAPSHOT_FOR_POLL"))
        // completion in the next slot, after the next poll
        assertTrue(clean(audit(listOf("09:52:05", "09:55:03"), "09:56:20" to prov("09:52:00"), "09:55:40" to null)))
        // round-2 provenance without origin_poll_ts still uses the slot-start fallback window
        val noOrigin = prov("09:50:00").put("origin_poll_ts", JSONObject.NULL)
        assertTrue(clean(audit(listOf("09:50:05"), "09:52:00" to noOrigin)))
        // midnight: > 6 h is a conservative eligibility restriction -> visible INVALID, never silently re-bucketed
        val r = Pc2ParityAudit.audit(Pc2ParityAudit.SessionEvidence("2026-10-13", listOf(ist("09:52:05").toString())),
            listOf(JSONObject().put("poll_ts_utc", Instant.parse("2026-10-13T18:31:00Z").toString()).put("session_date", "2026-10-13").put("capture_provenance", prov("09:52:00"))),
            emptyList(), emptyList())
        assertEquals(1, r.count("INVALID_CAPTURE_PROVENANCE"))
    }

    // ------------------------------------------------------------------ C3
    private fun cohortRequest(nf: Int, bnf: Int, slot: Int = 8, hm: String = "09:50", fp: Boolean = true): FrozenCaptureRequest {
        val ts = "2026-10-13T$hm:00+0530"
        return request(slot = "2026-10-13|$slot", pollTime = hm, attempt = "c$slot",
            result = if (fp) B503Harness.result(ts, nf, bnf) else B503Harness.result(null))
    }
    private fun built(h: B503Harness, r: FrozenCaptureRequest, completion: String = "2026-10-13T09:50:42+0530") =
        h.engine.build(r, CaptureDelivery.LATE, CaptureCoordinator.Computed(pythonSnapshot(completion), 1, 1), 0, 0)
    private fun manifestOf(b: CaptureEngine.BuiltChannels) = JSONObject(b.channels.getString("snapshot")).getJSONObject("poll_summary_json").getJSONObject("generated_cohort_manifest")

    @Test fun c3_captureTimeManifest_expected_empty_failed() {
        val h = B503Harness()
        val m = manifestOf(built(h, cohortRequest(3, 2)))
        assertEquals("EXPECTED", m.getString("status")); assertEquals(5, m.getInt("row_count"))
        assertEquals(3, m.getJSONObject("per_index").getInt("NF")); assertEquals(2, m.getJSONObject("per_index").getInt("BNF"))
        assertEquals("2026-10-13T09:50:00+0530", m.getString("snapshot_poll_ts"))
        assertEquals("bounded_sample_not_full_population", m.getString("population"))
        val z = manifestOf(built(h, cohortRequest(0, 0)))
        assertEquals("NOT_EXPECTED", z.getString("status")); assertEquals("zero_candidates", z.getString("reason"))
        assertEquals("no_fact_pack", manifestOf(built(h, cohortRequest(0, 0, fp = false))).getString("reason"))
        // a present but unreadable frozen fact pack is a FAILURE, never "no fact pack"/zero
        val ok = cohortRequest(3, 2)
        val broken = FrozenCaptureRequest(ok.captureKey, ok.sessionDate, ok.slotOrdinal, ok.originPollTs, ok.slotScheduledIst, ok.attemptId,
            ok.runId, ok.appVersion, ok.versionCode, ok.brainVersion, ok.requestedMonoMs, ok.requestedWallMs, ok.resultJson, ok.ctxJson,
            ok.pollsJson, "{broken", ok.persistenceMode, ok.freezeMs)
        val bf = built(h, broken)
        assertEquals("BUILD_FAILED", manifestOf(bf).getString("status")); assertEquals("BUILD_FAILED", bf.states["generated"])
    }

    private fun exportShape(b: CaptureEngine.BuiltChannels, pollUtc: String): Pair<JSONObject, List<JSONObject>> {
        val snap = JSONObject(b.channels.getString("snapshot"))
        val s = JSONObject().put("poll_ts_utc", pollUtc).put("session_date", snap.getString("session_date"))
            .put("recommendation_id", snap.opt("recommendation_id"))
            .put("capture_provenance", snap.getJSONObject("poll_summary_json").getJSONObject("capture_provenance"))
            .put("generated_cohort_manifest", snap.getJSONObject("poll_summary_json").getJSONObject("generated_cohort_manifest"))
        val rows = b.channels.optString("generated").takeIf { it.isNotEmpty() }?.let { JSONObject(it).getJSONArray("rows") } ?: JSONArray()
        val gen = (0 until rows.length()).map { i -> JSONObject(rows.getJSONObject(i).toString()).also {
            it.put("snapshot_poll_ts_utc", Instant.ofEpochSecond(Pc2ParityAudit.parseInstant(it.getString("snapshot_poll_ts"))!!.epochSecond).toString()) } }
        return s to gen
    }

    @Test fun c3_auditSemantics_intact_missing_conflict_orphan_wrongIndex() {
        val h = B503Harness()
        val (s, g) = exportShape(built(h, cohortRequest(3, 2)), "2026-10-13T04:20:42Z")
        assertTrue(CaptureCohortAudit.audit("2026-10-13", listOf(s), g).toJson().toString(), CaptureCohortAudit.audit("2026-10-13", listOf(s), g).verified)
        assertEquals(1, CaptureCohortAudit.audit("2026-10-13", listOf(s), emptyList()).count("GENERATED_COHORT_ABSENT"))
        assertEquals(1, CaptureCohortAudit.audit("2026-10-13", listOf(s), g.drop(1)).count("COHORT_ROWS_MISSING"))
        val changed = g.map { JSONObject(it.toString()) }; changed[0].put("strategy_type", "IRON_FLY")
        assertEquals(1, CaptureCohortAudit.audit("2026-10-13", listOf(s), changed).count("COHORT_CONTENT_CONFLICT"))
        val idx = g.map { JSONObject(it.toString()) }; idx[0].put("index_key", if (idx[0].getString("index_key") == "NF") "BNF" else "NF")
        assertEquals(1, CaptureCohortAudit.audit("2026-10-13", listOf(s), idx).count("COHORT_WRONG_INDEX"))
        val orphan = g + JSONObject(g[0].toString()).put("snapshot_poll_ts_utc", "2026-10-13T04:40:00Z")
        assertEquals(1, CaptureCohortAudit.audit("2026-10-13", listOf(s), orphan).count("ORPHAN_GENERATED_ROWS"))
        val pre = JSONObject().put("poll_ts_utc", "2026-10-13T04:25:30Z").put("session_date", "2026-10-13").put("capture_provenance", JSONObject.NULL)
        assertEquals(1, CaptureCohortAudit.audit("2026-10-13", listOf(s, pre), g).count("COHORT_UNVERIFIABLE_PRE_B503"))
        val r = CaptureReadiness.report(Pc2ParityAudit.audit(null, emptyList(), emptyList(), emptyList()), CaptureCohortAudit.audit("2026-10-13", listOf(s), g))
        assertEquals("CLOSED", r.getString("c3_gate")); assertEquals("CLOSED", r.getString("source_cap_gate")); assertFalse(r.getBoolean("full_population_verified"))
    }
}

/**
 * Writes SQL for tools/pc2_db_suite/run.sh from the REAL capture engine (b503 C3): snapshots with their frozen
 * capture provenance + cohort manifest, and the generated rows exactly as the generated channel uploads them.
 * No-op unless B503_COHORT_FIXTURE_OUT is set.
 */
class B503CohortFixtureTest {
    private fun d(tag: String, t: String) = "\$$tag\$$t\$$tag\$"

    @Test fun writeWhenRequested() {
        val out = System.getenv("B503_COHORT_FIXTURE_OUT")?.takeIf { it.isNotBlank() } ?: return
        val day = "2026-10-07"
        val h = B503Harness()
        // (slot, slot start, NF, BNF, completion IST)  slot 2 late, slot 3 rolls over past slot 4's poll, slot 4 zero candidates
        val plan = listOf(Triple(1, "09:15", 3 to 2) to "09:15:20", Triple(2, "09:20", 2 to 0) to "09:22:40",
            Triple(3, "09:25", 0 to 2) to "09:31:10", Triple(4, "09:30", 0 to 0) to "09:30:30")
        val sql = StringBuilder()
        for ((p, completion) in plan) {
            val (slot, hm, counts) = p
            val fp = B503Harness.richFactPack("${day}T$hm:00+0530", counts.first, counts.second).put("session_date", day)
            val r = FrozenCaptureRequest.freeze("$day|$slot", day, hm, JSONObject().put("elephant_fact_pack", fp),
                JSONObject().put("today_ist", day), "[]", "2.6.72", 503, "2.6.72", "run-F", attemptId = "fx-$slot")
            val py = JSONObject(pythonSnapshot("${day}T$completion+0530")).put("session_date", day).toString()
            val b = h.engine.build(r, if (slot == 1) CaptureDelivery.ON_TIME else CaptureDelivery.LATE, CaptureCoordinator.Computed(py, 1, 1), 0, 0)
            sql.append("insert into public.ml_brain_snapshots (poll_ts, session_date, recommendation_id, context_json, poll_summary_json) ")
                .append("select (p->>'poll_ts')::timestamptz, (p->>'session_date')::date, p->>'recommendation_id', '{}'::jsonb, p->'poll_summary_json' from (select ")
                .append(d("s", b.channels.getString("snapshot"))).append("::jsonb p) x;\n")
            b.channels.optString("generated").takeIf { it.isNotEmpty() }?.let {
                val cols = GeneratedCohort.COLUMNS.joinToString(", ") { c -> c.first }
                // json (not jsonb), as PostgREST's bulk insert does: every covered column, typed by the table.
                sql.append("insert into public.ml_generated_candidates ($cols) select $cols ")
                    .append("from json_populate_recordset(null::public.ml_generated_candidates, ").append(d("g", JSONObject(it).getJSONArray("rows").toString())).append("::json);\n")
            }
        }
        File(out).also { it.parentFile?.mkdirs() }.writeText(sql.toString())
    }
}
