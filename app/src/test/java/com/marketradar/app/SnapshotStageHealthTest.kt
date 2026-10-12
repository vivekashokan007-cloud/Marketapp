package com.marketradar.app

import org.junit.Assert.*
import org.junit.Test

class SnapshotStageHealthTest {
    @Test fun countsFailuresAcrossAttemptsAndResetsAtTheNextSession() {
        val a = SnapshotStageHealth.record(null, "2026-10-12", "timeout", "2026-10-12|12:00", 4000)
        val b = SnapshotStageHealth.record(a.toString(), "2026-10-12", "inflight_skip", "2026-10-12|12:05")
        val c = SnapshotStageHealth.record(b.toString(), "2026-10-12", "built", "2026-10-12|12:10", 1000)
        assertEquals(1, c.getJSONObject("counts").getInt("timeout"))
        assertEquals(1, c.getJSONObject("counts").getInt("inflight_skip"))
        assertEquals(1, c.getJSONObject("counts").getInt("built"))
        val next = SnapshotStageHealth.record(c.toString(), "2026-10-13", "attempt", "2026-10-13|09:15")
        assertFalse(next.getJSONObject("counts").has("timeout"))
        assertEquals(1, next.getJSONObject("counts").getInt("attempt"))
    }
}
