package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject

/**
 * Keeps locally closed trades visible to the risk gate until a later successful
 * server read contains the same id. The PWA remains responsible for the remote
 * update; this journal only protects local safety state across a reload, restart,
 * or stale bootstrap response.
 */
internal object ClosedTradeLedger {
    const val CLOSED_TRADES_PREF = "closed_trades"
    const val PENDING_CLOSED_TRADES_PREF = "pending_closed_trades"
    private const val MAX_ROWS = 500

    internal data class State(val closedTradesJson: String, val pendingTradesJson: String)

    private fun array(raw: String?): JSONArray = try {
        JSONArray(raw ?: "[]")
    } catch (_: Exception) {
        JSONArray()
    }

    private fun id(row: JSONObject?): String = row?.optString("id", "")?.trim().orEmpty()

    private fun boundedUnique(first: List<JSONObject>, second: List<JSONObject>): JSONArray {
        val result = JSONArray()
        val seen = mutableSetOf<String>()
        for (row in first + second) {
            val rowId = id(row)
            if (rowId.isBlank() || !seen.add(rowId)) continue
            result.put(row)
            if (result.length() >= MAX_ROWS) break
        }
        return result
    }

    private fun rows(raw: JSONArray): List<JSONObject> = buildList {
        for (index in 0 until raw.length()) raw.optJSONObject(index)?.let(::add)
    }

    /** Records a close locally and retains it in the journal until observed remotely. */
    fun record(existingClosedJson: String?, pendingJson: String?, incomingJson: String): State? {
        val incoming = try { JSONObject(incomingJson) } catch (_: Exception) { return null }
        if (id(incoming).isBlank()) return null
        val closed = boundedUnique(listOf(incoming), rows(array(existingClosedJson)))
        val pending = boundedUnique(listOf(incoming), rows(array(pendingJson)))
        return State(closed.toString(), pending.toString())
    }

    /**
     * Reconciles a successful server response. A response can acknowledge pending
     * rows, but cannot erase rows it did not contain. Call only after a successful
     * parse of an authoritative server response.
     */
    fun reconcileRemote(remoteJson: String, pendingJson: String?): State? {
        val remote = try { JSONArray(remoteJson) } catch (_: Exception) { return null }
        val remoteRows = rows(remote)
        val remoteIds = remoteRows.mapTo(mutableSetOf()) { id(it) }
        val pendingUnacknowledged = rows(array(pendingJson)).filter { id(it) !in remoteIds }
        val closed = boundedUnique(remoteRows, pendingUnacknowledged)
        val pending = boundedUnique(pendingUnacknowledged, emptyList())
        return State(closed.toString(), pending.toString())
    }

    /** Merges an unacknowledged UI payload without clearing the pending journal. */
    fun mergeUnacknowledged(incomingJson: String, pendingJson: String?): State? {
        val incoming = try { JSONArray(incomingJson) } catch (_: Exception) { return null }
        val pending = rows(array(pendingJson))
        val closed = boundedUnique(rows(incoming), pending)
        val retainedPending = boundedUnique(pending, emptyList())
        return State(closed.toString(), retainedPending.toString())
    }
}
