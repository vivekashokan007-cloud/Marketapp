package com.marketradar.app

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class B493ChainEvidenceGateTest {
    private fun source(name: String): String = listOf(
        File("src/main/java/com/marketradar/app/$name"),
        File("app/src/main/java/com/marketradar/app/$name")
    ).first { it.isFile }.readText()

    @Test
    fun fetchedChainIsPersistedBeforeBrainAndNotInsideSuccessBranch() {
        val src = source("MarketWatchService.kt")
        val call = src.indexOf("persistFetchedChainEvidenceBeforeBrain(bnfChainJson, nfChainJson)")
        val brain = src.indexOf("runBrainAnalysis(\n                pollObj")

        assertTrue("pre-Brain chain persistence call must exist", call >= 0)
        assertTrue("chain evidence must be saved before Brain", brain > call)

        val successBranch = src.substring(src.indexOf("if (result != null)"), src.indexOf("} else {", src.indexOf("if (result != null)")))
        assertFalse(
            "chain persistence must not depend on a successful Brain result",
            successBranch.contains("saveChainRows(")
        )
    }

    @Test
    fun chainReplayUsesDatabaseBusinessKey() {
        val src = source("SupabaseClient.kt")
        assertTrue(
            src.contains("ml_option_chain_snapshots?on_conflict=poll_ts,index_key,strike,option_type")
        )
        assertTrue(src.contains("resolution=merge-duplicates"))
    }

    @Test
    fun incompleteH2CoverageBlocksLabelsAndC3() {
        val ml = source("MarketMLService.kt")
        val bridge = source("NativeBridge.kt")

        assertTrue(ml.contains("phase = \"INCOMPLETE_H2_MARKET_DATA\""))
        assertTrue(ml.contains("Labels are NOT saved and C3 will not run"))
        assertTrue(ml.contains("EVAL_BLOCKED_INCOMPLETE_H2"))
        assertFalse(ml.contains("EVAL_CHAIN_H2_INCOMPLETE_ADVISORY"))
        assertTrue(bridge.contains("phase == \"INCOMPLETE_H2_MARKET_DATA\""))
    }
}
