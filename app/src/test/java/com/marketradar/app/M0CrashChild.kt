package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * TEST-ONLY child process for the PROCESS-CRASH recovery test. Usage: <dir> <scenario> <boundary> <targetFile>.
 * Establishes an "old" committed state, then arms M0Durable.crashAt and performs the "new" write; the JVM halts
 * (exit 77) at the armed boundary. Never used by the app.
 */
object M0CrashChild {
    @JvmStatic fun main(args: Array<String>) {
        val dir = File(args[0]); val scenario = args[1]; val boundary = args[2]; val target = args[3]
        M0Durable.crashAt = null
        when (scenario) {
            "protection" -> {
                if (args.getOrNull(4) != "first") ResearchProtection.request(dir, "A", "o", "NF", "t1", "2026-10-21", listOf("CHAIN_POLL"), 1L, expectedRows = 2)
                M0Durable.crashAt = boundary; M0Durable.crashFile = target
                ResearchProtection.request(dir, "B", "o", "NF", "t2", "2026-10-21", listOf("CHAIN_POLL"), 2L, expectedRows = 2)
            }
            "manifest" -> {
                ResearchCollector.setEnabled(dir, true, 1_792_000_000_000L)
                ResearchCollector.beginAttempt(dir, "NF", "2026-10-21|40", 1_792_000_000_000L)
                M0Durable.crashAt = boundary; M0Durable.crashFile = target
                ResearchCollector.beginAttempt(dir, "NF", "2026-10-21|40", 1_792_000_100_000L)
            }
            "imap" -> {
                val lines1 = listOf("NF|2026-10-27|100|CE|K1")
                ResearchCollector.putInstrumentMap(dir, "NF", lines1, 1L, "L1")
                M0Durable.crashAt = boundary; M0Durable.crashFile = target
                ResearchCollector.putInstrumentMap(dir, "NF", listOf("NF|2026-10-27|100|CE|K2"), 2L, "L2")
            }
        }
        System.out.println("NO_CRASH")
        System.exit(0)
    }
}
