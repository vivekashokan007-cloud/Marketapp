package com.marketradar.app

import org.junit.Assert.*
import org.junit.Test
import java.io.File

class LearningModelEvidenceTest {
    @Test fun identifiesInstalledFileWithoutClaimingLoadedOrTrained() {
        val file = File.createTempFile("model-evidence", ".json")
        try {
            file.writeText("""{"version":"test-model","n_train":12}""")
            val first = LearningModelEvidence.read(file)
            assertEquals("INSTALLED_FILE_ONLY", first.getString("status"))
            assertEquals("test-model", first.getString("version"))
            assertEquals(64, first.getString("sha256").length)
            assertFalse(first.getBoolean("loaded_predictor_verified"))
            file.writeText("""{"version":"different-model","n_train":12}""")
            assertNotEquals(first.getString("sha256"), LearningModelEvidence.read(file).getString("sha256"))
            file.writeText("invalid")
            assertEquals("UNREADABLE", LearningModelEvidence.read(file).getString("status"))
        } finally { file.delete() }
        assertEquals("MISSING", LearningModelEvidence.read(file).getString("status"))
    }
}
