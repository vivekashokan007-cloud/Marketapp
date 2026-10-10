package com.marketradar.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/** Evidence batch 1: source-contract checks for the service wiring (Android-free). */
class EvidenceBatch1WiringContractTest {
    private fun source(name: String): String = listOf(
        File("src/main/java/com/marketradar/app/$name"),
        File("app/src/main/java/com/marketradar/app/$name")
    ).first { it.isFile }.readText()

    private val svc by lazy { source("MarketWatchService.kt") }

    @Test
    fun spotProvenanceIsBuiltFromTheQuotesFetchAndPassedBesideUnchangedSpot() {
        val prov = svc.indexOf("SpotProvenance.build(")
        val spot = svc.indexOf("val bnfSpot = data.getJSONObject(\"NSE_INDEX:Nifty Bank\").getDouble(\"last_price\")")
        assertTrue(prov in 1 until spot)
        assertTrue(svc.contains("quotesFetchStartedAtMs = System.currentTimeMillis()"))
        assertTrue(svc.contains("ctxObj.put(\"spot_provenance\", spotProvenance)"))
        // Spot values themselves are unchanged.
        assertTrue(svc.contains("ctxObj.put(\"bnfSpot\",    bnfSpot)"))
    }

    @Test
    fun menuIsRequestedOnlyThroughTheCaptureStateMachineAndDetachedFromTheResult() {
        val req = svc.indexOf("preparePaperMenuCaptureRequest(poll.optString(\"slot_key\"")
        val analyze = svc.indexOf("brain.callAttr(\"analyze\",")
        assertTrue(req in 1 until analyze)
        val detach = svc.indexOf("resultObj.remove(\"paper_decision_menu\")")
        val firstSnapshot = svc.indexOf("\"take_poll_snapshot\",")
        val notify = svc.indexOf("processUnifiedBrainNotifications(brain, resultObj, ctxObj)")
        assertTrue("menu must be detached before notifications and snapshots", detach in 1 until minOf(notify, firstSnapshot))
        assertEquals(1, Regex(Regex.escape("ctxObj.put(\"paper_menu_capture_request\"")).findAll(svc).count())
        assertTrue(svc.indexOf("ctxObj.remove(\"paper_menu_capture_request\")") in 1 until req)
    }

    @Test
    fun capturedMarginUsesTheExistingEnrichmentFirstAndCapsExtraRequests() {
        val enrich = svc.indexOf("enrichTopCandidateMargin(resultObj, ctxObj, authToken, marginPollKey)")
        val capture = svc.indexOf("capturePaperDecisionMenu(slot, paperDecisionMenu")
        assertTrue(capture > enrich)
        assertTrue(svc.contains("PAPER_MENU_EXTRA_MARGIN_REQUESTS = 2"))
        val fn = svc.substring(svc.indexOf("private fun collectPaperMenuRoleMargins"), svc.indexOf("private fun capturePaperDecisionMenu"))
        assertTrue(fn.contains("role != \"PRIMARY\" && role != \"REGISTERED_UNIFORM_PEER\""))
        assertTrue(fn.contains("extra >= PAPER_MENU_EXTRA_MARGIN_REQUESTS"))
        assertFalse("menu margin must not feed ranking/sizing keys", fn.contains("marginForSizing"))
    }

    @Test
    fun failuresAreLoggedAsAttemptsAndUploadsReuseTheFrozenFile() {
        assertTrue(svc.contains("recordPaperMenuAttemptFailure(it, \"brain_analyze_timeout\")"))
        assertTrue(svc.contains("recordPaperMenuAttemptFailure(it, \"brain_exception:"))
        val up = svc.substring(svc.indexOf("private fun launchPaperMenuUploadIfPending"))
        // R2: uploads drain the durable per-menu outbox (frozen-bytes sha check lives in PaperMenuOutbox.drain).
        assertTrue(up.contains("PaperMenuOutbox.drain(dir, todayIstDate()"))
        // R2-review Blocker B: capture freeze + checked state commit go through PaperMenuRecovery.commitCapture.
        assertTrue(svc.contains("PaperMenuRecovery.commitCapture(paperMenuDir()"))
        assertTrue(source("PaperMenuRecovery.kt").contains("PaperMenuOutbox.enqueueCaptured(dir, menuId"))
        assertTrue(svc.contains("PaperMenuOutbox.prune(paperMenuDir()"))
        assertTrue(source("PaperMenuOutbox.kt").contains("PaperMenuCapture.sha256Hex(text) != e.payloadSha256"))
        assertTrue(up.contains("SupabaseClient.savePaperDecisionMenu("))
        val sc = source("SupabaseClient.kt")
        val fn = sc.substring(sc.indexOf("fun savePaperDecisionMenu"), sc.indexOf("fun paperMenuCollectorActive"))
        // R4: writes only through the authorized RPCs, with the device credential, then readback.
        assertTrue(fn.contains("postRpc(\"paper_decision_menu_ingest_manifest\""))
        assertTrue(fn.contains("postRpc(\"paper_decision_menu_ingest_candidates\""))
        assertTrue(fn.contains("postRpc(\"paper_decision_menu_finalize\""))
        assertTrue(fn.contains(".put(\"p_device_key\", deviceKey)"))
        assertTrue(fn.contains("PaperMenuCapture.readbackDifference(canonicalRowsInRankOrder, arr)"))
        assertFalse("no direct table INSERT any more", fn.contains("postArrayToTableDetailed(\"ml_paper_decision_menu"))
    }

    @Test
    fun r2ReviewBlockerB_reconcileRunsBeforeRolloverAndBeforeDrain() {
        assertTrue(svc.contains("PaperMenuRecovery.preparePoll("))
        assertFalse("the service no longer runs its own prepare/enqueue sequence", svc.contains("PaperMenuCapture.prepare("))
        val rec = source("PaperMenuRecovery.kt")
        val fn = rec.substring(rec.indexOf("fun preparePoll"), rec.indexOf("data class CommitResult"))
        val reconcile = fn.indexOf("PaperMenuOutbox.reconcile(dir, nowMs)")
        val prepare = fn.indexOf("PaperMenuCapture.prepare(populationMode, rawState, sessionDate, slotKey, slotOrdinal, nowIso, frozenLookup(dir))")
        val enqueue = fn.indexOf("PaperMenuOutbox.enqueueMissed(dir, m, nowMs)")
        val save = fn.indexOf("saveState(p.stateToSave)")
        assertTrue(reconcile in 1 until prepare); assertTrue(prepare < enqueue); assertTrue(enqueue < save)
        val ob = source("PaperMenuOutbox.kt")
        val drain = ob.substring(ob.indexOf("fun drain("))
        assertTrue(drain.trimStart().contains("val rec = reconcile(dir, nowMs)"))
        assertTrue(drain.indexOf("val rec = reconcile(dir, nowMs)") < drain.indexOf("for (e in entries(dir))"))
        assertTrue(svc.contains("drain.storageErrors.forEach"))
    }
}
