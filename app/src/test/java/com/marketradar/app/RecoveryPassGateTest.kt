package com.marketradar.app

import org.junit.Assert.*
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.nio.file.StandardCopyOption

/** 27 Nov step 3: quiet recovery passes may skip re-hashing only when every change signal is clean. SYNTHETIC. */
class RecoveryPassGateTest {
    private val m = "a".repeat(64)
    private val sig = mapOf("files/paper/open.json" to "f:10:1:k", "prefs/market_radar.json" to "p:" + "b".repeat(64))
    private val base = RecoveryPassGate.Baseline(mutation = 7, signature = sig, revision = 32, manifestSha256 = m, verifiedAtElapsed = 1_000)
    private fun obs(
        ready: Boolean = true, canMonitor: Boolean = true, restorePending: Boolean = false, webCaptureFailed: Boolean = false,
        revision: Long = 32, localRevision: Long = 32, head: String = m, local: String = m,
        mutation: Long = 7, signature: Map<String, String> = sig, now: Long = 61_000
    ) = RecoveryPassGate.Observation(ready, canMonitor, restorePending, webCaptureFailed, revision, localRevision, head, local, mutation, signature, now)

    @Test fun onlyAFullyQuietVerifiedStateSkipsAndEveryDoubtRunsTheFullCryptographicPass() {
        assertEquals(RecoveryPassGate.SKIP, RecoveryPassGate.decide(base, obs()))
        assertEquals("NO_VERIFIED_BASELINE", RecoveryPassGate.decide(null, obs())) // first pass of every process
        assertEquals("RESTORE_PENDING", RecoveryPassGate.decide(base, obs(restorePending = true)))
        assertEquals("NOT_READY", RecoveryPassGate.decide(base, obs(ready = false)))
        assertEquals("NOT_MONITOR_READY", RecoveryPassGate.decide(base, obs(canMonitor = false)))
        assertEquals("WEB_CAPTURE_FAILED", RecoveryPassGate.decide(base, obs(webCaptureFailed = true)))
        assertEquals("REVISION_CHANGED", RecoveryPassGate.decide(base, obs(revision = 33)))           // another commit (other device or lost reply)
        assertEquals("REVISION_CHANGED", RecoveryPassGate.decide(base, obs(revision = 33, localRevision = 33)))
        assertEquals("MANIFEST_CHANGED", RecoveryPassGate.decide(base, obs(head = "c".repeat(64))))
        assertEquals("MANIFEST_CHANGED", RecoveryPassGate.decide(base, obs(local = "c".repeat(64))))
        assertEquals("MANIFEST_CHANGED", RecoveryPassGate.decide(base, obs(head = "", local = "")))
        assertEquals("MUTATION_SIGNAL", RecoveryPassGate.decide(base, obs(mutation = 8)))             // FileObserver / prefs listener
        assertEquals("INVENTORY_CHANGED", RecoveryPassGate.decide(base, obs(signature = sig + ("files/new.json" to "f:1:1:k"))))
        assertEquals("INVENTORY_CHANGED", RecoveryPassGate.decide(base, obs(signature = sig - "files/paper/open.json")))
        assertEquals("CONTENT_SIGNATURE_CHANGED", RecoveryPassGate.decide(base, obs(signature = sig + ("prefs/market_radar.json" to "p:" + "d".repeat(64)))))
        assertEquals("SCRUB_DUE", RecoveryPassGate.decide(base, obs(now = 1_000 + RecoveryPassGate.SCRUB_INTERVAL_MS)))
        assertEquals("SCRUB_DUE", RecoveryPassGate.decide(base, obs(now = 500))) // clock anomaly is not trusted
    }

    @Test fun statSignatureDetectsAppendsRewritesAndReplacementsButTheMtimeResetGapIsBoundedByScrub() {
        val dir = Files.createTempDirectory("gate").toFile()
        val f = File(dir, "open.json").apply { writeText("0123456789"); setLastModified(1_700_000_000_000) }
        val s0 = RecoveryPassGate.fileSignature(f)
        f.appendText("x"); assertNotEquals(s0, RecoveryPassGate.fileSignature(f))
        f.writeText("9876543210"); f.setLastModified(1_700_000_005_000)
        assertNotEquals(s0, RecoveryPassGate.fileSignature(f))                  // same size, new mtime
        val replacement = File(dir, "tmp").apply { writeText("abcdefghij"); setLastModified(1_700_000_000_000) }
        Files.move(replacement.toPath(), f.toPath(), StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE)
        if (Files.readAttributes(f.toPath(), java.nio.file.attribute.BasicFileAttributes::class.java).fileKey() != null)
            assertNotEquals(s0, RecoveryPassGate.fileSignature(f))              // atomic save: new file key
        // Residual gap, made explicit: same inode, same size, mtime forced back, no observer event.
        val g = File(dir, "g.json").apply { writeText("0123456789"); setLastModified(1_700_000_000_000) }
        val g0 = RecoveryPassGate.fileSignature(g)
        g.writeText("ABCDEFGHIJ"); g.setLastModified(1_700_000_000_000)
        assertEquals(g0, RecoveryPassGate.fileSignature(g))
        val b = base.copy(signature = mapOf("files/g.json" to g0))
        val quiet = obs(signature = mapOf("files/g.json" to RecoveryPassGate.fileSignature(g)))
        assertEquals(RecoveryPassGate.SKIP, RecoveryPassGate.decide(b, quiet))
        assertEquals("SCRUB_DUE", RecoveryPassGate.decide(b, quiet.copy(nowElapsed = 1_000 + RecoveryPassGate.SCRUB_INTERVAL_MS)))
        assertNotEquals(RecoveryPassGate.bytesSignature("a".toByteArray()), RecoveryPassGate.bytesSignature("b".toByteArray()))
    }

    @Test fun symlinkSignatureIsNotFollowed() {
        val dir = Files.createTempDirectory("gate_link").toFile()
        val target = File(dir, "t").apply { writeText("123") }
        val link = File(dir, "l"); Files.createSymbolicLink(link.toPath(), target.toPath())
        val before = RecoveryPassGate.fileSignature(link)
        target.writeText("123456789")
        assertEquals(before, RecoveryPassGate.fileSignature(link)) // inventory still rejects symlinks (SYMLINK_NOT_BACKED_UP)
    }

    private fun src(name: String) = listOf(File("src/main/java/com/marketradar/app/$name"),
        File("app/src/main/java/com/marketradar/app/$name")).first { it.exists() }.readText()

    @Test fun runPassEvaluatesTheGateAfterLeaseClaimAndRestoreAndTheSkipPathCannotCommitPruneOrDelete() {
        val s = src("CloudRecovery.kt")
        val body = s.substringAfter("private fun runPassMeasured(").substringBefore("private fun fingerprintMap(")
        val gate = body.indexOf("RecoveryPassGate.decide(")
        assertTrue(gate > 0)
        assertTrue(body.indexOf("head = transport.rpc(\"mr_cloud_claim\", claim)") in 0 until gate) // lease renewed every pass
        assertTrue(body.indexOf("vault.stage(sourceManifest, stage)") in 0 until gate)                // restore decided first
        assertTrue(body.indexOf("CloudMonitorResume.eligible(") in 0 until gate)
        assertTrue(body.indexOf("error(\"CLOUD_REVISION_CONFLICT\")") in 0 until gate)
        val skip = body.substringAfter("if (gate == RecoveryPassGate.SKIP) {").substringBefore("return\n")
        for (forbidden in listOf("publishManifest", "mr_cloud_commit", "pruneAcknowledged", "delete", "fingerprints(", "vault.", "putLong(\"revision\"", "verifiedBaseline ="))
            assertFalse(forbidden, skip.contains(forbidden))
        // FULL path keeps both complete SHA-256 inventories and the head readback.
        val after = body.substring(gate)
        assertTrue(after.contains("val before = fingerprints(sources) { transport.renewLease?.invoke() }"))
        assertTrue(after.contains("require(before == fingerprints(inventory(c)) { transport.renewLease?.invoke() }) { \"CACHE_CHANGED_DURING_UPLOAD\" }"))
        assertTrue(after.contains("\"HEAD_READBACK_MISMATCH\""))
        // R2: the post-commit status label uses the pre-hash stat signatures (no third full inventory read).
        assertTrue(after.contains("expectedSignature == signature(inventory(c)))\n                \"VERIFIED_CHECKPOINT\""))
        assertFalse(after.contains("before == fingerprints(inventory(c)) { transport.renewLease?.invoke() })\n                \"VERIFIED_CHECKPOINT\""))
        // A baseline exists only after a verified, mutation-free FULL pass; failures clear it.
        assertTrue(body.contains("verifiedBaseline = if (state == \"VERIFIED_CHECKPOINT\" && snapshotMutation == mutations.get())"))
        assertTrue(body.contains("Baseline(snapshotMutation, expectedSignature, revision,"))
        // R4: the gate uses the real mutation counter; no blanket override when signatures match.
        assertFalse(body.contains("gateMutation"))
        assertTrue(body.contains("mutation = mutations.get(), signature = gateSignature"))

        assertTrue(after.indexOf("verifiedBaseline = null") < after.indexOf("val before = fingerprints(sources)"))
        assertTrue(body.substringAfterLast("} catch (e: Exception) {").trimStart().startsWith("verifiedBaseline = null"))
        assertTrue(body.indexOf("val baselineSignature = signature(sources)") < body.indexOf("val before = fingerprints(sources)"))
        // Unchanged scheduling: 30 s fixed delay plus 15 min worker; skipping keeps claims (lease 120 s) flowing.
        assertTrue(s.contains("executor.scheduleWithFixedDelay({ runPass(c, \"scheduled\") }, 0, 30, TimeUnit.SECONDS)"))
    }
}
