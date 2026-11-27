package com.marketradar.app

import org.junit.Assert.*
import org.junit.Test
import java.io.File
import java.nio.file.Files

/** 27 Nov step 2: diagnostics are bounded, read-only and never expose paths, names below top level or contents. SYNTHETIC. */
class RecoveryDiagnosticsTest {
    private fun tree(): File {
        val root = Files.createTempDirectory("diag").toFile()
        File(root, "bnf_raw_capture_v1").mkdirs()
        File(root, "bnf_raw_capture_v1/1_aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.json").writeText("{\"url\":\"SECRET_URL_CONTENT\"}")
        File(root, "bnf_raw_capture_v1/1_aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.body.gz").writeBytes(ByteArray(1000))
        File(root, "paper/positions").mkdirs()
        File(root, "paper/positions/open_trade_private_name.json").writeBytes(ByteArray(300))
        File(root, "cloud_session_v1").mkdirs()
        File(root, "cloud_session_v1/access_token.json").writeText("eyJ-not-a-real-token")
        File(root, "top_level.txt").writeBytes(ByteArray(7))
        return root
    }
    private fun snapshot(root: File) = root.walkTopDown().filter { it.isFile }.associate { it.path to (it.length() to it.readBytes().contentHashCode()) }

    @Test fun topLevelUsageCountsBytesPerGroupWithoutPathsNamesOrContents() {
        val root = tree()
        val before = snapshot(root)
        val b = RecoveryDiagnostics.topLevelUsage(root, "files")
        assertEquals(RecoveryDiagnostics.Usage(2, 1000L + "{\"url\":\"SECRET_URL_CONTENT\"}".length), b.groups["bnf_raw_capture_v1"])
        assertEquals(RecoveryDiagnostics.Usage(1, 300), b.groups["paper"])
        assertEquals(RecoveryDiagnostics.Usage(1, 7), b.groups["(root_files)"])
        assertNull(b.groups["cloud_session_v1"]) // secret-looking top-level names are redacted
        assertEquals(1L, b.groups["redacted"]!!.files)
        assertEquals(5L, b.totalFiles)
        val text = b.logLine() + b.toJson().toString()
        for (leak in listOf(root.path, "open_trade_private_name", "access_token", "SECRET_URL_CONTENT", "eyJ", "positions"))
            assertFalse(leak, text.contains(leak))
        assertEquals(before, snapshot(root)) // read-only
    }

    @Test fun walkIsBoundedAndSymlinksAreNeitherFollowedNorCounted() {
        val root = tree()
        val outside = Files.createTempDirectory("outside").toFile()
        File(outside, "big.bin").writeBytes(ByteArray(5000))
        Files.createSymbolicLink(File(root, "link_dir").toPath(), outside.toPath())
        Files.createSymbolicLink(File(root, "paper/link_file").toPath(), File(outside, "big.bin").toPath())
        val b = RecoveryDiagnostics.topLevelUsage(root, "files")
        assertEquals(5L, b.totalFiles)
        assertTrue(b.totalBytes < 5000)
        assertTrue(b.symlinksSkipped >= 1)
        val bounded = RecoveryDiagnostics.topLevelUsage(root, "files", maxEntries = 3)
        assertTrue(bounded.truncated)
        val folded = RecoveryDiagnostics.topLevelUsage(root, "files", maxGroups = 1)
        assertEquals(setOf("bnf_raw_capture_v1", "other"), folded.groups.keys)
        assertEquals(5L, folded.groups.values.sumOf { it.files })
        assertEquals(0L, RecoveryDiagnostics.topLevelUsage(File(root, "missing"), "x").totalFiles)
    }

    @Test fun rawOutboxReportsAckStateWithoutTouchingFilesOrIds() {
        val root = Files.createTempDirectory("outbox").toFile()
        val dir = File(root, "bnf_raw_capture_v1").apply { mkdirs() }
        val ack = File(root, "bnf_table_ack_v1").apply { mkdirs() }
        val ids = (1..4).map { "17957${it}_0000000$it-aaaa-bbbb-cccc-dddddddddddd" }
        ids.forEach { File(dir, "$it.json").writeText("{}"); File(dir, "$it.body.gz").writeBytes(ByteArray(10)) }
        File(dir, "stray.tmp").writeText("x")
        File(ack, ids[0]).writeText("verified"); File(ack, ids[1]).writeText("verified")
        File(ack, "1_orphan0-aaaa-bbbb-cccc-dddddddddddd").writeText("verified")
        File(ack, "ack_1.tmp").writeText("partial")
        val before = snapshot(root)
        val s = RecoveryDiagnostics.rawOutbox(dir, ack)
        assertEquals(4, s.metadataFiles); assertEquals(4, s.bodyFiles); assertEquals(1, s.otherFiles)
        assertEquals(2, s.acknowledged); assertEquals(2, s.pending); assertEquals(1, s.orphanAcks)
        assertEquals(4L * 2 + 4L * 10 + 1, s.bytes)
        ids.forEach { assertFalse(s.logLine().contains(it)) }
        assertEquals(before, snapshot(root))
        assertEquals(0, RecoveryDiagnostics.rawOutbox(null, null).metadataFiles)
    }

    @Test fun passStatsMeasureStagesAndUploadedBytesAsNumbersOnly() {
        var now = 1000L
        val s = RecoveryDiagnostics.PassStats("scheduled") { now }
        s.stage("claim"); now += 40
        s.stage("hash_before"); now += 2500
        s.stage("upload")
        val counted = s.countingStream(ByteArray(70_000).inputStream()).use { it.readBytes() }
        assertEquals(70_000, counted.size)
        now += 10
        val total = s.finish("VERIFIED_CHECKPOINT")
        assertEquals(2550L, total)
        val j = s.toJson(total)
        assertEquals(40L, j.getJSONObject("stages_ms").getLong("claim"))
        assertEquals(2500L, j.getJSONObject("stages_ms").getLong("hash_before"))
        assertEquals(70_000L, j.getLong("bytes_uploaded"))
        assertTrue(s.logLine(total).startsWith("CLOUD_PASS_STATS trigger=scheduled mode=FULL"))
        val w = RecoveryDiagnostics.Wakeups(); w.pass("scheduled"); w.pass("scheduled"); w.pass("worker"); w.mutationSignals.addAndGet(5)
        assertEquals(2L, w.toJson().getLong("passes_scheduled")); assertEquals(5L, w.toJson().getLong("mutation_signals"))
        val every = RecoveryDiagnostics.Every(100)
        assertTrue(every.due(0)); assertFalse(every.due(50)); assertTrue(every.due(150))
    }

    @Test fun diagnosticsAreWiredIntoRecoveryWithoutBecomingPortableState() {
        val src = listOf(File("src/main/java/com/marketradar/app/CloudRecovery.kt"),
            File("app/src/main/java/com/marketradar/app/CloudRecovery.kt")).first { it.exists() }.readText()
        assertTrue(src.contains("LogBuffer.add('I', \"CloudRecovery\", stats.logLine(total))"))
        assertTrue(src.contains(".put(\"diagnostics\", JSONObject().put(\"last_pass\""))
        assertTrue(src.contains("vault.store(stats.countingStream(source.stream()))"))
        // LogBuffer persists under files/forensics, which is never a checkpoint input,
        // so writing diagnostics cannot itself trigger a new recovery upload.
        assertFalse(CloudDataPolicy.portablePath("forensics/logbuffer.jsonl"))
        assertFalse(src.contains("getSharedPreferences(\"cloud_recovery_diagnostics"))
    }
}
