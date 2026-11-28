package com.marketradar.app

import android.app.Application
import android.content.Context
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import java.io.File
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.attribute.BasicFileAttributes
import java.time.Instant
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong

/** 27 Nov R2 (Codex R1 "Tests and phone acceptance required"): the REAL CloudRecovery.runPass, end to end, against an
 * in-memory owner/device/revision server, on a representative inventory (1,224 files, ~21 MiB here; the phone has
 * 11,926 files / ~14.2 GiB, costs scale linearly with bytes). Measures bytes hashed and FULL-pass frequency per
 * scenario instead of gate strings. SYNTHETIC: wall/elapsed clocks are simulated; 1 tick = the 30 s scheduler. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], application = Application::class)
class CloudRecoveryLargeInventoryTest {
    private lateinit var c: Context
    private val wall = AtomicLong(1_795_770_000_000L) // 27 Nov 2026 ~16:00 IST
    private val mono = AtomicLong(10_000_000L)
    private var bg = false
    private lateinit var cloud: FakeCloud
    private var inventoryBytes = 0L

    /** Owner/device/revision semantics of mr_cloud_claim / mr_cloud_commit (lease 120 s), content-addressed objects. */
    inner class FakeCloud : CloudRecovery.Remote {
        val objects = ConcurrentHashMap<String, ByteArray>()
        @Volatile var revision = 0L; @Volatile var manifest: String? = null; @Volatile var device: String? = null
        @Volatile var leaseUntilWall = 0L
        val claims = AtomicInteger(); val commits = AtomicInteger(); val failPuts = AtomicInteger()
        override var renewLease: (() -> Unit)? = null
        override fun uid() = "owner-test"
        private fun row() = JSONObject().put("revision", revision).put("manifest_sha256", manifest ?: JSONObject.NULL)
            .put("device_id", device ?: JSONObject.NULL).put("committed_at", Instant.ofEpochMilli(wall.get()).toString())
        override fun head(): JSONObject? = if (manifest == null && revision == 0L) null else row()
        @Synchronized override fun rpc(name: String, body: JSONObject): JSONObject {
            val dev = body.getString("p_device")
            if (device != null && device != dev && leaseUntilWall > wall.get()) error("OTHER_DEVICE_ACTIVE")
            if (body.getLong("p_expected") != revision) error("CLOUD_REVISION_CONFLICT")
            when (name) {
                "mr_cloud_claim" -> claims.incrementAndGet()
                "mr_cloud_commit" -> { manifest = body.getString("p_manifest"); revision++; commits.incrementAndGet() }
                else -> error("UNKNOWN_RPC")
            }
            device = dev; leaseUntilWall = wall.get() + 120_000
            return row().put("lease_until", Instant.ofEpochMilli(leaseUntilWall).toString())
                .put("server_now", Instant.ofEpochMilli(wall.get()).toString())
                .put("_lease_deadline_elapsed", mono.get() + 115_000)
        }
        override fun put(hash: String, bytes: ByteArray) {
            renewLease?.invoke()
            if (failPuts.get() > 0 && failPuts.decrementAndGet() >= 0) throw java.io.IOException("simulated network drop")
            require(CloudVault.hash(bytes) == hash); objects.putIfAbsent(hash, bytes)
        }
        override fun get(hash: String): ByteArray = objects[hash] ?: error("CLOUD_HTTP_404")
    }

    private fun field(name: String) = CloudRecovery::class.java.getDeclaredField(name).apply { isAccessible = true }
    @Suppress("UNCHECKED_CAST")
    private fun stopObservers() { // a killed process's inotify watches die with it; events from earlier tests must not leak in
        (field("observers").get(CloudRecovery) as MutableMap<String, android.os.FileObserver>).let { m -> m.values.forEach { it.stopWatching() }; m.clear() }
    }
    private fun resetProcess() { // what a new process starts with
        stopObservers()
        field("ready").setBoolean(CloudRecovery, false); field("webVerified").setBoolean(CloudRecovery, false)
        field("leaseUntil").setLong(CloudRecovery, 0L); field("verifiedBaseline").set(CloudRecovery, null)
        field("state").set(CloudRecovery, "APP_SIGN_IN_REQUIRED"); field("legacyStarted").setBoolean(CloudRecovery, false)
    }

    private val originals = listOf(CloudRecovery.remoteFactory, CloudRecovery.signedIn, CloudRecovery.elapsed,
        CloudRecovery.wallClock, CloudRecovery.background)

    @Before fun setUp() {
        c = RuntimeEnvironment.getApplication()
        cloud = FakeCloud()
        CloudRecovery.remoteFactory = { cloud }
        CloudRecovery.signedIn = { true }
        CloudRecovery.elapsed = { mono.get() }
        CloudRecovery.wallClock = { wall.get() }
        CloudRecovery.background = { bg }
        CloudRecovery.legacySideEffects = false
        CloudRecovery.fileObservers = false // see CloudRecovery.fileObservers; detection then relies on signatures only
        CloudArchive.install(c.filesDir, c.noBackupFilesDir)
        resetProcess()
        c.filesDir.listFiles()?.forEach { it.deleteRecursively() }
        c.noBackupFilesDir.listFiles()?.forEach { it.deleteRecursively() }
        c.getSharedPreferences("cloud_vault_v1", Context.MODE_PRIVATE).edit().clear().commit()
        // Representative inventory: many small research rows plus multi-chunk files, Paper state, a pending raw capture.
        val rnd = java.util.Random(7)
        fun text(n: Int) = ByteArray(n) { (32 + rnd.nextInt(90)).toByte() }
        for (i in 0 until 1_200) File(c.filesDir, "research_m0/2026-11-%02d/row_%04d.jsonl".format(i % 27 + 1, i)).apply { parentFile!!.mkdirs(); writeBytes(text(8 * 1024)) }
        for (i in 0 until 22) File(c.filesDir, "evaluation_local_cache/brain_snapshots_2026-11-%02d.jsonl".format(i + 1)).apply { parentFile!!.mkdirs(); writeBytes(text(512 * 1024)) }
        File(c.filesDir, "single_leg_v1/positions.json").apply { parentFile!!.mkdirs(); writeText("""{"open":[{"id":"paper-1","qty":30}]}""") }
        File(c.filesDir, "status/tick.json").apply { parentFile!!.mkdirs(); writeText("{\"n\":0}") }
        File(c.filesDir, "${BnfRawCapture.DIRECTORY}/1_00000000-0000-0000-0000-000000000001.json").apply { parentFile!!.mkdirs(); writeText("{\"pending\":true}") }
        c.getSharedPreferences("market_radar", Context.MODE_PRIVATE).edit().putString("open_trades", "[{\"id\":\"paper-1\"}]").commit()
        assertTrue(CloudRecovery.saveWeb(c, """{"mr_ui":"{\"tab\":\"strategies\"}"}"""))
        inventoryBytes = c.filesDir.walkTopDown().filter { it.isFile && !it.path.contains(BnfRawCapture.DIRECTORY) }.sumOf { it.length() }
    }

    @After fun tearDown() {
        @Suppress("UNCHECKED_CAST") run {
            CloudRecovery.remoteFactory = originals[0] as (Context) -> CloudRecovery.Remote
            CloudRecovery.signedIn = originals[1] as (Context) -> Boolean
            CloudRecovery.elapsed = originals[2] as () -> Long
            CloudRecovery.wallClock = originals[3] as () -> Long
            CloudRecovery.background = originals[4] as () -> Boolean
        }
        CloudRecovery.legacySideEffects = true
        CloudRecovery.fileObservers = true
        CloudArchive.install(null)
        CloudArchive.resetForTest()
        resetProcess()
    }

    data class Pass(val mode: String, val hashed: Long, val budget: String, val gate: String, val outcome: String, val confirmed: Boolean)
    private fun lastPass(): Pass = JSONObject(CloudRecovery.status(c)).getJSONObject("diagnostics").getJSONObject("last_pass").let {
        Pass(it.getString("mode"), it.getLong("bytes_hashed"), it.optString("budget"), it.optString("gate_reason"), it.getString("outcome"), it.getBoolean("commit_confirmed")) }
    private fun tick(trigger: String = "scheduled", seconds: Long = 30, mutate: Boolean = false): Pass {
        wall.addAndGet(seconds * 1000); mono.addAndGet(seconds * 1000)
        if (mutate) File(c.filesDir, "status/tick.json").writeText("{\"n\":${wall.get()}}")
        CloudRecovery.runPass(c, trigger)
        return lastPass()
    }
    private fun initialBackup(): Pass = tick("worker").also {
        assertEquals("$it ${CloudRecovery.status(c)}", "FULL", it.mode); assertTrue("$it ${CloudRecovery.status(c)}", it.confirmed)
        assertTrue("monitor ready after verified checkpoint", CloudRecovery.canMonitor())
        assertEquals("VERIFIED_CHECKPOINT", JSONObject(CloudRecovery.status(c)).getString("state"))
    }
    private fun report(name: String, passes: List<Pass>) {
        val full = passes.count { it.mode == "FULL" }
        val hashed = passes.sumOf { it.hashed }
        println("R2_MEASURE scenario=$name ticks=${passes.size} full=$full skip=${passes.count { it.mode == "SKIP" }} " +
            "defer=${passes.count { it.mode == "DEFER" }} bytes_hashed=$hashed inventory_bytes=$inventoryBytes " +
            "hashed_per_full=${if (full == 0) 0 else hashed / full} legacy_model_bytes=${passes.size.toLong() * 3 * inventoryBytes} " +
            "claims=${cloud.claims.get()} commits=${cloud.commits.get()}")
    }
    private fun assertPendingWorkIntact() {
        assertTrue(File(c.filesDir, "single_leg_v1/positions.json").readText().contains("paper-1"))
        assertTrue(File(c.filesDir, "${BnfRawCapture.DIRECTORY}/1_00000000-0000-0000-0000-000000000001.json").exists())
        assertEquals("[{\"id\":\"paper-1\"}]", c.getSharedPreferences("market_radar", Context.MODE_PRIVATE).getString("open_trades", null))
        assertEquals(1_200, File(c.filesDir, "research_m0").walkTopDown().count { it.isFile && it.name.startsWith("row_") })
    }

    @Test fun unchangedInventorySkipsHashingWhileTheLeaseIsRenewedEveryTick() {
        val first = initialBackup()
        assertTrue("a FULL pass hashes the inventory exactly twice", first.hashed in 2 * inventoryBytes..2 * inventoryBytes + 64 * 1024)
        val claims0 = cloud.claims.get()
        val passes = (1..40).map { tick().also { assertTrue(CloudRecovery.canMonitor()) } }
        report("unchanged_20min", passes)
        assertTrue(passes.all { it.mode == "SKIP" && it.hashed == 0L })
        assertEquals(40, cloud.claims.get() - claims0) // ownership/revision re-checked and lease renewed every 30 s
        assertEquals(1, cloud.commits.get())
    }

    @Test fun tinyFileChangingEvery30sCausesAtMostOneFullPassPer5MinutesNotEveryTick() {
        initialBackup()
        val claims0 = cloud.claims.get()
        val passes = (1..60).map { tick(mutate = true).also { assertTrue("monitoring never lapses", CloudRecovery.canMonitor()) } }
        report("mutation_every_30s_foreground_30min", passes)
        val full = passes.count { it.mode == "FULL" }
        assertTrue("full=$full", full in 5..6) // legacy: 60 (one per tick)
        assertTrue(passes.filter { it.mode == "DEFER" }.all { it.hashed == 0L && it.budget.startsWith("DEFER_COOLDOWN_") })
        assertTrue(passes.sumOf { it.hashed } <= full * (2 * inventoryBytes + 64 * 1024))
        assertEquals(60, cloud.claims.get() - claims0 - full) // + one extra claim inside each committing FULL pass
        assertEquals(1 + full, cloud.commits.get())          // every FULL pass still commits the change, verified
        assertTrue(passes.filter { it.mode == "FULL" }.all { it.confirmed })
        // Maximum backup delay observed: no change waits more than one cooldown (5 min = 10 ticks) + 1 tick.
        var sinceFull = 0; var worst = 0
        passes.forEach { if (it.mode == "FULL") sinceFull = 0 else { sinceFull++; worst = maxOf(worst, sinceFull) } }
        assertTrue("worst wait $worst ticks", worst <= 10)
        assertPendingWorkIntact()
    }

    @Test fun cachedProcessOnlyHashesOnTheWorkManagerJob() {
        initialBackup()
        bg = true
        val passes = (1..60).map { i -> tick(trigger = if (i % 30 == 0) "worker" else "scheduled", mutate = true) }
        report("mutation_every_30s_cached_30min", passes)
        assertEquals(listOf(30, 60), passes.withIndex().filter { it.value.mode == "FULL" }.map { it.index + 1 })
        assertTrue(passes.filter { it.mode != "FULL" }.all { it.hashed == 0L })
        assertTrue(passes.any { it.budget == "DEFER_BACKGROUND_UNTIL_WORKER" })
        assertTrue(CloudRecovery.canMonitor()) // lease still renewed by the deferred passes
        assertPendingWorkIntact()
    }

    @Test fun failedUploadIsRetriedAfterTheCooldownWithoutLosingPendingWork() {
        initialBackup()
        repeat(9) { tick() } // quiet until the cooldown after the initial backup has elapsed
        val commits0 = cloud.commits.get()
        File(c.filesDir, "research_m0/new_batch.jsonl").writeText("x".repeat(300_000))
        cloud.failPuts.set(1)
        val failed = tick(mutate = true)
        assertEquals("FULL", failed.mode); assertFalse(failed.confirmed)
        assertEquals(commits0, cloud.commits.get())             // no partial checkpoint
        assertTrue(JSONObject(CloudRecovery.status(c)).getString("state").startsWith("PENDING_OR_BLOCKED"))
        val retries = (1..12).map { tick() }
        report("failure_then_retry", listOf(failed) + retries)
        val firstRetry = retries.indexOfFirst { it.mode == "FULL" }
        assertTrue("retry waits for the cooldown, got tick ${firstRetry + 1}", firstRetry + 1 >= 10)
        assertTrue(retries[firstRetry].confirmed)
        assertEquals(commits0 + 1, cloud.commits.get())
        assertTrue(File(c.filesDir, "research_m0/new_batch.jsonl").exists())
        assertPendingWorkIntact()
    }

    @Test fun processRestartDoesNotRestartTheCooldownAndMonitoringResumesWithoutHashing() {
        initialBackup()
        tick(mutate = true) // within cooldown => deferred
        resetProcess()       // process killed (e.g. EXCESSIVE_RESOURCE_USAGE) and restarted
        val first = tick()
        assertEquals("DEFER", first.mode); assertEquals(0L, first.hashed)
        assertEquals("NO_VERIFIED_BASELINE", first.gate)
        assertTrue("same-phone verified checkpoint resumes monitoring", CloudRecovery.canMonitor())
        val later = (1..9).map { tick() }
        report("restart_inside_cooldown", listOf(first) + later)
        assertEquals(1, later.count { it.mode == "FULL" })
        assertPendingWorkIntact()
    }

    @Test fun scrubRunsAtMostOncePerSixHoursOnAnUnchangedInventory() {
        initialBackup()
        val passes = (1..42).map { tick(seconds = 600) } // 7 h in 10-minute ticks
        report("unchanged_7h_scrub", passes)
        assertEquals(1, passes.count { it.mode == "FULL" && it.gate == "SCRUB_DUE" })
        assertEquals(1, passes.count { it.mode == "FULL" })
    }

    @Test fun deferredPassesStillDetectAnotherDeviceAndStopMonitoringWithoutCommitting() {
        initialBackup()
        tick(mutate = true) // deferred
        // Another phone claimed and committed (same verified manifest object, next revision) and holds the lease.
        cloud.revision += 1; cloud.device = "other-phone"; cloud.leaseUntilWall = wall.get() + 300_000
        val commits0 = cloud.commits.get()
        val p = tick(mutate = true)
        assertFalse(CloudRecovery.canMonitor())
        assertEquals(commits0, cloud.commits.get())
        assertTrue(p.outcome, p.outcome.contains("CONFLICT") || p.outcome.contains("OTHER_DEVICE"))
        assertPendingWorkIntact()
    }

    @Test fun archivedFilesArePrunedOnlyAfterAConfirmedCheckpointThatStillReferencesThem() {
        initialBackup()
        val dir = File(c.filesDir, CloudArchive.DIRECTORY).apply { mkdirs() }
        File(dir, "a_1.bin").writeText("archived-history")
        File(dir, "a_1.json").writeText("{\"source_path\":\"x\"}")
        (1..10).forEach { tick() } // reach the end of the cooldown
        assertFalse(File(dir, "a_1.bin").exists())
        val manifest = JSONObject(String(cloud.objects.getValue(cloud.manifest!!)))
        assertTrue(manifest.getJSONObject("entries").has("files/${CloudArchive.DIRECTORY}/a_1.bin")) // remote reference kept
        val commitsAfterPrune = cloud.commits.get()
        // The post-prune baseline is the remaining signature: an unchanged inventory does not scan again,
        // and a later FULL (none here) would republish the same archive references, not an A/B flip.
        val after = (1..30).map { tick() }
        report("archive_prune", after)
        assertEquals(0, after.count { it.mode == "FULL" })
        assertTrue(after.all { it.mode == "SKIP" && it.hashed == 0L })
        assertEquals(commitsAfterPrune, cloud.commits.get())
        assertTrue(JSONObject(String(cloud.objects.getValue(cloud.manifest!!))).getJSONObject("entries")
            .has("files/${CloudArchive.DIRECTORY}/a_1.bin"))
        assertPendingWorkIntact()
    }

    @Test fun interruptedPruneResumesWithoutAFullScanAndKeepsRemoteReferences() {
        initialBackup()
        val dir = File(c.filesDir, CloudArchive.DIRECTORY).apply { mkdirs() }
        File(dir, "a_1.bin").writeText("archived-history")
        File(dir, "a_1.json").writeText("{\"source_path\":\"x\"}")
        (1..10).forEach { tick() }
        assertFalse(File(dir, "a_1.bin").exists())
        // Simulate a crash after only one of the two acknowledged files was removed.
        File(dir, "a_1.bin").writeText("archived-history")
        val commits0 = cloud.commits.get()
        val p = tick()
        assertEquals(0L, p.hashed)
        assertFalse(File(dir, "a_1.bin").exists())
        assertEquals(commits0, cloud.commits.get())
        val later = (1..10).map { tick() }
        report("interrupted_prune_resume", listOf(p) + later)
        assertTrue(later.all { it.mode == "SKIP" && it.hashed == 0L })
        assertTrue(JSONObject(String(cloud.objects.getValue(cloud.manifest!!))).getJSONObject("entries")
            .has("files/${CloudArchive.DIRECTORY}/a_1.bin"))
        assertPendingWorkIntact()
    }

    @Test fun persistentWebCaptureFailureDoesNotRescanEveryTickAndRecoversWhenTheFlagClears() {
        initialBackup()
        c.getSharedPreferences("cloud_vault_v1", Context.MODE_PRIVATE).edit().putBoolean("web_capture_failed", true).commit()
        val first = tick()
        assertEquals("FULL", first.mode) // one immediate attempt after the prerequisite appears
        val rest = (1..9).map { tick() }
        report("persistent_web_capture_failed", listOf(first) + rest)
        assertTrue(rest.all { it.mode == "DEFER" && it.hashed == 0L && it.budget.startsWith("DEFER_COOLDOWN_") })
        assertPendingWorkIntact()
        // After the cooldown a single bounded retry is allowed, then backoff resumes.
        val retryWindow = (1..10).map { tick() }
        assertEquals(1, retryWindow.count { it.mode == "FULL" })
        assertEquals("FULL", retryWindow.first().mode)
        assertTrue(retryWindow.drop(1).all { it.mode == "DEFER" && it.hashed == 0L })
        c.getSharedPreferences("cloud_vault_v1", Context.MODE_PRIVATE).edit().putBoolean("web_capture_failed", false).commit()
        val recovered = tick(mutate = true)
        assertEquals("FULL", recovered.mode)
        assertTrue(recovered.confirmed)
        assertPendingWorkIntact()
    }

    @Test fun storageWalkRunsOffTheRecoveryMonitorAndReportsIncompleteWalks() {
        initialBackup()
        val many = File(c.filesDir, "many").apply { mkdirs() }
        for (i in 0 until 5_000) File(many, "f$i").writeText("x")
        val started = System.nanoTime()
        assertTrue(CloudRecovery.requestStorageWalk(c))
        val passes = (1..3).map { tick() } // runPass is not blocked by the walk
        val returnedMs = (System.nanoTime() - started) / 1_000_000
        var storage: JSONObject? = null
        repeat(200) { if (storage == null) { Thread.sleep(50); storage = JSONObject(CloudRecovery.status(c)).getJSONObject("diagnostics").optJSONObject("storage") } }
        assertNotNull(storage)
        val files = storage!!.getJSONObject("files")
        assertFalse(files.getBoolean("incomplete")); assertEquals(0, files.getInt("failures"))
        assertTrue(files.getLong("elapsed_ms") < RecoveryDiagnostics.WALK_BUDGET_MS)
        println("R2_MEASURE scenario=storage_walk files=${files.getLong("files")} walk_ms=${files.getLong("elapsed_ms")} passes_meanwhile=${passes.size} returned_ms=$returnedMs")
        assertTrue(CloudRecovery.canMonitor())
    }

    @Test fun logsTabExportCarriesABoundedDiagnosticSnapshotAndAFreshStorageBreakdown() {
        initialBackup()
        tick(mutate = true) // a deferred pass, so the snapshot shows the budget decision
        fun lines(prefix: String) = com.marketradar.app.util.LogBuffer.snapshot(null).filter { it.tag == "CloudRecovery" && it.message.startsWith(prefix) }
        val before = lines("RECOVERY_DIAG_SNAPSHOT").size
        CloudRecovery.exportDiagnostics(c)            // what NativeBridge.getLogBuffer now does first
        CloudRecovery.exportDiagnostics(c)            // a Logs-tab refresh within the minute adds nothing
        val snaps = lines("RECOVERY_DIAG_SNAPSHOT")
        assertEquals(before + 1, snaps.size)
        val json = JSONObject(snaps.first().message.removePrefix("RECOVERY_DIAG_SNAPSHOT "))
        assertEquals("mr.recovery_diag.v1", json.getString("schema"))
        assertEquals("DEFER", json.getJSONObject("last_pass").getString("mode"))
        assertTrue(snaps.first().message.length <= 12_000 + 64)
        var waited = 0
        while (lines("STORAGE_BREAKDOWN root=files").isEmpty() && waited++ < 200) Thread.sleep(25)
        val storage = lines("STORAGE_BREAKDOWN root=files").first().message
        assertTrue(storage, storage.contains("incomplete=no") && storage.contains("research_m0="))
        assertFalse(storage.contains("row_")); assertFalse(storage.contains(c.filesDir.absolutePath))
        assertTrue(lines("RAW_OUTBOX_STATS").first().message.contains("pending=1"))
        mono.addAndGet(61_000)
        CloudRecovery.exportDiagnostics(c)
        assertEquals(before + 2, lines("RECOVERY_DIAG_SNAPSHOT").size)
        val about = CloudRecovery.aboutSummary(c)
        assertTrue(about, about.contains("Recovery: rev 1") && about.contains("last pass: DEFER") && about.contains("files dir: "))
    }

    /** Same-length rewrite with the original mtime restored: the stat signature is unchanged, so only the mutation counter is dirty. */
    private fun rewriteKeepingSignature(file: File, newBytes: ByteArray) {
        val path = file.toPath()
        val before = Files.readAttributes(path, BasicFileAttributes::class.java, LinkOption.NOFOLLOW_LINKS)
        assertEquals(before.size(), newBytes.size.toLong())
        val sig = RecoveryPassGate.fileSignature(file)
        file.writeBytes(newBytes)
        Files.setLastModifiedTime(path, before.lastModifiedTime())
        assertEquals("signature must stay the same so only the mutation signal is dirty", sig, RecoveryPassGate.fileSignature(file))
    }

    @Test fun sameLengthRewriteWithRestoredMtimeCannotSkipAndIsLaterVerified() {
        initialBackup()
        val file = File(c.filesDir, "status/tick.json")
        val original = file.readBytes()
        val updated = ByteArray(original.size) { i -> if (i == 0) (original[0] + 1).toByte() else original[i] }
        rewriteKeepingSignature(file, updated)
        CloudRecovery.deliverMutation("files/status")
        val first = tick()
        assertNotEquals(RecoveryPassGate.SKIP, first.gate)
        assertNotEquals("SKIP", first.mode)
        val later = (1..12).map { tick() }
        val confirmed = (listOf(first) + later).firstOrNull { it.mode == "FULL" && it.confirmed }
        assertNotNull(confirmed)
        report("same_length_mtime_restored", listOf(first) + later)
        val entries = JSONObject(String(cloud.objects.getValue(cloud.manifest!!))).getJSONObject("entries")
        assertEquals(CloudVault.hash(updated), entries.getJSONObject("files/status/tick.json").getString("sha256"))
        assertPendingWorkIntact()
    }

    @Test fun unrelatedWriteDuringPruneStaysDirty() {
        initialBackup()
        val dir = File(c.filesDir, CloudArchive.DIRECTORY).apply { mkdirs() }
        File(dir, "a_1.bin").writeText("archived-history")
        File(dir, "a_1.json").writeText("{\"source_path\":\"x\"}")
        (1..10).forEach { tick() }
        assertFalse(File(dir, "a_1.bin").exists())
        File(dir, "a_1.bin").writeText("archived-history") // leftover, as if prune was interrupted
        val file = File(c.filesDir, "status/tick.json")
        val original = file.readBytes()
        val updated = ByteArray(original.size) { i -> if (i == 0) (original[0] + 2).toByte() else original[i] }
        CloudArchive.testDuringPrune = {
            rewriteKeepingSignature(file, updated)
            CloudRecovery.deliverMutation("files/status")
        }
        val first = tick()
        assertFalse(File(dir, "a_1.bin").exists()) // prune still finished
        assertNotEquals(RecoveryPassGate.SKIP, first.gate)
        assertNotEquals("SKIP", first.mode)
        val later = (1..12).map { tick() }
        val confirmed = (listOf(first) + later).firstOrNull { it.mode == "FULL" && it.confirmed }
        assertNotNull(confirmed)
        report("unrelated_write_during_prune", listOf(first) + later)
        val entries = JSONObject(String(cloud.objects.getValue(cloud.manifest!!))).getJSONObject("entries")
        assertEquals(CloudVault.hash(updated), entries.getJSONObject("files/status/tick.json").getString("sha256"))
        assertTrue(entries.has("files/${CloudArchive.DIRECTORY}/a_1.bin"))
        assertPendingWorkIntact()
    }
}
