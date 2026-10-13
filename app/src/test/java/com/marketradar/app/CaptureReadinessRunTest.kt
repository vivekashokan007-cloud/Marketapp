package com.marketradar.app

import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/** Runs the readiness report (PC2 parity + generated sampled cohort) over a real export when PC2_AUDIT_DIR is set. */
class CaptureReadinessRunTest {
    @Test fun readinessWhenRequested() {
        val dir = System.getenv("PC2_AUDIT_DIR")?.takeIf { it.isNotBlank() } ?: return
        val r = CaptureReadiness.run(File(dir))
        assertTrue("capture cohort NOT complete - see ${File(dir, "capture_readiness_report.json")}", r.getBoolean("capture_cohort_complete"))
    }
}
