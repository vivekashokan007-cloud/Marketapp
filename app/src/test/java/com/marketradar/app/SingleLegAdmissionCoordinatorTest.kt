package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference

/** Codex R7 s2: paused-transport integration tests for the native single-leg admission path. */
class SingleLegAdmissionCoordinatorTest {
    private fun state(
        decisionId: String? = "idx1_a", key: String? = "NSE_FO|1", enabled: Boolean = true, paper: Boolean = true,
        selector: String = "BOTH", openTrades: String = "[]", capital: Double? = 250000.0, rev: Long = 1,
    ) = SingleLegAdmissionCoordinator.State("{}", "NF", decisionId, key, "BUY_CE", 65, listOf("NSE_FO|1"),
        enabled, paper, selector, openTrades, capital, 10.0, rev)

    private class Rig(initial: SingleLegAdmissionCoordinator.State) {
        val st = AtomicReference(initial)
        val entered = CountDownLatch(1)
        val release = CountDownLatch(1)
        val fetches = AtomicInteger(0)
        val admits = AtomicInteger(0)
        val lastProbe = AtomicReference<String?>(null)
        var admitResult: (SingleLegAdmissionCoordinator.Call) -> String? = { c ->
            lastProbe.set(c.probe.current())
            """{"status":"ADMITTED","position_id":"sl1_x","request_id":"${c.requestId}"}"""
        }
        var reconcileResult: (String) -> String? = { null }
        val wall = AtomicLong(1_000_000L)
        val transport = ManagedReadAdapters.Transport { _, _, _ ->
            fetches.incrementAndGet(); entered.countDown(); release.await(5, TimeUnit.SECONDS)
            ManagedReadAdapters.Response(200, """{"status":"success","data":{}}""")
        }
        val coord = SingleLegAdmissionCoordinator(
            readState = { st.get() }, transport = { transport },
            clocks = ManagedReadAdapters.Clocks({ wall.get() }, { wall.get() }),
            admit = { c -> admits.incrementAndGet(); admitResult(c) },
            reconcile = { reconcileResult(it) },
            executor = Executors.newSingleThreadExecutor(),
            sleeper = { wall.addAndGet(it * 20) })

        fun await(id: String): JSONObject {
            val end = System.currentTimeMillis() + 5000
            while (System.currentTimeMillis() < end) {
                val j = JSONObject(coord.status(id))
                if (j.optBoolean("completed")) return j
                Thread.sleep(5)
            }
            throw AssertionError("not completed")
        }
    }

    private fun changedDuringFetch(change: (SingleLegAdmissionCoordinator.State) -> SingleLegAdmissionCoordinator.State,
                                   field: String) {
        val rig = Rig(state())
        val p = JSONObject(rig.coord.submit("NF", "r1"))
        assertEquals("PENDING", p.getString("status"))               // non-blocking: returns before the fetch ends
        assertTrue(rig.entered.await(5, TimeUnit.SECONDS))
        assertEquals("PENDING", JSONObject(rig.coord.status("r1")).getString("status"))
        rig.st.set(change(rig.st.get()))                              // change while the transport is paused
        rig.release.countDown()
        val out = rig.await("r1")
        assertEquals("REJECTED", out.getString("status"))
        assertEquals("STATE_CHANGED_DURING_FETCH:$field", out.getString("reason"))
        assertEquals(0, rig.admits.get())                             // the old proposal never reaches commit
    }

    @Test fun toggleOffDuringFetch() = changedDuringFetch({ it.copy(enabled = false) }, "enabled")
    @Test fun modeChangedDuringFetch() = changedDuringFetch({ it.copy(paper = false) }, "mode")
    @Test fun selectorChangedDuringFetch() = changedDuringFetch({ it.copy(selector = "BNF") }, "selector")
    @Test fun decisionChangedDuringFetch() = changedDuringFetch({ it.copy(decisionId = "idx1_b") }, "decision_id")
    @Test fun capitalChangedDuringFetch() = changedDuringFetch({ it.copy(capital = 1000.0, configRevision = 2) }, "capital")
    @Test fun openManagedExposureDuringFetch() =
        changedDuringFetch({ it.copy(openTrades = """[{"index_key":"NF","status":"OPEN","managed_policy_id":"index_managed_v1"}]""") }, "open_trades")
    @Test fun configRevisionDuringFetch() = changedDuringFetch({ it.copy(configRevision = 9) }, "config_revision")

    @Test fun unchangedStateCommitsWithProbeAndDeadline() {
        val rig = Rig(state())
        val seen = AtomicReference<SingleLegAdmissionCoordinator.Call?>(null)
        val base = rig.admitResult
        rig.admitResult = { c -> seen.set(c); base(c) }
        rig.coord.submit("NF", "r2")
        rig.release.countDown()
        val out = rig.await("r2")
        assertEquals("ADMITTED", out.getString("status"))
        assertEquals(1, rig.fetches.get())                            // BUY: quote only, no margin call
        val c = seen.get()!!
        assertEquals("r2", c.requestId)
        assertEquals(1_000_000L + 15_000L, c.deadlineMs)
        val probe = JSONObject(rig.lastProbe.get())
        assertEquals("idx1_a", probe.getJSONObject("decisions").getJSONObject("NF").getString("decision_id"))
        assertEquals(250000.0, probe.getDouble("capital"), 0.0)
        assertEquals(1L, probe.getLong("config_revision"))
    }

    @Test fun probeSeesCommitTimeState() {
        // state changes AFTER the native re-read but before Python's locked commit: the probe returns the new state
        val rig = Rig(state())
        rig.admitResult = { c -> rig.st.set(rig.st.get().copy(enabled = false)); rig.lastProbe.set(c.probe.current());
            """{"status":"REJECTED","reason":"SINGLE_LEG_PAPER_DISABLED_AT_ADMISSION"}""" }
        rig.coord.submit("NF", "r3"); rig.release.countDown()
        rig.await("r3")
        assertFalse(JSONObject(rig.lastProbe.get()).getBoolean("enabled"))
    }

    @Test fun timeoutIsReconciledByRequestId() {
        val rig = Rig(state())
        rig.admitResult = { null }                                    // PyTimeout
        val n = AtomicInteger(0)
        rig.reconcileResult = { id -> if (n.incrementAndGet() < 3) """{"found":false,"status":"NOT_RECORDED"}"""
            else """{"found":true,"status":"ADMITTED","position_id":"sl1_x","request_id":"$id"}""" }
        rig.coord.submit("NF", "r4"); rig.release.countDown()
        val out = rig.await("r4")
        assertEquals("ADMITTED", out.getString("status"))
        assertTrue(out.getBoolean("reconciled"))
    }

    @Test fun timeoutNeverBecomesUnknownLateAdmission() {
        val rig = Rig(state())
        rig.admitResult = { null }
        rig.reconcileResult = { """{"found":false,"status":"NOT_RECORDED"}""" }
        rig.coord.submit("NF", "r5"); rig.release.countDown()
        val out = rig.await("r5")
        assertEquals("NOT_ADMITTED_COMMIT_DEADLINE_PASSED", out.getString("reason"))
    }

    @Test fun gatesClosedMakeNoBrokerCall() {
        val rig = Rig(state(enabled = false))
        rig.coord.submit("NF", "r6")
        rig.await("r6")
        assertEquals(0, rig.fetches.get())
        assertEquals(1, rig.admits.get())                             // Python still records the named reason
    }

    @Test fun noProposalAndMissingCapital() {
        val rig = Rig(state(decisionId = null)); rig.coord.submit("NF", "r7")
        assertEquals("NO_PROPOSAL", rig.await("r7").getString("reason"))
        val p = state(capital = null).probeJson()
        assertTrue(JSONObject(p).isNull("capital"))
        assertNull(state(capital = null).diff(state(capital = null)))
        assertEquals("NaN", JSONObject(state(capital = Double.NaN).probeJson()).getString("capital"))
    }

    @Test fun unknownRequestFallsBackToReconcile() {
        val rig = Rig(state())
        rig.reconcileResult = { """{"found":false,"status":"NOT_RECORDED"}""" }
        assertEquals("NOT_RECORDED", JSONObject(rig.coord.status("zzz")).getString("status"))
    }
}
