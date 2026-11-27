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

    @Test fun dirtyGroupsNameOnlyTopLevelGroupsAndCountChangesAgainstTheVerifiedManifest() {
        val previous = mapOf("files/single_leg_v1/positions.json" to "a", "files/cloud_history_outbox_v1/x_1.bin" to "b",
            "prefs/market_radar.json" to "c", "files/cloud_session_v1/token_cache.json" to "d")
        val current = mapOf("files/single_leg_v1/positions.json" to "a2", "prefs/market_radar.json" to "c",
            "files/research_m0/session_private_name.jsonl" to "e", "files/cloud_session_v1/token_cache.json" to "d2")
        val d = RecoveryDiagnostics.dirty(current, previous, { 100L })
        assertEquals(1, d.added); assertEquals(2, d.modified); assertEquals(1, d.removed); assertEquals(300L, d.bytes)
        assertTrue(d.groups.contains("files/single_leg_v1:a0m1r0"))
        assertTrue(d.groups.contains("files/cloud_history_outbox_v1:a0m0r1"))
        assertTrue(d.groups.contains("files/redacted:a0m1r0"))
        for (leak in listOf("positions.json", "session_private_name", "token_cache", "cloud_session")) assertFalse(leak, d.groups.contains(leak))
        val s = RecoveryDiagnostics.PassStats("worker") { 0L }; s.dirty = d
        assertTrue(s.logLine(0).contains("dirty=a1/m2/r1/300b"))
        assertEquals(0, RecoveryDiagnostics.dirty(previous, previous, { 1L }).let { it.added + it.modified + it.removed })
    }

    // ---- R2 corrections ----
    @Test fun unreadableEntriesAreCountedAndMarkTheBreakdownIncomplete() {
        val root = tree()
        val locked = File(root, "locked_dir").apply { mkdirs(); File(this, "x").writeText("1") }
        try {
            assertTrue(locked.setReadable(false, false) && locked.setExecutable(false, false))
            val b = RecoveryDiagnostics.topLevelUsage(root, "t")
            if (System.getProperty("user.name") != "root") { // root bypasses permissions; nothing would fail
                assertTrue(b.failures >= 1); assertTrue(b.incomplete)
                assertTrue(b.incompleteReason.contains("UNREADABLE_ENTRIES"))
                assertTrue(b.logLine().contains("incomplete=UNREADABLE_ENTRIES"))
                assertTrue(b.toJson().getBoolean("incomplete"))
            }
            val ok = RecoveryDiagnostics.topLevelUsage(tree(), "t")
            assertFalse(ok.incomplete); assertEquals(0L, ok.failures); assertTrue(ok.logLine().contains("incomplete=no"))
        } finally { locked.setReadable(true, false); locked.setExecutable(true, false) }
    }

    @Test fun walkStopsAtItsTimeBudgetAndSaysSo() {
        val root = createTempDir("budget")
        for (i in 0 until 2_000) File(root, "g${i % 4}/f$i").apply { parentFile!!.mkdirs(); writeText("x") }
        var fakeNs = 0L
        val b = RecoveryDiagnostics.topLevelUsage(root, "t", budgetMs = 1_000, nanoTime = { fakeNs += 100_000_000; fakeNs }) // each clock read = 100 ms
        assertTrue(b.incomplete); assertTrue(b.incompleteReason.contains("TIME_BUDGET"))
        assertTrue(b.totalFiles < 2_000)
        val full = RecoveryDiagnostics.topLevelUsage(root, "t")
        assertFalse(full.incomplete); assertEquals(2_000L, full.totalFiles)
        // Real-clock bound on a large tree (20,000 files): well inside the 20 s budget.
        for (i in 0 until 18_000) File(root, "h${i % 30}/f$i").apply { parentFile!!.mkdirs(); writeText("x") }
        val t0 = System.nanoTime(); val big = RecoveryDiagnostics.topLevelUsage(root, "t"); val ms = (System.nanoTime() - t0) / 1_000_000
        assertFalse(big.incomplete); assertEquals(20_000L, big.totalFiles)
        println("R2_MEASURE scenario=walk_20000_files walk_ms=$ms budget_ms=${RecoveryDiagnostics.WALK_BUDGET_MS}")
        assertTrue("walk took $ms ms", ms < RecoveryDiagnostics.WALK_BUDGET_MS)
    }

    @Test fun commitAttemptIsDistinctFromAConfirmedCheckpoint() {
        val s = RecoveryDiagnostics.PassStats("t") { 0L }
        s.commitAttempted = true
        assertTrue(s.toJson(0).getBoolean("commit_attempted")); assertFalse(s.toJson(0).getBoolean("commit_confirmed"))
        assertTrue(s.logLine(0).contains("commit_attempted=true commit_confirmed=false"))
        assertFalse(s.toJson(0).has("committed"))
        val src = listOf(File("src/main/java/com/marketradar/app/CloudRecovery.kt"),
            File("app/src/main/java/com/marketradar/app/CloudRecovery.kt")).first { it.exists() }.readText()
        val commit = src.substringAfter("stats.commitAttempted = true").substringBefore("stats.commitConfirmed = true")
        assertTrue(commit.contains("vault.publishManifest(")); assertTrue(commit.contains("\"mr_cloud_commit\""))
        assertTrue(commit.contains("\"HEAD_READBACK_MISMATCH\""))   // confirmed only after the head readback matched
    }

    @Test fun changeTraceNamesWriterClassesRevertsAndPreferenceKeysWithoutValues() {
        val t = RecoveryDiagnostics.ChangeTrace()
        val prefs1 = """{"open_trades":{"type":"string","value":"[]"},"ui":{"type":"string","value":"{\"tab\":\"a\",\"scroll\":1}"}}""".toByteArray()
        val prefs2 = """{"open_trades":{"type":"string","value":"[]"},"ui":{"type":"string","value":"{\"tab\":\"a\",\"scroll\":2}"}}""".toByteArray()
        val base = mapOf("files/research_m0/2026-11-27/row_0001.jsonl" to "h1", "files/status/tick.json" to "t1", "prefs/market_radar.json" to "p1")
        assertFalse(t.observe(base, mapOf("prefs/market_radar.json" to prefs1)).primed)
        val arch = "files/cloud_history_outbox_v1/0123456789abcdef_" + "e".repeat(64) + ".bin"
        val r1 = t.observe(base + mapOf("files/status/tick.json" to "t2", "prefs/market_radar.json" to "p2", arch to "a1"),
            mapOf("prefs/market_radar.json" to prefs2))
        assertEquals(3, r1.changed); assertEquals(0, r1.reverted)
        assertTrue(r1.classes, r1.classes.contains("files/status/tick.json=1"))
        assertTrue(r1.classes, r1.classes.contains("files/cloud_history_outbox_v1/H_H.bin=1"))
        assertEquals(setOf("market_radar:ui", "market_radar:ui.scroll"), r1.prefKeys.split(",").toSet()) // key and JSON sub-key names only
        // The archive copy is pruned, then re-created with identical content: a revert, the A/B manifest flip.
        val r2 = t.observe(base + mapOf("files/status/tick.json" to "t1", "prefs/market_radar.json" to "p2"), mapOf("prefs/market_radar.json" to prefs2))
        assertTrue(r2.classes.contains(":removed")); assertEquals(1, r2.reverted) // tick.json back to t1
        val r3 = t.observe(base + mapOf("files/status/tick.json" to "t1", "prefs/market_radar.json" to "p2", arch to "a1"), emptyMap())
        assertEquals(1, r3.reverted)
        for (r in listOf(r1, r2, r3)) { val all = r.toJson().toString()
            assertFalse(all.contains("[]")); assertFalse(all.contains("scroll\":")); assertFalse(all.contains("2026-11-27")) }
        assertEquals("prefs/redacted", RecoveryDiagnostics.nameClass("prefs/auth_token_store.json"))
        assertEquals("files/research_m0/.../row_N.jsonl", RecoveryDiagnostics.nameClass("files/research_m0/2026-11-27/row_0001.jsonl"))
    }

    @Test fun signalCounterIsBoundedAndDrains() {
        val s = RecoveryDiagnostics.SignalCounter(maxKeys = 3)
        repeat(5) { s.add("prefs/market_radar:open_trades") }; s.add("files/status"); s.add("web/saveWeb"); s.add("files/x"); s.add("files/y")
        val text = s.drain()
        assertTrue(text, text.startsWith("prefs/market_radar:open_trades=5")); assertTrue(text, text.contains("other=2"))
        assertEquals("", s.drain())
    }
}
