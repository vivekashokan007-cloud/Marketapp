package com.marketradar.app

import android.content.Context
import android.content.SharedPreferences
import android.os.FileObserver
import androidx.work.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.time.OffsetDateTime
import java.util.UUID
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicBoolean
import com.marketradar.app.util.LogBuffer

/** Cloud backing for native files, typed preferences and PWA localStorage.
 * Local files remain the working cache/outbox; never delete unacknowledged work.
 * Startup hydration precedes every legacy worker/monitor. No network result is
 * represented as a verified checkpoint until both object and head readbacks pass.
 */
object CloudRecovery {
    private const val PREFS = "cloud_vault_v1"
    private val executor = Executors.newSingleThreadScheduledExecutor()
    private val started = AtomicBoolean(false)
    @Volatile private var ready = false
    @Volatile private var leaseUntil = 0L
    @Volatile private var state = "APP_SIGN_IN_REQUIRED"
    @Volatile private var legacyStarted = false
    @Volatile private var webVerified = false
    @Volatile private var filesChecked = 0
    @Volatile private var filesTotal = 0
    private val mutations = AtomicLong()
    private val observers = mutableMapOf<String, FileObserver>()
    private val preferenceListeners = mutableListOf<SharedPreferences.OnSharedPreferenceChangeListener>()
    // 27 Nov diagnostics: counts/bytes/durations only (see RecoveryDiagnostics).
    private val wakeups = RecoveryDiagnostics.Wakeups()
    private val storageWalk = RecoveryDiagnostics.Every(6 * 60 * 60 * 1000L)
    private val skipLog = RecoveryDiagnostics.Every(10 * 60 * 1000L) // quiet passes: one stats line per 10 min
    @Volatile private var currentPass: RecoveryDiagnostics.PassStats? = null
    @Volatile private var lastPass: JSONObject? = null
    @Volatile private var lastStorage: JSONObject? = null
    // Set only by a FULL pass that ended VERIFIED_CHECKPOINT with no mutation; cleared on any failure.
    @Volatile private var verifiedBaseline: RecoveryPassGate.Baseline? = null
    private fun changed() {
        mutations.incrementAndGet()
        wakeups.mutationSignals.incrementAndGet()
        if (ready) state = "LOCAL_CHANGES_PENDING_UPLOAD"
    }
    private fun observeFiles(c: Context) {
        observers.values.forEach { it.stopWatching() }
        observers.clear()
        c.filesDir.walkTopDown().onEnter { dir -> dir == c.filesDir ||
            CloudDataPolicy.portablePath(dir.relativeTo(c.filesDir).invariantSeparatorsPath) }
            .filter { it.isDirectory }.forEach { directory ->
                if (observers.containsKey(directory.absolutePath)) return@forEach
                val observer = object: FileObserver(directory.absolutePath,
                    FileObserver.CLOSE_WRITE or FileObserver.MOVED_TO or FileObserver.MOVED_FROM or FileObserver.DELETE or FileObserver.CREATE) {
                    override fun onEvent(event: Int, path: String?) {
                        if (path == null) return
                        val relative = File(directory, path).relativeTo(c.filesDir).invariantSeparatorsPath
                        if (CloudDataPolicy.portablePath(relative)) changed()
                    }
                }
                observers[directory.absolutePath] = observer
                observer.startWatching()
            }
    }
    private fun meta(c: Context) = c.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
    fun owner(c: Context): String = meta(c).getString("owner", "") ?: ""
    /** A normal in-place update must not wait for the entire file inventory/upload
     * before showing this phone's already verified Paper state. This is a UI gate
     * only: canMonitor still requires a fresh cloud lease and verified web state.
     * A new device or interrupted restore continues through CloudSetupActivity.
     */
    fun canOpen(c: Context): Boolean {
        if (File(c.noBackupFilesDir, "cloud_restore_pending").exists()) return false
        if (ready) return true
        val m = meta(c)
        return m.getBoolean("initialized", false) && owner(c).isNotBlank() &&
            m.getLong("revision", 0L) > 0L &&
            m.getString("verified_at", "").orEmpty().isNotBlank() &&
            CloudVault.validHash(m.getString("checkpoint_sha256", "").orEmpty())
    }
    fun openLocalPaperCache(c: Context): Boolean {
        if (File(c.noBackupFilesDir, "cloud_restore_pending").exists()) return false
        ready = true; leaseUntil = 0; webVerified = false
        state = "LOCAL_PAPER_CACHE_NOT_BACKED_UP"
        return true
    }
    fun canMonitor(): Boolean = ready && webVerified && leaseUntil > android.os.SystemClock.elapsedRealtime() + 5000
    fun status(c: Context): String = JSONObject().put("state", if (meta(c).getBoolean("web_capture_failed", false)) "WEB_STATE_CAPTURE_FAILED" else state).put("ready", ready)
        .put("monitor_ready", canMonitor()).put("verified_at", meta(c).getString("verified_at", ""))
        .put("revision", meta(c).getLong("revision", 0)).put("last_error", meta(c).getString("last_error", ""))
        .put("files_checked", filesChecked).put("files_total", filesTotal)
        .put("verified_files", meta(c).getInt("verified_files", 0)).put("verified_bytes", meta(c).getLong("verified_bytes", 0))
        .put("checkpoint_sha256", meta(c).getString("checkpoint_sha256", ""))
        .put("scope", "native files + preferences + PWA state; broker login stays on device")
        .put("web_state_captured", c.getSharedPreferences("cloud_web_cache_v1", Context.MODE_PRIVATE).contains("storage"))
        .put("local_cache", true)
        .put("market_source_upload", c.getSharedPreferences("bnf_source_upload_status", Context.MODE_PRIVATE).let { p ->
            JSONObject().put("state", p.getString("state", "NOT_CHECKED"))
                .put("verified_rows", p.getInt("verified", 0)).put("pending_rows", p.getInt("pending", 0))
                .put("checked_at_ms", p.getLong("checked_at_ms", 0))
        })
        .put("diagnostics", JSONObject().put("last_pass", lastPass ?: JSONObject.NULL)
            .put("wakeups", wakeups.toJson()).put("storage", lastStorage ?: JSONObject.NULL)).toString()

    fun start(context: Context) {
        // Never acquire runPass's monitor from an Activity. It can be held by
        // a long initial backup; repeated startup calls only need this gate.
        if (!started.compareAndSet(false, true)) return
        val c = context.applicationContext
        for (name in listOf("market_radar", "ev_v12_acceptance", "cloud_web_cache_v1")) {
            val listener = SharedPreferences.OnSharedPreferenceChangeListener { _, key ->
                if (key == null || CloudDataPolicy.portablePreference(key)) changed()
            }
            preferenceListeners.add(listener)
            c.getSharedPreferences(name, Context.MODE_PRIVATE).registerOnSharedPreferenceChangeListener(listener)
        }
        observeFiles(c)
        executor.scheduleWithFixedDelay({ runPass(c, "scheduled") }, 0, 30, TimeUnit.SECONDS)
        val work = PeriodicWorkRequestBuilder<CloudRecoveryWorker>(15, TimeUnit.MINUTES)
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()).build()
        WorkManager.getInstance(c).enqueueUniquePeriodicWork("mr_cloud_recovery_v1", ExistingPeriodicWorkPolicy.KEEP, work)
    }
    fun request(c: Context) { executor.execute { runPass(c.applicationContext, "request") } }
    /** The PWA calls this synchronously after every localStorage mutation. Never takes a credential. */
    fun saveWeb(c: Context, raw: String): Boolean = try {
        require(raw.toByteArray().size <= 4 * 1024 * 1024) { "PWA_STATE_TOO_LARGE" }
        val clean = CloudDataPolicy.cleanStorage(JSONObject(raw)).toString()
        val prefs = c.getSharedPreferences("cloud_web_cache_v1", Context.MODE_PRIVATE)
        if (meta(c).getBoolean("web_capture_failed", false)) meta(c).edit().putBoolean("web_capture_failed", false).commit()
        if (prefs.getString("storage", null) == clean) true else {
            changed()
            require(prefs.edit().putString("storage", clean).commit()) { "WEB_CACHE_WRITE_FAILED" }
            true
        }
    } catch (_: Exception) {
        changed(); webVerified = false
        meta(c).edit().putBoolean("web_capture_failed", true).commit()
        false
    }
    fun web(c: Context): String = c.getSharedPreferences("cloud_web_cache_v1", Context.MODE_PRIVATE).getString("storage", "null") ?: "null"

    private class Transport(c: Context): CloudVault.Remote {
        var renewLease: (() -> Unit)? = null
        private val cache = File(c.noBackupFilesDir, "cloud_manifest_cache")
        private val auth = CloudSession(c)
        private val authLock = Any()
        private var sessionCache: JSONObject? = null
        private fun currentSession(): JSONObject = synchronized(authLock) {
            val cached = sessionCache
            if (cached != null && cached.optLong("expires_at") > System.currentTimeMillis()/1000 + 120) cached
            else auth.session().also { sessionCache = it }
        }
        private val client = OkHttpClient.Builder().connectTimeout(20, TimeUnit.SECONDS).readTimeout(60, TimeUnit.SECONDS)
            .authenticator { _, response ->
                // One server-driven refresh covers clock skew without an authentication retry loop.
                if (response.priorResponse != null) null else synchronized(authLock) {
                    val current = currentSession().getString("access_token")
                    val token = if (response.request.header("Authorization") != "Bearer $current") current
                        else auth.refreshAccessToken().also { sessionCache = auth.session() }
                    response.request.newBuilder().header("Authorization", "Bearer $token").build()
                }
            }.build()
        private fun request(path: String): Request.Builder {
            val s = currentSession()
            return Request.Builder().url(BuildConfig.SUPABASE_URL + path).header("apikey", BuildConfig.SUPABASE_ANON_KEY)
                .header("Authorization", "Bearer " + s.getString("access_token"))
        }
        fun uid(): String = currentSession().getJSONObject("user").getString("id")
        fun head(): JSONObject? {
            val req = request("/rest/v1/mr_cloud_heads?select=*&owner_id=eq." + uid()).get().build()
            val bytes = execute(req, 64 * 1024)
            val rows = JSONArray(String(bytes, Charsets.UTF_8))
            return if (rows.length() == 0) null else rows.getJSONObject(0)
        }
        fun rpc(name: String, body: JSONObject): JSONObject {
            val sent = android.os.SystemClock.elapsedRealtime()
            val response = JSONObject(String(execute(request("/rest/v1/rpc/$name")
                .post(body.toString().toRequestBody("application/json".toMediaType())).build(), 64 * 1024), Charsets.UTF_8))
            return response.put("_lease_deadline_elapsed", CloudLeaseClock.deadline(response, sent, android.os.SystemClock.elapsedRealtime()))
        }
        override fun put(hash: String, bytes: ByteArray) {
            renewLease?.invoke()
            require(CloudVault.validHash(hash))
            val req = request("/storage/v1/object/mr-private-state/${uid()}/$hash")
                .post(bytes.toRequestBody("application/octet-stream".toMediaType())).build()
            client.newCall(req).execute().use { r ->
                if (!r.isSuccessful) {
                    // Storage sometimes returns 400 with a 409 status in the JSON body.
                    val body = r.body?.string()?.take(4096).orEmpty()
                    val code = try { JSONObject(body).optString("statusCode") } catch (_: Exception) { "" }
                    require(r.code == 409 || code == "409") { "CLOUD_UPLOAD_HTTP_${r.code}" }
                }
            }
        }
        override fun get(hash: String): ByteArray {
            renewLease?.invoke()
            require(CloudVault.validHash(hash))
            if (cache.exists()) {
                val cached = cache.readBytes()
                if (CloudVault.hash(cached) == hash) return cached
            }
            val bytes = execute(request("/storage/v1/object/authenticated/mr-private-state/${uid()}/$hash").get().build(), CloudVault.MAX_MANIFEST)
            if (bytes.firstOrNull() == '{'.code.toByte() && CloudVault.hash(bytes) == hash) {
                val tmp = File(cache.parentFile, cache.name + ".tmp"); tmp.writeBytes(bytes); require(tmp.renameTo(cache))
            }
            return bytes
        }
        private fun execute(req: Request, max: Int): ByteArray = client.newCall(req).execute().use { r ->
            if (!r.isSuccessful) {
                val errorBody = r.body?.string()?.take(4096).orEmpty()
                val reason = listOf("CLOUD_REVISION_CONFLICT", "OTHER_DEVICE_ACTIVE", "AUTH_REQUIRED").firstOrNull { errorBody.contains(it) }
                error(reason ?: "CLOUD_HTTP_${r.code}")
            }
            val out = java.io.ByteArrayOutputStream()
            r.body?.byteStream()?.use { input ->
                val buffer = ByteArray(8192)
                while (true) { val n = input.read(buffer); if (n < 0) break
                    require(out.size() + n <= max) { "CLOUD_RESPONSE_TOO_LARGE" }; out.write(buffer, 0, n) }
            }
            out.toByteArray()
        }
    }
    private fun preferenceFiles(c: Context): List<String> = File(c.applicationInfo.dataDir, "shared_prefs")
        .listFiles()?.map { it.name.removeSuffix(".xml") }?.filter {
            it.endsWith(".bak").not() && CloudDataPolicy.portablePreferenceFile(it)
        }?.distinct()?.sorted() ?: emptyList()
    private fun typedPreferences(c: Context, name: String): ByteArray {
        val values = JSONObject()
        c.getSharedPreferences(name, Context.MODE_PRIVATE).all.toSortedMap().forEach { (key, value) ->
            if (CloudDataPolicy.portablePreference(key)) {
                val type = when (value) { is String -> "string"; is Long -> "long"; is Int -> "int";
                    is Boolean -> "bool"; is Float -> "float"; is Set<*> -> "set"; else -> error("UNKNOWN_PREF_TYPE") }
                var v: Any? = if (value is Set<*>) JSONArray(value.sortedBy { it.toString() }) else value
                if (v is String) {
                    val parsed = try { org.json.JSONTokener(v).nextValue() } catch (_: Exception) { null }
                    if (parsed is JSONObject || parsed is JSONArray) v = CloudDataPolicy.clean(parsed).toString()
                }
                values.put(key, JSONObject().put("type", type).put("value", v))
            }
        }
        return values.toString().toByteArray()
    }
    private fun restorePreferences(c: Context, name: String, file: File) {
        require(name.matches(Regex("[A-Za-z0-9_.-]+")) && name != PREFS)
        val prefs = c.getSharedPreferences(name, Context.MODE_PRIVATE)
        val editor = prefs.edit()
        prefs.all.keys.filter { CloudDataPolicy.portablePreference(it) }.forEach { editor.remove(it) }
        val values = JSONObject(file.readText())
        values.keys().forEach { key ->
            if (!CloudDataPolicy.portablePreference(key)) return@forEach
            val v = values.getJSONObject(key)
            when (v.getString("type")) {
                "string" -> editor.putString(key, v.getString("value"))
                "long" -> editor.putLong(key, v.getLong("value"))
                "int" -> editor.putInt(key, v.getInt("value"))
                "bool" -> editor.putBoolean(key, v.getBoolean("value"))
                "float" -> editor.putFloat(key, v.getDouble("value").toFloat())
                "set" -> { val a = v.getJSONArray("value"); editor.putStringSet(key, (0 until a.length()).map { a.getString(it) }.toSet()) }
                else -> error("UNSUPPORTED_PREF_TYPE")
            }
        }
        if (name == "market_radar") editor.putBoolean("service_running", false).putString("execution_mode", "paper")
            .putBoolean("execution_sandbox_enabled", false)
        require(editor.commit()) { "PREFERENCE_RESTORE_FAILED" }
    }
    private data class Source(val path: String, val bytes: ByteArray? = null, val file: File? = null) {
        fun stream() = bytes?.inputStream() ?: file!!.inputStream()
        fun fingerprint(stats: RecoveryDiagnostics.PassStats? = null): String { val md = MessageDigest.getInstance("SHA-256")
            var read = 0L
            stream().use { input -> val b = ByteArray(32768); while (true) { val n = input.read(b); if (n < 0) break; md.update(b, 0, n); read += n } }
            stats?.bytesHashed?.addAndGet(read); stats?.filesHashed?.incrementAndGet()
            return md.digest().joinToString("") { "%02x".format(it) }
        }
    }
    private fun inventory(c: Context): List<Source> {
        val files = c.filesDir.walkTopDown().onEnter { dir ->
            dir == c.filesDir || CloudDataPolicy.portablePath(dir.relativeTo(c.filesDir).invariantSeparatorsPath)
        }.filter { it.isFile }.mapNotNull { file ->
            val path = file.relativeTo(c.filesDir).invariantSeparatorsPath
            if (!CloudDataPolicy.portablePath(path)) null else {
                require(CloudDataPolicy.ownedFile(c.filesDir, file)) { "SYMLINK_NOT_BACKED_UP" }
                Source("files/$path", file = file)
            }
        }.toList()
        return (files + preferenceFiles(c).map { Source("prefs/$it.json", bytes = typedPreferences(c, it)) }).sortedBy { it.path }
    }
    private fun signature(sources: List<Source>): Map<String, String> = sources.associate { source ->
        source.path to (source.file?.let { RecoveryPassGate.fileSignature(it) } ?: RecoveryPassGate.bytesSignature(source.bytes!!))
    }.toSortedMap()
    private fun fingerprints(sources: List<Source>, heartbeat: () -> Unit = {}) =
        currentPass.let { stats -> sources.associate { heartbeat(); it.path to it.fingerprint(stats) }.toSortedMap() }
    private fun manifestFingerprints(manifest: JSONObject): Map<String, String> {
        val entries = manifest.getJSONObject("entries")
        return entries.keys().asSequence().associateWith { entries.getJSONObject(it).getString("sha256") }.toSortedMap()
    }
    private fun applyStage(c: Context, stage: File, manifest: JSONObject) {
        val entries = manifest.getJSONObject("entries")
        val obsolete = inventory(c).filter { !entries.has(it.path) && it.file != null }.map { it.file!! }
        CloudRestoreTransaction(c.filesDir, c.noBackupFilesDir).activate(stage, entries, obsolete,
            preferences = { name, file -> restorePreferences(c, name, file) })
    }
    @Synchronized fun runPass(c: Context, trigger: String = "worker") {
        val stats = RecoveryDiagnostics.PassStats(trigger) { android.os.SystemClock.elapsedRealtime() }
        currentPass = stats
        wakeups.pass(trigger)
        try { runPassMeasured(c, stats) } finally {
            currentPass = null
            val total = stats.finish(state.replace(": ", "/").replace(Regex("[^A-Za-z0-9_/]"), "_").take(80).ifBlank { "UNKNOWN" })
            lastPass = stats.toJson(total)
            if (stats.mode != "SKIP" || skipLog.due(android.os.SystemClock.elapsedRealtime()))
                runCatching { LogBuffer.add('I', "CloudRecovery", stats.logLine(total)) }
            if (storageWalk.due(android.os.SystemClock.elapsedRealtime())) runCatching { logStorage(c) }
        }
    }
    /** Read-only, bounded storage attribution for the 15.58 GB question. Relative top-level names only. */
    private fun logStorage(c: Context) {
        val data = RecoveryDiagnostics.topLevelUsage(File(c.applicationInfo.dataDir), "data")
        val files = RecoveryDiagnostics.topLevelUsage(c.filesDir, "files")
        val noBackup = RecoveryDiagnostics.topLevelUsage(c.noBackupFilesDir, "no_backup")
        val outbox = RecoveryDiagnostics.rawOutbox(File(c.filesDir, BnfRawCapture.DIRECTORY), File(c.noBackupFilesDir, "bnf_table_ack_v1"))
        listOf(data.logLine(), files.logLine(), noBackup.logLine(), outbox.logLine()).forEach { LogBuffer.add('I', "CloudRecovery", it) }
        lastStorage = JSONObject().put("data", data.toJson()).put("files", files.toJson())
            .put("no_backup", noBackup.toJson()).put("raw_outbox", outbox.toJson())
    }
    private fun runPassMeasured(c: Context, stats: RecoveryDiagnostics.PassStats) {
        val hadMonitorLease = canMonitor()
        try {
            stats.stage("claim")
            val session = CloudSession(c)
            if (!session.signedIn()) { state = "APP_SIGN_IN_REQUIRED"; return }
            val transport = Transport(c)
            val uid = transport.uid()
            val metadata = meta(c)
            require(owner(c).isBlank() || owner(c) == uid) { "DIFFERENT_APP_ACCOUNT" }
            val device = metadata.getString("device", null) ?: UUID.randomUUID().toString().also {
                require(metadata.edit().putString("device", it).putString("owner", uid).commit())
            }
            val vault = CloudVault(transport)
            var head = transport.head()
            var revision = head?.getLong("revision") ?: 0L
            var localRevision = metadata.getLong("revision", 0L)
            val remoteWriter = head?.optString("device_id")
            val hasCloud = head != null && !head.isNull("manifest_sha256")
            var oldManifest = if (hasCloud) vault.manifest(head!!.getString("manifest_sha256"), BuildConfig.VERSION_CODE) else null
            // A lost commit response must not become a false competing-device conflict.
            // This install's UUID is private local identity; only its own ambiguous commit can be rebased.
            if (hasCloud && remoteWriter == device && revision != localRevision && metadata.getBoolean("initialized", false)) {
                require(metadata.edit().putLong("revision", revision).commit())
                localRevision = revision
            }
            val claim = JSONObject().put("p_device", device).put("p_expected", revision)
            head = transport.rpc("mr_cloud_claim", claim)
            leaseUntil = head.getLong("_lease_deadline_elapsed")
            var lastRenewal = android.os.SystemClock.elapsedRealtime()
            val leaseRenewalLock = Any()
            transport.renewLease = { synchronized(leaseRenewalLock) {
                val now = android.os.SystemClock.elapsedRealtime()
                if (now - lastRenewal >= 30_000) {
                    val renewed = transport.rpc("mr_cloud_claim", JSONObject().put("p_device", device).put("p_expected", revision))
                    leaseUntil = renewed.getLong("_lease_deadline_elapsed")
                    lastRenewal = now
                }
            } }
            if (!ready && oldManifest != null && (!metadata.getBoolean("initialized", false) || revision != localRevision ||
                        File(c.noBackupFilesDir, "cloud_restore_pending").exists())) {
                state = "RESTORING_VERIFIED_CLOUD_STATE"
                stats.stage("restore")
                val current = fingerprints(inventory(c))
                val recovering = File(c.noBackupFilesDir, "cloud_restore_pending").exists()
                if (!recovering && !metadata.getBoolean("initialized", false) && inventory(c).any { it.path.startsWith("files/") } &&
                    current != manifestFingerprints(oldManifest)) error("LOCAL_AND_CLOUD_CHANGED_RECONCILIATION_REQUIRED")
                if (!recovering && metadata.getBoolean("initialized", false) && revision != localRevision &&
                    metadata.getString("fingerprint", "") != fingerprintMap(current) && current != manifestFingerprints(oldManifest)) {
                    error("LOCAL_AND_CLOUD_CHANGED_RECONCILIATION_REQUIRED")
                }
                val stage = File(c.noBackupFilesDir, "cloud_restore_stage")
                if (stage.exists()) stage.deleteRecursively()
                vault.stage(oldManifest, stage)
                applyStage(c, stage, oldManifest)
                require(metadata.edit().putLong("revision", revision).putBoolean("initialized", true)
                    .putString("fingerprint", fingerprintMap(fingerprints(inventory(c)))).commit())
            } else if (ready && revision != localRevision) error("CLOUD_REVISION_CONFLICT")
            // The existing install already has a verified checkpoint. After a
            // successful owner/device/revision claim, new local files may upload
            // in the background without withholding market polls until the full
            // inventory finishes. Fresh installs and incomplete restores do not
            // satisfy this admission. Monitoring still expires with the lease.
            val localWebCaptured = c.getSharedPreferences("cloud_web_cache_v1", Context.MODE_PRIVATE).contains("storage")
            if (CloudMonitorResume.eligible(
                    metadata.getBoolean("initialized", false), localRevision, revision,
                    device, remoteWriter, metadata.getString("checkpoint_sha256", "").orEmpty(),
                    head?.optString("manifest_sha256").orEmpty(),
                    oldManifest?.optJSONObject("entries")?.has("prefs/cloud_web_cache_v1.json") == true,
                    localWebCaptured, metadata.getBoolean("web_capture_failed", false),
                    File(c.noBackupFilesDir, "cloud_restore_pending").exists())) {
                ready = true
                webVerified = true
                if (canMonitor() && !legacyStarted) {
                    legacyStarted = true
                    PositionTickUploadRunner.onProcessStart(c, PositionTickDrainTrigger.APP_START)
                    DailyReviewScheduler.request(c, "verified_local_checkpoint")
                    CaptureRuntime.ensure(c)
                    CaptureRuntime.reconcile()
                }
                if (canMonitor() && !hadMonitorLease)
                    MarketOpenScheduler.maybeStartIngestionNow(c, "verified_local_checkpoint")
            }
            // Quiet-pass gate: evaluated only after the claim above has renewed the
            // lease and after restore/monitor-resume checks. SKIP never commits,
            // prunes, restores or deletes; anything uncertain runs the FULL pass.
            stats.stage("gate")
            val gateSources = inventory(c)
            val gateSignature = signature(gateSources)
            val gate = RecoveryPassGate.decide(verifiedBaseline, RecoveryPassGate.Observation(
                ready = ready, canMonitor = canMonitor(),
                restorePending = File(c.noBackupFilesDir, "cloud_restore_pending").exists(),
                webCaptureFailed = metadata.getBoolean("web_capture_failed", false),
                revision = revision, localRevision = metadata.getLong("revision", 0L),
                headManifestSha256 = head?.optString("manifest_sha256").orEmpty(),
                localManifestSha256 = metadata.getString("checkpoint_sha256", "").orEmpty(),
                mutation = mutations.get(), signature = gateSignature,
                nowElapsed = android.os.SystemClock.elapsedRealtime()))
            stats.gateReason = gate
            if (gate == RecoveryPassGate.SKIP) {
                stats.mode = "SKIP"
                wakeups.pass("skipped_unchanged")
                stats.portableFiles = gateSources.size.toLong()
                stats.portableBytes = gateSources.sumOf { it.file?.length() ?: it.bytes?.size?.toLong() ?: 0L }
                filesTotal = gateSources.size; filesChecked = gateSources.size
                return
            }
            verifiedBaseline = null
            state = "CHECKING_PENDING_CACHE"
            stats.stage("inventory")
            observeFiles(c)
            val snapshotMutation = mutations.get()
            val sources = inventory(c)
            filesChecked = 0; filesTotal = sources.size
            val baselineSignature = signature(sources) // stat taken before hashing: later edits force FULL
            stats.portableFiles = sources.size.toLong()
            stats.portableBytes = sources.sumOf { it.file?.length() ?: it.bytes?.size?.toLong() ?: 0L }
            stats.stage("hash_before")
            val before = fingerprints(sources) { transport.renewLease?.invoke() }
            val sizes = sources.associate { it.path to (it.file?.length() ?: it.bytes?.size?.toLong() ?: 0L) }
            stats.dirty = RecoveryDiagnostics.dirty(before, oldManifest?.let { manifestFingerprints(it) } ?: emptyMap(), { sizes[it] ?: 0L })
            stats.stage("upload")
            val entries = JSONObject()
            val oldEntries = oldManifest?.getJSONObject("entries") ?: JSONObject()
            val receiptCache = VerifiedRecoveryUploadCache(File(c.noBackupFilesDir, "cloud_verified_upload_cache_v1/$uid"))
            val completed = java.util.concurrent.atomic.AtomicInteger()
            val pool = Executors.newFixedThreadPool(6)
            val batchFailure = java.util.concurrent.atomic.AtomicReference<Exception>()
            try {
                state = "UPLOADING_RECOVERY_DATA"
                // Wait for all workers before publishing or failing this pass. Receipts from
                // successful workers survive a later failure or a process restart.
                val jobs = sources.map { source -> java.util.concurrent.Callable {
                    batchFailure.get()?.let { throw it }
                    try {
                    transport.renewLease?.invoke()
                    val expected = before.getValue(source.path)
                    val old = oldEntries.optJSONObject(source.path)
                    val entry = if (old != null && old.optString("sha256") == expected) old.also { stats.filesReusedManifest.incrementAndGet() }
                        else { var uploaded = false
                            receiptCache.entry(expected) { uploaded = true; stats.filesUploaded.incrementAndGet(); vault.store(stats.countingStream(source.stream())) }
                                .also { if (!uploaded) stats.filesReusedReceipt.incrementAndGet() } }
                    require(entry.getString("sha256") == expected) { "CACHE_CHANGED_DURING_UPLOAD" }
                    synchronized(completed) { filesChecked = completed.incrementAndGet() }
                    source.path to entry
                    } catch (e: Exception) { batchFailure.compareAndSet(null, e); throw e }
                } }
                pool.invokeAll(jobs).forEach { future ->
                    val result = try { future.get() }
                        catch (e: java.util.concurrent.ExecutionException) { throw (e.cause as? Exception ?: e) }
                    val (path, entry) = result
                    entries.put(path, entry)
                }
            } finally { pool.shutdownNow() }
            stats.stage("hash_after")
            require(before == fingerprints(inventory(c)) { transport.renewLease?.invoke() }) { "CACHE_CHANGED_DURING_UPLOAD" }
            stats.stage("commit")
            if (oldManifest == null || before != manifestFingerprints(oldManifest)) {
                state = "UPLOADING_CHECKPOINT"
                stats.committed = true
                val hash = vault.publishManifest(entries, BuildConfig.VERSION_CODE)
                transport.rpc("mr_cloud_claim", claim) // renew/check lease after a large initial upload
                head = transport.rpc("mr_cloud_commit", JSONObject(claim.toString()).put("p_manifest", hash))
                val check = transport.head() ?: error("HEAD_READBACK_MISSING")
                require(check.getString("manifest_sha256") == hash && check.getLong("revision") == revision + 1) { "HEAD_READBACK_MISMATCH" }
                revision = check.getLong("revision")
            }
            val checkTime = head!!.optString("committed_at")
            val verifiedBytes = entries.keys().asSequence().sumOf { entries.getJSONObject(it).getLong("size") }
            require(metadata.edit().putLong("revision", revision).putBoolean("initialized", true)
                .putString("fingerprint", fingerprintMap(before)).putString("verified_at", checkTime)
                .putInt("verified_files", entries.length()).putLong("verified_bytes", verifiedBytes)
                .putString("checkpoint_sha256", head.getString("manifest_sha256"))
                .putString("last_error", "").commit())
            leaseUntil = maxOf(leaseUntil, head.getLong("_lease_deadline_elapsed"))
            ready = true
            stats.stage("prune")
            CloudArchive.pruneAcknowledged(c.filesDir, entries)
            val expectedCache = before.filterNot { (path, _) ->
                path.startsWith("files/${CloudArchive.DIRECTORY}/") && !File(c.filesDir, path.removePrefix("files/")).exists()
            }
            require(metadata.edit().putString("fingerprint", fingerprintMap(expectedCache)).commit())
            stats.stage("state_check")
            val webCaptured = c.getSharedPreferences("cloud_web_cache_v1", Context.MODE_PRIVATE).contains("storage")
            webVerified = webCaptured && !metadata.getBoolean("web_capture_failed", false) && before.containsKey("prefs/cloud_web_cache_v1.json")
            state = if (!webCaptured) "PWA_CAPTURE_REQUIRED" else if (snapshotMutation == mutations.get() &&
                before == fingerprints(inventory(c)) { transport.renewLease?.invoke() })
                "VERIFIED_CHECKPOINT" else "LOCAL_CHANGES_PENDING_UPLOAD"
            if (webCaptured && snapshotMutation != mutations.get()) state = "LOCAL_CHANGES_PENDING_UPLOAD"
            verifiedBaseline = if (state == "VERIFIED_CHECKPOINT" && snapshotMutation == mutations.get())
                RecoveryPassGate.Baseline(snapshotMutation, baselineSignature, revision,
                    metadata.getString("checkpoint_sha256", "").orEmpty(), android.os.SystemClock.elapsedRealtime()) else null
            if (webCaptured && canMonitor() && !legacyStarted) {
                legacyStarted = true
                PositionTickUploadRunner.onProcessStart(c, PositionTickDrainTrigger.APP_START)
                DailyReviewScheduler.request(c, "cloud_recovered")
                CaptureRuntime.ensure(c)
                CaptureRuntime.reconcile()
            }
            if (webCaptured && canMonitor() && !hadMonitorLease) MarketOpenScheduler.maybeStartIngestionNow(c, "cloud_recovered")
        } catch (e: Exception) {
            verifiedBaseline = null
            val detail = e.message.orEmpty().takeIf { it.matches(Regex("[A-Z0-9_]+")) } ?: e.javaClass.simpleName
            meta(c).edit().putString("last_error", detail).commit()
            state = "PENDING_OR_BLOCKED: $detail"
            if (detail.contains("CONFLICT") || detail.contains("OTHER_DEVICE") || detail.contains("RECONCILIATION")) {
                ready = false; leaseUntil = 0
            }
            // Existing verified cache may be inspected offline. Automatic monitoring remains lease-gated.
            if (!ready && meta(c).getBoolean("initialized", false) && !File(c.noBackupFilesDir, "cloud_restore_pending").exists() &&
                detail.startsWith("CLOUD_HTTP_").not() && e is java.io.IOException) ready = true
        }
    }
    private fun fingerprintMap(map: Map<String, String>) = CloudVault.hash(map.entries.joinToString("\n") { it.key + ":" + it.value }.toByteArray())
}

class CloudRecoveryWorker(c: Context, p: WorkerParameters): Worker(c, p) {
    override fun doWork(): Result { CloudRecovery.runPass(applicationContext, "worker"); return if (CloudRecovery.canMonitor()) Result.success() else Result.retry() }
}
