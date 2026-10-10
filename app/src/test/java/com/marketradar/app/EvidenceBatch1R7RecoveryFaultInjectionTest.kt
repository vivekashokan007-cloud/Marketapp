package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

/**
 * R2-review Blocker B: fault injection at the exact storage boundaries of capture enqueue / state commit, driven
 * through PaperMenuRecovery (the sequence MarketWatchService runs) with a crashing file layer and preference store.
 */
class EvidenceBatch1R7RecoveryFaultInjectionTest {
    @get:Rule val tmp = TemporaryFolder()
    @After fun restoreIo() { PaperMenuOutbox.fileOps = PaperMenuOutbox.RealFileOps }

    private val thu = "2026-10-08"; private val fri = "2026-10-09"; private val mon = "2026-10-12"
    private val friId = PaperMenuCapture.menuIdFor(fri); private val thuId = PaperMenuCapture.menuIdFor(thu)

    /**
     * Simulated process: once [die] triggers, NOTHING more reaches storage (files or preferences) until restart.
     * [failOnly] makes matching writes fail without killing the process (a plain storage error).
     */
    class Process(val prefs: MutableMap<String, String> = mutableMapOf()) {
        @Volatile var dead = false
        var die: (File) -> Boolean = { false }
        var failOnly: (File) -> Boolean = { false }
        var preferenceCommitDies = false
        var preferenceCommitFails = false
        val io = object : PaperMenuOutbox.FileOps {
            override fun atomicWrite(target: File, text: String): Boolean {
                if (dead) return false
                if (die(target)) { dead = true; return false }
                if (failOnly(target)) return false
                return PaperMenuOutbox.RealFileOps.atomicWrite(target, text)
            }
            override fun rename(from: File, to: File): Boolean = !dead && PaperMenuOutbox.RealFileOps.rename(from, to)
        }
        fun save(st: String): Boolean {
            if (dead) return false
            if (preferenceCommitDies) { dead = true; return false }
            if (preferenceCommitFails) return false
            prefs["state"] = st; return true
        }
        fun install() { PaperMenuOutbox.fileOps = io }
        /** Restart: same disk + committed preferences, healthy process. */
        fun restart() = Process(prefs).also { it.install() }
    }

    private fun capturedPayload(date: String, n: Int = 3, tag: String = "v1"): JSONObject {
        val id = PaperMenuCapture.menuIdFor(date)
        val c = JSONArray(); for (i in 1..n) c.put(JSONObject().put("menu_id", id).put("candidate_id", "c$i").put("generated_rank", i).put("tag", tag))
        return JSONObject().put("manifest", JSONObject().put("menu_id", id).put("session_date", date)
            .put("capture_policy_version", PaperMenuCapture.POLICY_VERSION).put("population_status", "MENU_BUILT")
            .put("expected_candidate_rows", n)).put("candidates", c)
    }

    private fun poll(p: Process, dir: File, date: String, ordinal: Int?, iso: String, ms: Long) =
        PaperMenuRecovery.preparePoll(dir, "paper", p.prefs["state"], date, ordinal?.let { "$date|$it" }, ordinal, iso, ms) { p.save(it) }

    private fun capture(p: Process, dir: File, date: String, ordinal: Int, payload: JSONObject, ms: Long) =
        PaperMenuRecovery.commitCapture(dir, PaperMenuCapture.stateFor(p.prefs["state"], date), "$date|$ordinal", payload,
            "${date}T12:${30 + 5 * (ordinal - 40)}:10+0530", ms) { st -> p.save(st.toString()) }

    private class Server {
        val received = mutableListOf<Triple<String, String, String>>()
        fun upload(e: PaperMenuOutbox.Entry, text: String): PaperMenuOutbox.Delivery {
            received.add(Triple(e.menuId, e.kind, text)); return PaperMenuOutbox.Delivery("ACKNOWLEDGED", "OK")
        }
    }

    /** Fri 12:30 poll + successful brain menu; the process dies after the payload rename, before the state write. */
    private fun dieAfterPayloadRenameBeforeStateWrite(dir: File): Pair<Process, String> {
        val p = Process().also { it.install() }
        val prep = poll(p, dir, fri, 40, "2026-10-09T12:30:05+0530", 1)
        assertNotNull(prep.prepared.request)
        assertEquals("OPEN", JSONObject(p.prefs["state"]!!).getString("status"))
        p.die = { it.name.endsWith(".outbox.json") }
        val payload = capturedPayload(fri)
        val r = capture(p, dir, fri, 40, payload, 2)
        assertTrue(p.dead)
        assertEquals("FAILED", r.status)                       // nothing more was persisted after death
        val bytes = File(dir, "$friId.json").readText()        // the rename completed: these are the frozen bytes
        assertFalse(File(dir, "$friId.outbox.json").exists())
        assertEquals("OPEN", JSONObject(p.prefs["state"]!!).getString("status"))
        return p to bytes
    }

    @Test fun stopAfterCapturedRenameBeforeStateWrite_restartAt1300_uploadsOriginalCapturedNoMissed() {
        val dir = tmp.newFolder()
        val (dead, bytes) = dieAfterPayloadRenameBeforeStateWrite(dir)
        val p = dead.restart()
        // 13:00 = slot 46, after the window: before R2-review this produced MISSED_SESSION.
        val prep = poll(p, dir, fri, 46, "2026-10-09T13:00:04+0530", 3)
        assertTrue(prep.prepared.missedToEnqueue.isEmpty())
        assertNull(prep.prepared.request)
        assertTrue(prep.reconcile!!.recovered.contains("$friId:CAPTURED:missing_state"))
        assertEquals("CAPTURED", JSONObject(p.prefs["state"]!!).getString("status"))
        assertEquals(PaperMenuCapture.sha256Hex(bytes), JSONObject(p.prefs["state"]!!).getString("payload_sha256"))
        assertTrue(prep.events.none { it.second.startsWith("PAPER_MENU_MISSED_SESSION") })
        // Next session too.
        val prepMon = poll(p, dir, mon, null, "2026-10-12T09:16:00+0530", 4)
        assertTrue(prepMon.prepared.missedToEnqueue.isEmpty())
        assertFalse(File(dir, "$friId.missed.json").exists())
        val s = Server(); val d = PaperMenuOutbox.drain(dir, mon, 5, s::upload)
        assertEquals(listOf(Triple(friId, PaperMenuOutbox.KIND_CAPTURED, bytes)), s.received)
        assertEquals(1, d.acknowledged)
    }

    @Test fun stopAfterCapturedRenameBeforeStateWrite_restartNextSession_uploadsOriginalCapturedNoMissed() {
        val dir = tmp.newFolder()
        val (dead, bytes) = dieAfterPayloadRenameBeforeStateWrite(dir)
        val p = dead.restart()
        val prep = poll(p, dir, mon, 40, "2026-10-12T12:30:05+0530", 3)  // first poll after restart is Monday
        assertTrue("Friday must not become MISSED", prep.prepared.missedToEnqueue.isEmpty())
        assertEquals(mon, JSONObject(p.prefs["state"]!!).getString("session_date"))
        assertNotNull(prep.prepared.request)                               // Monday's own capture proceeds normally
        val s = Server(); PaperMenuOutbox.drain(dir, mon, 4, s::upload)
        assertEquals(listOf(Triple(friId, PaperMenuOutbox.KIND_CAPTURED, bytes)), s.received)
        assertFalse(File(dir, "$friId.missed.json").exists())
    }

    @Test fun drainAloneAlsoRecoversTheOrphanCapture() {
        val dir = tmp.newFolder()
        val (dead, bytes) = dieAfterPayloadRenameBeforeStateWrite(dir)
        dead.restart()
        val s = Server(); val d = PaperMenuOutbox.drain(dir, fri, 3, s::upload)  // e.g. a live-mode poll's recovery upload
        assertEquals(listOf(bytes), s.received.map { it.third })
        assertEquals(1, d.acknowledged)
        assertTrue(d.reconcile.recovered.contains("$friId:CAPTURED:missing_state"))
    }

    @Test fun stopAfterOutboxWriteBeforePreferenceCommit_oneFrozenCaptureNoRecapture() {
        val dir = tmp.newFolder()
        val p0 = Process().also { it.install() }
        poll(p0, dir, fri, 40, "2026-10-09T12:30:05+0530", 1)
        p0.preferenceCommitDies = true
        val first = capturedPayload(fri, tag = "first")
        val r = capture(p0, dir, fri, 40, first, 2)
        assertFalse(r.stateSaved)
        assertTrue(r.events.any { it.first == 'E' && it.second.startsWith("PAPER_MENU_STORAGE_ERROR") })
        val bytes = File(dir, "$friId.json").readText()
        assertEquals(PaperMenuOutbox.NOT_STARTED, PaperMenuOutbox.readState(dir, friId)!!.getString("state"))
        assertEquals("OPEN", JSONObject(p0.prefs["state"]!!).getString("status"))
        val p = p0.restart()
        // 12:35, still inside the window: no new capture request.
        val prep = poll(p, dir, fri, 41, "2026-10-09T12:35:05+0530", 3)
        assertNull(prep.prepared.request)
        assertTrue(prep.events.any { it.second.startsWith("PAPER_MENU_STATE_RECONCILED") })
        assertEquals("CAPTURED", JSONObject(p.prefs["state"]!!).getString("status"))
        // Even a capture racing with a stale OPEN state keeps the first frozen bytes.
        val stale = PaperMenuCapture.newState(fri).toString()
        val racer = PaperMenuRecovery.commitCapture(dir, JSONObject(stale), "$fri|41", capturedPayload(fri, tag = "second"),
            "2026-10-09T12:35:10+0530", 4) { st -> p.save(st.toString()) }
        assertEquals("ALREADY_FROZEN_RECONCILED", racer.status)
        assertEquals(PaperMenuCapture.sha256Hex(bytes), JSONObject(p.prefs["state"]!!).getString("payload_sha256"))
        assertEquals(bytes, File(dir, "$friId.json").readText())
        assertEquals(1, dir.list()!!.count { it.endsWith(".json") && !it.endsWith(".outbox.json") })
        val s = Server(); PaperMenuOutbox.drain(dir, fri, 5, s::upload)
        assertEquals(listOf(bytes), s.received.map { it.third })
    }

    @Test fun missingStateJsonIsRecoveredFromOriginalBytes() {
        val dir = tmp.newFolder()
        val bytes = capturedPayload(fri).toString()
        PaperMenuOutbox.enqueueCaptured(dir, friId, fri, bytes, 0)!!
        assertTrue(File(dir, "$friId.outbox.json").delete())
        val rep = PaperMenuOutbox.reconcile(dir, 10)
        assertEquals(listOf("$friId:CAPTURED:missing_state"), rep.recovered)
        val st = PaperMenuOutbox.readState(dir, friId)!!
        assertEquals(PaperMenuCapture.sha256Hex(bytes), st.getString("payload_sha256"))
        assertEquals("missing_state", st.getJSONObject("recovered").getString("reason"))
        assertEquals(bytes, File(dir, "$friId.json").readText())
    }

    @Test fun corruptStateJsonIsRetainedAsideAndRebuiltVisibly() {
        val dir = tmp.newFolder()
        val bytes = capturedPayload(fri).toString()
        PaperMenuOutbox.enqueueCaptured(dir, friId, fri, bytes, 0)!!
        File(dir, "$friId.outbox.json").writeText("{\"menu_id\":\"$friId\",\"sta")   // truncated
        // Before reconcile the corruption is visible, not silently absent.
        assertTrue(PaperMenuOutbox.summary(dir).getJSONArray("faults").toString().contains("$friId.outbox.json:unusable_state_file"))
        val s = Server(); val d = PaperMenuOutbox.drain(dir, fri, 10, s::upload)
        assertEquals(listOf(bytes), s.received.map { it.third })
        assertEquals(1, d.acknowledged)
        val aside = dir.list()!!.single { it.startsWith("$friId.outbox.corrupt.") }
        assertEquals("{\"menu_id\":\"$friId\",\"sta", File(dir, aside).readText())     // retained for diagnosis
        assertTrue(d.reconcile.faults.any { it.contains("corrupt_state_file_retained_as:$aside") })
        assertTrue(PaperMenuOutbox.summary(dir).getJSONArray("faults").toString().contains("$aside:corrupt_state_file_retained"))
        assertEquals("corrupt_state", PaperMenuOutbox.readState(dir, friId)!!.getJSONObject("recovered").getString("reason"))
    }

    @Test fun corruptStateAndCorruptPayloadIsAnExplicitRetainedFaultNeverMissed() {
        val dir = tmp.newFolder()
        PaperMenuOutbox.enqueueCaptured(dir, friId, fri, capturedPayload(fri).toString(), 0)!!
        File(dir, "$friId.outbox.json").writeText("garbage")
        File(dir, "$friId.json").writeText("{\"manifest\":{\"menu_id\":\"pdm_000000000000000000000000\"")
        val s = Server(); PaperMenuOutbox.drain(dir, fri, 10, s::upload)
        assertTrue(s.received.isEmpty())
        val st = PaperMenuOutbox.readState(dir, friId)!!
        assertEquals(PaperMenuOutbox.LOCAL_CORRUPT_RETAINED, st.getString("state"))
        assertEquals("CAPTURED_payload_not_json", st.getString("last_outcome"))
        assertTrue(PaperMenuOutbox.summary(dir).getJSONArray("faults").toString().contains("$friId:LOCAL_CORRUPT_RETAINED"))
        // A later MISSED for the same session never replaces or hides it.
        val p = Process().also { it.install() }
        val prep = poll(p, dir, fri, 46, "2026-10-09T13:00:00+0530", 11)
        assertFalse(File(dir, "$friId.missed.json").exists())
        assertTrue(prep.events.any { it.second.startsWith("PAPER_MENU_MISSED_SUPPRESSED") })
        assertTrue(prep.events.none { it.second.startsWith("PAPER_MENU_MISSED_SESSION") })
        assertTrue(PaperMenuOutbox.prune(dir, 400L * 86_400_000, 30L * 86_400_000).isEmpty())
        assertTrue(File(dir, "$friId.json").exists())
    }

    @Test fun interruptedMissedEnqueueRecoversIdenticalBytes() {
        val dir = tmp.newFolder()
        val p0 = Process().also { it.install() }
        val thuState = PaperMenuCapture.newState(thu)
        PaperMenuCapture.recordAttempt(thuState, "$thu|40", "FAILED", "brain_analyze_timeout", "t")
        p0.prefs["state"] = thuState.toString()
        p0.die = { it.name.endsWith(".outbox.json") }
        val prep0 = poll(p0, dir, fri, null, "2026-10-09T09:16:00+0530", 1)
        assertTrue(p0.dead)
        assertEquals(false, prep0.stateSaved)
        val bytes = File(dir, "$thuId.missed.json").readText()
        val mtime = File(dir, "$thuId.missed.json").lastModified()
        assertEquals(thu, JSONObject(p0.prefs["state"]!!).getString("session_date"))       // not advanced
        val p = p0.restart()
        val prep = poll(p, dir, fri, null, "2026-10-09T09:21:00+0530", 2)                // re-derives with a new missed_at
        assertTrue(prep.reconcile!!.recovered.contains("$thuId:MISSED:missing_state"))
        assertEquals(true, prep.stateSaved)
        assertEquals(bytes, File(dir, "$thuId.missed.json").readText())
        assertEquals(mtime, File(dir, "$thuId.missed.json").lastModified())   // never rewritten
        val s = Server(); PaperMenuOutbox.drain(dir, fri, 3, s::upload)
        assertEquals(listOf(Triple(thuId, PaperMenuOutbox.KIND_MISSED, bytes)), s.received)
        assertEquals(PaperMenuCapture.sha256Hex(bytes), PaperMenuOutbox.readState(dir, thuId)!!.getString("payload_sha256"))
        assertEquals("MISSED_SESSION", JSONObject(bytes).getString("capture_status"))
    }

    @Test fun metadataWriteFailureIsObservableAndEvidenceRetained() {
        val dir = tmp.newFolder()
        // (1) State write fails at capture: observable error; frozen bytes retained and recovered on the next poll.
        val p = Process().also { it.install() }
        poll(p, dir, fri, 40, "2026-10-09T12:30:05+0530", 1)
        p.failOnly = { it.name.endsWith(".outbox.json") }
        val r = capture(p, dir, fri, 40, capturedPayload(fri), 2)
        assertEquals("FAILED", r.status)
        assertTrue(r.events.any { it.first == 'E' && it.second.contains("frozen_file_write_failed") })
        val bytes = File(dir, "$friId.json").readText()
        p.failOnly = { false }
        val prep = poll(p, dir, fri, 41, "2026-10-09T12:35:05+0530", 3)
        assertNull(prep.prepared.request)
        assertEquals("CAPTURED", JSONObject(p.prefs["state"]!!).getString("status"))
        // (2) Delivery state write fails after the server acknowledged: not counted as acked; reported; re-sent later.
        p.failOnly = { it.name.endsWith(".outbox.json") }
        val s = Server()
        val d1 = PaperMenuOutbox.drain(dir, fri, 4, s::upload)
        assertEquals(0, d1.acknowledged)
        assertTrue(d1.storageErrors.single().startsWith("$friId:state_write_failed:after_delivery:ACKNOWLEDGED"))
        assertEquals(PaperMenuOutbox.NOT_STARTED, PaperMenuOutbox.readState(dir, friId)!!.getString("state"))
        assertTrue(File(dir, "$friId.json").exists())
        p.failOnly = { false }
        val d2 = PaperMenuOutbox.drain(dir, fri, 5, s::upload)
        assertEquals(1, d2.acknowledged)
        assertEquals(listOf(bytes, bytes), s.received.map { it.third })
        // (3) Preference commit failure is reported, not silent.
        p.preferenceCommitFails = true
        val prep3 = poll(p, dir, mon, null, "2026-10-12T09:16:00+0530", 6)
        assertEquals(false, prep3.stateSaved)
        assertTrue(prep3.events.any { it.first == 'E' && it.second.startsWith("PAPER_MENU_STORAGE_ERROR: capture_state_commit_failed") })
    }

    @Test fun r1EraMissedEntryBesideAValidCaptureIsAmbiguousRetainedNotSent() {
        val dir = tmp.newFolder()
        val cap = capturedPayload(fri).toString()
        File(dir, "$friId.json").writeText(cap)
        val missed = PaperMenuCapture.missedManifest(PaperMenuCapture.newState(fri), "x", "t")
        File(dir, "$friId.missed.json").writeText(missed.toString())
        File(dir, "$friId.outbox.json").writeText(JSONObject().put("schema", PaperMenuOutbox.SCHEMA).put("menu_id", friId)
            .put("kind", "MISSED").put("session_date", fri).put("payload_file", "$friId.missed.json")
            .put("payload_sha256", PaperMenuCapture.sha256Hex(missed.toString())).put("state", "NOT_STARTED").toString())
        val s = Server(); PaperMenuOutbox.drain(dir, fri, 1, s::upload)
        assertTrue(s.received.isEmpty())
        assertEquals(PaperMenuOutbox.AMBIGUOUS_RETAINED, PaperMenuOutbox.readState(dir, friId)!!.getString("state"))
        assertTrue(PaperMenuOutbox.summary(dir).getJSONArray("faults").toString().contains("AMBIGUOUS_RETAINED"))
    }

    @Test fun reconcileIsIdempotentAndNeverRewritesFrozenPayloads() {
        val dir = tmp.newFolder()
        val bytes = capturedPayload(fri).toString()
        File(dir, "$friId.json").writeText(bytes)
        val t0 = File(dir, "$friId.json").lastModified()
        PaperMenuOutbox.reconcile(dir, 1)
        val st1 = File(dir, "$friId.outbox.json").readText()
        assertTrue(PaperMenuOutbox.reconcile(dir, 2).recovered.isEmpty())
        assertEquals(st1, File(dir, "$friId.outbox.json").readText())
        assertEquals(bytes, File(dir, "$friId.json").readText())
        assertEquals(t0, File(dir, "$friId.json").lastModified())
    }

    @Test fun uncommittedTempFileAndUnboundPayloadStayVisible() {
        val dir = tmp.newFolder()
        File(dir, "$friId.json.tmp").writeText("partial")
        val other = PaperMenuCapture.menuIdFor(thu)
        File(dir, "$other.json").writeText(capturedPayload(fri).toString())  // bytes of another session under this id
        PaperMenuOutbox.reconcile(dir, 1)
        val faults = PaperMenuOutbox.summary(dir).getJSONArray("faults").toString()
        assertTrue(faults, faults.contains("$friId.json.tmp:uncommitted_temp_file_retained"))
        assertEquals("CAPTURED_manifest_menu_id_differs_from_file", PaperMenuOutbox.readState(dir, other)!!.getString("last_outcome"))
        assertTrue(File(dir, "$friId.json.tmp").exists())
    }
}
