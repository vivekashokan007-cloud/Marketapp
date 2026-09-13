package com.marketradar.app

import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Verifies compactTeacherResearchCandidate retains contract-identity keys.
 *
 * Prefers the extracted [MarketMLService.CONTRACT_IDENTITY_COMPACTION_KEYS]
 * constant when the JVM test classpath can load the service companion; also
 * asserts key strings appear in the MarketMLService.kt source so the check
 * stays meaningful if companion loading is awkward in unit-test isolation.
 *
 * Kotlin unit execution may still be blocked pending Android SDK/JDK in some
 * agents — Python suite also cross-checks the same source allowlist.
 */
class ContractIdentityCompactionTest {

    private val requiredIdentityKeys = listOf(
        "index_key",
        "expiry_cycle",
        "calendar_dte",
        "trading_dte",
        "dte_basis",
        "dte_source",
        "dte_calendar_version",
        "contract_lot_size",
        "number_of_lots",
        "lot_size",
        "quantity_units",
        "quantity_unit",
        "lot_source",
        "lot_table_version",
        "lot_as_of",
        "lot_conflict",
        "matched_rule_id",
        "captured_contract_lot",
        "rule_contract_lot",
        "contract_identity_quarantine",
        "evaluation_ineligible",
        "calibration_ineligible",
        "contract_identity",
        "identity_complete",
        "exclusion_reason",
        "retained_for_recovery",
    )

    @Test
    fun contractIdentityCompactionKeysConstantContainsRequiredFields() {
        val keys = MarketMLService.CONTRACT_IDENTITY_COMPACTION_KEYS.toSet()
        for (key in requiredIdentityKeys) {
            assertTrue("missing CONTRACT_IDENTITY_COMPACTION_KEYS entry: $key", keys.contains(key))
        }
    }

    @Test
    fun compactTeacherResearchCandidateSourceAllowlistIncludesIdentityKeys() {
        val source = loadMarketMLServiceSource()
        assertTrue(source.contains("CONTRACT_IDENTITY_COMPACTION_KEYS"))
        val fn = source
            .substringAfter("private fun compactTeacherResearchCandidate")
            .substringBefore("private fun compactTeacherResearchCandidates")
        assertTrue(
            "compactTeacherResearchCandidate must append CONTRACT_IDENTITY_COMPACTION_KEYS",
            fn.contains("CONTRACT_IDENTITY_COMPACTION_KEYS")
        )
        // Pre-existing allowlist keys that must remain
        assertTrue(fn.contains("\"tDTE\"") || source.contains("\"tDTE\""))
        assertTrue(source.contains("\"index\""))
        assertTrue(source.contains("\"expiry\""))
        for (key in requiredIdentityKeys) {
            assertTrue("MarketMLService.kt missing identity key \"$key\"", source.contains("\"$key\""))
        }
    }

    private fun loadMarketMLServiceSource(): String {
        val candidates = listOf(
            File("src/main/java/com/marketradar/app/MarketMLService.kt"),
            File("app/src/main/java/com/marketradar/app/MarketMLService.kt"),
            File("../main/java/com/marketradar/app/MarketMLService.kt"),
        )
        for (file in candidates) {
            if (file.isFile) return file.readText()
        }
        // Walk up from user.dir looking for the module source
        var dir = File(System.getProperty("user.dir") ?: ".").canonicalFile
        repeat(6) {
            val hit = File(dir, "app/src/main/java/com/marketradar/app/MarketMLService.kt")
            if (hit.isFile) return hit.readText()
            val hit2 = File(dir, "src/main/java/com/marketradar/app/MarketMLService.kt")
            if (hit2.isFile) return hit2.readText()
            dir = dir.parentFile ?: return@repeat
        }
        error("MarketMLService.kt not found from user.dir=${System.getProperty("user.dir")}")
    }
}
