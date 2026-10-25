package com.marketradar.app

import java.io.File
import java.nio.file.Files
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class Stage2CapacityCounterexamples {
    @Test fun unknown_string_state_is_not_reported_as_known_zero_active_capacity() {
        val dir = Files.createTempDirectory("capacity-semantic-state").toFile()
        try {
            val ledger = JSONObject().put("entries", JSONObject().put("e", JSONObject().put("state", "UNRECOGNISED")))
                .put("rejected_total", 0)
            M0Durable.write(File(dir, "protection.json"), ledger.toString())
            val result = ResearchCapacity.read(dir, 1L)
            assertFalse(result.toString(), result.getBoolean("known"))
            assertTrue(result.toString(), result.isNull("active"))
        } finally { dir.deleteRecursively() }
    }
}
