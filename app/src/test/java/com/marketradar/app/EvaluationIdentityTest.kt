package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class EvaluationIdentityTest {
    private val date = "2026-09-10"
    private fun snapshot(id: Any? = null, ts: String = "2026-09-10T15:20:44+0530") = JSONObject()
        .put("id", id ?: JSONObject.NULL).put("session_date", date).put("poll_ts", ts)
        .put("recommendation_id", "repeatable-recommendation")
    private fun outcome(id: Any? = 5688L, candidate: String = "candidate", role: String = "secondary") =
        JSONObject().put("snapshot_id", id ?: JSONObject.NULL).put("session_date", date)
            .put("candidate_id", candidate).put("role", role).put("managed_pnl", 12.5)
    private fun rejects(block: () -> Unit) {
        try { block(); fail("Unsafe identity was accepted") } catch (_: IllegalArgumentException) { }
    }

    @Test fun resolvesLocalSnapshotUsingExactInstantAcrossOffsets() {
        val index = EvaluationIdentity.SnapshotIndex(date, JSONArray().put(snapshot(5688L, "2026-09-10T09:50:44Z")))
        assertEquals(5688L, index.resolve(snapshot()))
    }

    @Test fun repeatedRecommendationAcrossPollsDoesNotCollapseSnapshots() {
        val remote = JSONArray().put(snapshot(5688L)).put(snapshot(5689L, "2026-09-10T15:25:44+0530"))
        val index = EvaluationIdentity.SnapshotIndex(date, remote)
        assertEquals(5689L, index.resolve(snapshot(ts = "2026-09-10T15:25:44+0530")))
        rejects { index.resolve(snapshot(ts = "2026-09-10T15:25:45+0530")) }
    }

    @Test fun rejectsAmbiguousOrConflictingSnapshotIdentity() {
        val remote = JSONArray().put(snapshot(5688L)).put(snapshot(5689L))
        rejects { EvaluationIdentity.SnapshotIndex(date, remote).resolve(snapshot()) }
        rejects { EvaluationIdentity.SnapshotIndex(date, JSONArray().put(snapshot(5688L))).resolve(snapshot(999L)) }
        rejects { EvaluationIdentity.SnapshotIndex(date, JSONArray()).resolve(snapshot()) }
    }

    @Test fun rejectsCrossSessionAndRecommendationMismatch() {
        val index = EvaluationIdentity.SnapshotIndex(date, JSONArray().put(snapshot(5688L)))
        rejects { index.resolve(snapshot().put("session_date", "2026-09-09")) }
        rejects { index.resolve(snapshot().put("recommendation_id", "different")) }
        rejects { EvaluationIdentity.SnapshotIndex(date, JSONArray().put(snapshot(5688L).put("session_date", "2026-09-09"))) }
    }

    @Test fun nullZeroNegativeDecimalAndUnknownIdsCannotResume() {
        for (id in listOf(null, JSONObject.NULL, "null", "", 0, -1, 5688.5, "5688.0", 99)) {
            assertFalse(EvaluationIdentity.hasSnapshotIdentity(outcome(id), setOf(5688L)))
        }
        assertTrue(EvaluationIdentity.hasSnapshotIdentity(outcome("5688"), setOf(5688L)))
        assertEquals(3000000000L, EvaluationIdentity.positiveId("3000000000"))
    }

    @Test fun collapsesIdenticalRetriesButKeepsDistinctPollsAndRoles() {
        val rows = JSONArray().put(outcome()).put(outcome()).put(outcome(5689L))
            .put(outcome(role = "primary")).put(outcome(role = "rejected"))
        val result = EvaluationIdentity.validatedDistinctOutcomes(date, rows)
        assertEquals(4, result.length())
        assertEquals(5, rows.length()) // original evidence remains intact
    }

    @Test fun detectsDuplicatesAcrossUploadChunkBoundaries() {
        val rows = JSONArray()
        repeat(501) { rows.put(outcome(candidate = "candidate-$it")) }
        rows.put(outcome(candidate = "candidate-0"))
        assertEquals(501, EvaluationIdentity.validatedDistinctOutcomes(date, rows).length())
    }

    @Test fun duplicateConflictsAreNeverSilentlyOverwritten() {
        rejects { EvaluationIdentity.validatedDistinctOutcomes(date,
            JSONArray().put(outcome()).put(outcome().put("managed_pnl", -100))) }
        rejects { EvaluationIdentity.validatedDistinctOutcomes(date,
            JSONArray().put(outcome(role = "rejected")).put(outcome(role = "rejected").put("label_version", "other"))) }
    }

    @Test fun allRolesRequireIdentityAndSessionBeforeAnyUpload() {
        for (role in listOf("primary", "secondary", "rejected")) {
            rejects { EvaluationIdentity.validatedDistinctOutcomes(date, JSONArray().put(outcome(null, role = role))) }
            rejects { EvaluationIdentity.validatedDistinctOutcomes(date, JSONArray().put(outcome(role = role).put("session_date", "2026-09-09"))) }
        }
        rejects { EvaluationIdentity.validatedDistinctOutcomes(date, JSONArray().put(outcome(candidate = ""))) }
    }

    @Test fun objectKeyOrderDoesNotCreateFalseConflict() {
        val a = outcome().put("extra", JSONObject().put("a", 1).put("b", JSONArray().put(true)))
        val b = outcome().put("extra", JSONObject().put("b", JSONArray().put(true)).put("a", 1))
        assertEquals(1, EvaluationIdentity.validatedDistinctOutcomes(date, JSONArray().put(a).put(b)).length())
    }

    @Test fun missingLocalCaptureIsReplayedThenResolvedBeforeOutcomeUpload() {
        val local = snapshot(ts = "2026-09-10T12:50:55+0530")
            .put("context_json", JSONObject().put("original_evidence", 123))
        val remote = JSONArray()
        var posts = 0
        val reconciler = EvaluationIdentity.SnapshotReconciler(date, { remote }) { payload ->
            posts++
            assertFalse(payload.has("id"))
            assertEquals(123, payload.getJSONObject("context_json").getInt("original_evidence"))
            remote.put(JSONObject(payload.toString()).put("id", 6001L))
            true
        }
        val id = reconciler.resolve(local)
        assertEquals(6001L, id)
        assertTrue(local.isNull("id")) // input is unchanged until whole-file reconciliation succeeds
        assertEquals(id, reconciler.resolve(local))
        assertEquals(1, posts)
        assertEquals(1, reconciler.replayed)
        assertEquals(1, EvaluationIdentity.validatedDistinctOutcomes(date, JSONArray().put(outcome(id))).length())
    }

    @Test fun retryAfterLostUploadResponseReusesDatabaseRow() {
        val remote = JSONArray()
        var posts = 0
        val persist: (JSONObject) -> Boolean = { payload ->
            posts++
            remote.put(JSONObject(payload.toString()).put("id", 6001L))
            false // server committed; client lost the response
        }
        try {
            EvaluationIdentity.SnapshotReconciler(date, { remote }, persist).resolve(snapshot())
            fail("Failed upload response must retain local data")
        } catch (e: IllegalStateException) { assertTrue(e.message!!.contains("EVAL_SNAPSHOT_REPLAY_FAILED")) }
        assertEquals(6001L, EvaluationIdentity.SnapshotReconciler(date, { remote }, persist).resolve(snapshot()))
        assertEquals(1, posts)
    }

    @Test fun successfulPostWithoutExactReadbackCannotInventId() {
        var posts = 0
        val reconciler = EvaluationIdentity.SnapshotReconciler(date, { JSONArray() }) { posts++; true }
        rejects { reconciler.resolve(snapshot()) }
        assertEquals(1, posts)
        assertEquals(0, reconciler.replayed)
    }

    @Test fun unavailableLookupNeverTriggersReplay() {
        var posts = 0
        try {
            EvaluationIdentity.SnapshotReconciler(date, { error("lookup unavailable") }) { posts++; true }
            fail("Unavailable lookup was treated as an empty database")
        } catch (_: IllegalStateException) { }
        assertEquals(0, posts)
    }

    @Test fun identityConflictsAndInvalidLocalDatesNeverTriggerReplay() {
        var posts = 0
        val persist: (JSONObject) -> Boolean = { posts++; true }
        val remote = JSONArray().put(snapshot(5688L))
        rejects { EvaluationIdentity.SnapshotReconciler(date, { remote }, persist)
            .resolve(snapshot().put("recommendation_id", "different")) }
        for (invalid in listOf(snapshot(99L), snapshot("bad-id"),
            snapshot(ts = "2026-09-09T15:20:44+0530"), snapshot().put("session_date", "2026-09-09"),
            snapshot().put("recommendation_id", JSONObject.NULL))) {
            rejects { EvaluationIdentity.SnapshotReconciler(date, { JSONArray() }, persist).resolve(invalid) }
        }
        assertEquals(0, posts)
    }

    @Test fun retryAfterReadbackFailureDoesNotReinsert() {
        val remote = JSONArray()
        var reads = 0
        var posts = 0
        val persist: (JSONObject) -> Boolean = { payload ->
            posts++
            remote.put(JSONObject(payload.toString()).put("id", 6001L))
            true
        }
        try {
            EvaluationIdentity.SnapshotReconciler(date, {
                if (++reads == 2) error("readback unavailable")
                remote
            }, persist).resolve(snapshot())
            fail("Unconfirmed readback must stop")
        } catch (_: IllegalStateException) { }
        assertEquals(6001L, EvaluationIdentity.SnapshotReconciler(date, { remote }, persist).resolve(snapshot()))
        assertEquals(1, posts)
    }
}
