package com.marketradar.app

import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Runs the DB-1 parity audit over a real export when, and only when,
 * PC2_AUDIT_DIR points at one (see tools/pc2_parity_export.sh). In ordinary CI
 * the variable is unset and this is a no-op; the audit logic itself is covered
 * by Pc2ParityAuditTest on every run.
 */
class Pc2ParityAuditRunTest {
    @Test
    fun certifyExportedSessionWhenRequested() {
        val dir = System.getenv("PC2_AUDIT_DIR")?.takeIf { it.isNotBlank() } ?: return
        val report = Pc2ParityAudit.run(File(dir))
        assertTrue(
            "DB-1 parity NOT certified - see ${File(dir, "pc2_parity_report.json")}",
            report.certified
        )
    }
}
