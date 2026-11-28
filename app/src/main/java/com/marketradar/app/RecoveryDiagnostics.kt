package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FilterInputStream
import java.io.InputStream
import java.nio.file.FileVisitResult
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.SimpleFileVisitor
import java.nio.file.attribute.BasicFileAttributes
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicLong

/** 27 Nov live-session diagnostics: non-destructive, bounded, Android-free.
 * Reports only counts, byte totals, durations and sanitised top-level directory
 * names. Never absolute paths, file names below the top level, file contents,
 * preference values, tokens or other secrets. Never deletes or rewrites anything.
 */
object RecoveryDiagnostics {
    const val MAX_WALK_ENTRIES = 250_000
    const val MAX_GROUPS = 32
    private val SAFE_NAME = Regex("[A-Za-z0-9_.-]{1,64}")
    private val SECRET_HINT = Regex("(token|auth|password|secret|api_?key|cookie|bearer|credential|session|device_key)", RegexOption.IGNORE_CASE)

    /** Name shown in logs for one top-level entry. Anything unusual or secret-looking is redacted. */
    fun safeName(name: String): String =
        if (name.matches(SAFE_NAME) && !SECRET_HINT.containsMatchIn(name)) name else "redacted"

    data class Usage(val files: Long, val bytes: Long)
    /** [failures] counts entries the walk could not read (visitFileFailed); [incompleteReason] is "" only when
     * every entry under the root was visited, so a partial total can never read as full attribution. */
    data class Breakdown(val label: String, val groups: Map<String, Usage>, val totalFiles: Long,
                         val totalBytes: Long, val symlinksSkipped: Long, val truncated: Boolean,
                         val failures: Long = 0, val incompleteReason: String = "", val elapsedMs: Long = 0) {
        val incomplete: Boolean get() = incompleteReason.isNotEmpty()
        fun toJson(): JSONObject = JSONObject().put("root", label).put("files", totalFiles).put("bytes", totalBytes)
            .put("symlinks_skipped", symlinksSkipped).put("truncated", truncated)
            .put("failures", failures).put("incomplete", incomplete).put("incomplete_reason", incompleteReason)
            .put("elapsed_ms", elapsedMs)
            .put("groups", JSONArray().also { a -> groups.forEach { (k, v) ->
                a.put(JSONObject().put("name", k).put("files", v.files).put("bytes", v.bytes)) } })
        fun logLine(): String = "STORAGE_BREAKDOWN root=$label files=$totalFiles bytes=$totalBytes truncated=$truncated " +
            "failures=$failures incomplete=${incompleteReason.ifEmpty { "no" }} elapsed_ms=$elapsedMs " +
            groups.entries.joinToString(" ") { "${it.key}=${it.value.files}f/${it.value.bytes}b" }
    }
    /** Default wall-time budget for one root's walk; the walk runs off the recovery monitor (see CloudRecovery). */
    const val WALK_BUDGET_MS = 20_000L

    /** Per top-level child of [root]: file count and bytes. Symlinks are not followed or counted.
     * The walk stops after [maxEntries] visited entries and then reports truncated=true.
     * Groups beyond [maxGroups] (largest first) are folded into "other".
     */
    fun topLevelUsage(root: File?, label: String, maxEntries: Int = MAX_WALK_ENTRIES, maxGroups: Int = MAX_GROUPS,
                      budgetMs: Long = WALK_BUDGET_MS, nanoTime: () -> Long = System::nanoTime): Breakdown {
        if (root == null || !root.isDirectory) return Breakdown(label, emptyMap(), 0, 0, 0, false,
            incompleteReason = if (root == null) "" else if (root.exists()) "NOT_A_DIRECTORY" else "")
        val rootPath = root.toPath()
        val files = HashMap<String, Long>(); val bytes = HashMap<String, Long>()
        var visited = 0L; var symlinks = 0L; var truncated = false; var failures = 0L; var timedOut = false; var error = false
        val startNs = nanoTime(); val budgetNs = budgetMs * 1_000_000L
        fun overBudget(): Boolean = (nanoTime() - startNs > budgetNs).also { if (it) timedOut = true }
        fun group(p: Path): String {
            val rel = rootPath.relativize(p)
            return if (rel.nameCount <= 1) "(root_files)"
                else safeName(rel.getName(0).toString())
        }
        try {
            Files.walkFileTree(rootPath, object : SimpleFileVisitor<Path>() {
                override fun preVisitDirectory(dir: Path, attrs: BasicFileAttributes): FileVisitResult {
                    if (++visited > maxEntries) { truncated = true; return FileVisitResult.TERMINATE }
                    if (overBudget()) return FileVisitResult.TERMINATE
                    return FileVisitResult.CONTINUE
                }
                override fun visitFile(file: Path, attrs: BasicFileAttributes): FileVisitResult {
                    if (++visited > maxEntries) { truncated = true; return FileVisitResult.TERMINATE }
                    if ((visited and 255L) == 0L && overBudget()) return FileVisitResult.TERMINATE
                    if (attrs.isSymbolicLink) { symlinks++; return FileVisitResult.CONTINUE }
                    if (attrs.isRegularFile) {
                        val g = group(file)
                        files[g] = (files[g] ?: 0L) + 1
                        bytes[g] = (bytes[g] ?: 0L) + attrs.size()
                    }
                    return FileVisitResult.CONTINUE
                }
                // Unreadable entry (permission, vanished mid-walk): counted, never silently dropped.
                override fun visitFileFailed(file: Path, exc: java.io.IOException): FileVisitResult { failures++; return FileVisitResult.CONTINUE }
                override fun postVisitDirectory(dir: Path, exc: java.io.IOException?): FileVisitResult {
                    if (exc != null) failures++ // directory listing failed part-way
                    return FileVisitResult.CONTINUE
                }
            })
        } catch (_: Exception) { truncated = true; error = true }
        val elapsedMs = (nanoTime() - startNs) / 1_000_000L
        val ordered = files.keys.sortedWith(compareByDescending<String> { bytes[it] ?: 0L }.thenBy { it })
        val out = LinkedHashMap<String, Usage>()
        var otherF = 0L; var otherB = 0L
        ordered.forEachIndexed { i, k ->
            val f = files[k] ?: 0L; val b = bytes[k] ?: 0L
            if (i < maxGroups) {
                val prev = out[k]
                out[k] = if (prev == null) Usage(f, b) else Usage(prev.files + f, prev.bytes + b)
            } else { otherF += f; otherB += b }
        }
        if (otherF > 0) out["other"] = Usage((out["other"]?.files ?: 0) + otherF, (out["other"]?.bytes ?: 0) + otherB)
        val reason = listOfNotNull("ENTRY_LIMIT".takeIf { truncated && !error }, "WALK_ERROR".takeIf { error },
            "TIME_BUDGET".takeIf { timedOut }, "UNREADABLE_ENTRIES".takeIf { failures > 0 }).joinToString("+")
        return Breakdown(label, out, files.values.sum(), bytes.values.sum(), symlinks, truncated, failures, reason, elapsedMs)
    }

    data class OutboxStats(val metadataFiles: Int, val bodyFiles: Int, val otherFiles: Int, val bytes: Long,
                           val acknowledged: Int, val pending: Int, val orphanAcks: Int, val ackBytes: Long, val truncated: Boolean) {
        fun toJson(): JSONObject = JSONObject().put("metadata_files", metadataFiles).put("body_files", bodyFiles)
            .put("other_files", otherFiles).put("bytes", bytes).put("acknowledged", acknowledged)
            .put("pending", pending).put("orphan_acks", orphanAcks).put("ack_bytes", ackBytes).put("truncated", truncated)
        fun logLine(): String = "RAW_OUTBOX_STATS metadata=$metadataFiles bodies=$bodyFiles other=$otherFiles bytes=$bytes " +
            "acknowledged=$acknowledged pending=$pending orphan_acks=$orphanAcks ack_bytes=$ackBytes truncated=$truncated"
    }

    /** Same id rule as BnfSourceUploader.drain: `<digits>_<uuid>.json`; ack marker is `<id>` in [ackDir]. Read-only. */
    private val CAPTURE_META = Regex("[0-9]+_[0-9a-fA-F-]{36}\\.json")
    private val CAPTURE_BODY = Regex("[0-9]+_[0-9a-fA-F-]{36}\\.body\\.gz")
    fun rawOutbox(outboxDir: File?, ackDir: File?, maxEntries: Int = MAX_WALK_ENTRIES): OutboxStats {
        val listed = outboxDir?.takeIf { it.isDirectory }?.listFiles()?.toList().orEmpty()
        val truncated = listed.size > maxEntries
        val entries = listed.take(maxEntries).filter { it.isFile && !Files.isSymbolicLink(it.toPath()) }
        val acks = ackDir?.takeIf { it.isDirectory }?.listFiles()?.filter { it.isFile && !it.name.endsWith(".tmp") }.orEmpty()
        val ackNames = acks.mapTo(HashSet()) { it.name }
        var meta = 0; var body = 0; var other = 0; var bytes = 0L; var acked = 0
        val ids = HashSet<String>()
        entries.forEach { f ->
            bytes += f.length()
            when {
                f.name.matches(CAPTURE_META) -> { meta++; val id = f.name.removeSuffix(".json"); ids += id; if (id in ackNames) acked++ }
                f.name.matches(CAPTURE_BODY) -> body++
                else -> other++
            }
        }
        return OutboxStats(meta, body, other, bytes, acked, meta - acked, ackNames.count { it !in ids },
            acks.sumOf { it.length() }, truncated)
    }

    /** Per-pass counters. Only numbers and fixed enum strings. */
    class PassStats(val trigger: String, private val clock: () -> Long) {
        private val started = clock()
        private var stageStart = started
        private var stage = "start"
        val stages = LinkedHashMap<String, Long>()
        val bytesHashed = AtomicLong(); val filesHashed = AtomicLong()
        val bytesUploaded = AtomicLong(); val filesUploaded = AtomicLong()
        val filesReusedManifest = AtomicLong(); val filesReusedReceipt = AtomicLong()
        @Volatile var portableFiles = 0L
        @Volatile var portableBytes = 0L
        @Volatile var mode = "FULL"
        @Volatile var gateReason = ""
        @Volatile var outcome = "RUNNING"
        /** Manifest publication was started. NOT a durable checkpoint. */
        @Volatile var commitAttempted = false
        /** Server commit returned AND head readback matched the new manifest and revision. */
        @Volatile var commitConfirmed = false
        @Volatile var dirty: Dirty? = null
        @Volatile var writers: ChangeTrace.Report? = null
        @Volatile var signals: String = ""
        @Volatile var budget: String = ""
        @Volatile var hashingStarted = false
        /** Restore download or full hash began; used to persist the budget cooldown on failure too. */
        @Volatile var expensiveWorkStarted = false
        @Volatile var budgetPrereq: String = ""
        @Synchronized fun stage(next: String) {
            val now = clock()
            stages[stage] = (stages[stage] ?: 0L) + (now - stageStart)
            stage = next; stageStart = now
        }
        @Synchronized fun finish(result: String): Long { stage("done"); outcome = result; return clock() - started }
        fun countingStream(input: InputStream): InputStream = object : FilterInputStream(input) {
            override fun read(): Int = super.read().also { if (it >= 0) bytesUploaded.incrementAndGet() }
            override fun read(b: ByteArray, off: Int, len: Int): Int = super.read(b, off, len).also { if (it > 0) bytesUploaded.addAndGet(it.toLong()) }
        }
        @Synchronized fun toJson(totalMs: Long): JSONObject = JSONObject().put("trigger", trigger).put("mode", mode)
            .put("gate_reason", gateReason).put("budget", budget).put("outcome", outcome)
            .put("commit_attempted", commitAttempted).put("commit_confirmed", commitConfirmed).put("total_ms", totalMs)
            .put("stages_ms", JSONObject().also { o -> stages.forEach { (k, v) -> if (k != "done") o.put(k, v) } })
            .put("bytes_hashed", bytesHashed.get()).put("files_hashed", filesHashed.get())
            .put("bytes_uploaded", bytesUploaded.get()).put("files_uploaded", filesUploaded.get())
            .put("files_reused_manifest", filesReusedManifest.get()).put("files_reused_receipt", filesReusedReceipt.get())
            .put("portable_files", portableFiles).put("portable_bytes", portableBytes)
            .put("dirty", dirty?.let { JSONObject().put("added", it.added).put("modified", it.modified)
                .put("removed", it.removed).put("bytes", it.bytes).put("groups", it.groups) } ?: JSONObject.NULL)
            .put("writers", writers?.toJson() ?: JSONObject.NULL).put("signals", signals)
        @Synchronized fun logLine(totalMs: Long): String = "CLOUD_PASS_STATS trigger=$trigger mode=$mode gate=${gateReason.ifBlank { "-" }} " +
            "budget=${budget.ifBlank { "-" }} outcome=$outcome commit_attempted=$commitAttempted commit_confirmed=$commitConfirmed total_ms=$totalMs " +
            stages.filterKeys { it != "done" }.entries.joinToString(" ") { "${it.key}_ms=${it.value}" } +
            " hashed=${bytesHashed.get()}b/${filesHashed.get()}f uploaded=${bytesUploaded.get()}b/${filesUploaded.get()}f " +
            "reused_manifest=${filesReusedManifest.get()} reused_receipt=${filesReusedReceipt.get()} " +
            "portable=${portableBytes}b/${portableFiles}f" +
            (dirty?.let { " dirty=a${it.added}/m${it.modified}/r${it.removed}/${it.bytes}b groups=${it.groups.ifBlank { "-" }}" } ?: "") +
            (writers?.let { " writers=c${it.changed}/rev${it.reverted} classes=${it.classes.ifBlank { "-" }} prefkeys=${it.prefKeys.ifBlank { "-" }}" } ?: "") +
            (if (signals.isNotBlank()) " signals=$signals" else "")
    }

    /** Leaf-name pattern for writer attribution: hex runs (8+) -> H, digit runs -> N, then [safeName].
     * Shows `files/<top>/<pattern>` (intermediate directories elided as `...`) or `prefs/<name>`. */
    fun nameClass(path: String): String {
        val parts = path.split('/')
        if (parts.size < 2) return safeName(path)
        if (parts[0] == "prefs") return "prefs/" + safeName(parts[1])
        val top = safeName(parts[1])
        if (parts.size == 2) return "files/(root)/" + pattern(top)
        return "files/" + top + (if (parts.size > 3) "/..." else "") + "/" + pattern(parts.last())
    }
    private val HEX_RUN = Regex("[0-9a-fA-F]{8,}")
    private val DIGIT_RUN = Regex("[0-9]+")
    private fun pattern(leaf: String): String = safeName(leaf.replace(HEX_RUN, "H").replace(DIGIT_RUN, "N"))

    /** Process-lifetime change tracker across FULL passes: which name classes change, how often a path returns to a
     * content hash it had before (`reverted`: toggling writers, re-created archive files), and which preference keys
     * (and first-level JSON sub-keys) changed. Names only; never values. Bounded by [maxPaths] and [maxNames]. */
    class ChangeTrace(private val maxPaths: Int = 50_000, private val maxNames: Int = 12, private val depth: Int = 4) {
        data class Report(val changed: Int, val reverted: Int, val classes: String, val prefKeys: String, val primed: Boolean) {
            fun toJson(): JSONObject = JSONObject().put("changed", changed).put("reverted", reverted)
                .put("classes", classes).put("pref_keys", prefKeys).put("primed", primed)
        }
        private val history = HashMap<String, ArrayDeque<String>>()
        private val prefKeys = HashMap<String, Map<String, Int>>()
        private var primed = false
        private fun remember(path: String, hash: String) {
            val h = history[path] ?: (if (history.size >= maxPaths) return else ArrayDeque<String>().also { history[path] = it })
            h.addLast(hash); while (h.size > depth) h.removeFirst()
        }
        @Synchronized fun observe(current: Map<String, String>, prefBytes: Map<String, ByteArray>): Report {
            val classes = LinkedHashMap<String, Int>(); var changed = 0; var reverted = 0
            val keyNames = LinkedHashSet<String>()
            current.forEach { (path, hash) ->
                val last = history[path]?.lastOrNull()
                if (last != hash) {
                    if (primed) {
                        changed++
                        if (history[path]?.contains(hash) == true) reverted++
                        val k = nameClass(path); classes[k] = (classes[k] ?: 0) + 1
                    }
                    remember(path, hash)
                }
            }
            history.keys.filter { it !in current && history[it]?.lastOrNull() != REMOVED }.forEach { path ->
                changed++; val k = nameClass(path) + ":removed"; classes[k] = (classes[k] ?: 0) + 1; remember(path, REMOVED)
            }
            prefBytes.forEach { (path, bytes) ->
                val keys = prefKeyHashes(bytes)
                val prev = prefKeys[path]
                if (primed && prev != null && prev != keys) {
                    (prev.keys + keys.keys).filter { prev[it] != keys[it] }.forEach { key ->
                        keyNames += nameClass(path).removePrefix("prefs/").removeSuffix(".json") + ":" + key }
                }
                prefKeys[path] = keys
            }
            val wasPrimed = primed; primed = true
            val classText = classes.entries.sortedByDescending { it.value }.take(maxNames).joinToString(",") { "${it.key}=${it.value}" } +
                (if (classes.size > maxNames) ",+${classes.size - maxNames}" else "")
            val keyText = keyNames.take(maxNames).joinToString(",") + (if (keyNames.size > maxNames) ",+${keyNames.size - maxNames}" else "")
            return Report(changed, reverted, classText, keyText, wasPrimed)
        }
        /** key (and key.subkey for JSON-object string values) -> value hashCode. Names are sanitised. */
        private fun prefKeyHashes(bytes: ByteArray): Map<String, Int> = try {
            val out = HashMap<String, Int>()
            val root = JSONObject(String(bytes, Charsets.UTF_8))
            root.keys().forEach { key ->
                val v = root.optJSONObject(key)?.opt("value")
                val name = safeName(key)
                out[name] = (out[name] ?: 0) * 31 + v.toString().hashCode()
                val text = v as? String
                if (text != null && text.startsWith("{")) {
                    val inner = try { JSONObject(text) } catch (_: Exception) { null }
                    inner?.keys()?.forEach { sub -> val n = name + "." + safeName(sub)
                        out[n] = (out[n] ?: 0) * 31 + inner.opt(sub).toString().hashCode() }
                }
            }
            out
        } catch (_: Exception) { mapOf("(unparsed)" to bytes.contentHashCode()) }
        private companion object { const val REMOVED = "(removed)" }
    }

    /** Bounded per-source counters of change signals (observer events, preference keys, PWA saves) since the last
     * drain. Names only. Keys beyond [maxKeys] fold into "other". */
    class SignalCounter(private val maxKeys: Int = 64) {
        private val counts = ConcurrentHashMap<String, AtomicLong>()
        fun add(source: String) {
            val key = if (counts.containsKey(source) || counts.size < maxKeys) source else "other"
            counts.computeIfAbsent(key) { AtomicLong() }.incrementAndGet()
        }
        @Synchronized fun drain(maxShown: Int = 10): String {
            val snapshot = counts.entries.map { it.key to it.value.getAndSet(0) }.filter { it.second > 0 }
                .sortedByDescending { it.second }
            counts.entries.removeIf { it.value.get() == 0L }
            return snapshot.take(maxShown).joinToString(",") { "${it.first}=${it.second}" } +
                (if (snapshot.size > maxShown) ",+${snapshot.size - maxShown}" else "")
        }
    }

    /** Which checkpoint groups differ from the previous verified manifest: `files/<top>` or `prefs/<name>`
     * (sanitised), with added/modified/removed counts and bytes of added+modified entries. No file names below top level.
     */
    data class Dirty(val added: Int, val modified: Int, val removed: Int, val bytes: Long, val groups: String)
    fun dirty(current: Map<String, String>, previous: Map<String, String>, sizeOf: (String) -> Long, maxGroups: Int = 8): Dirty {
        fun group(path: String): String { val parts = path.split('/'); return parts[0] + "/" + safeName(parts.getOrElse(1) { "" }) }
        val counts = LinkedHashMap<String, IntArray>(); var a = 0; var m = 0; var r = 0; var bytes = 0L
        fun bump(path: String, i: Int) { counts.getOrPut(group(path)) { IntArray(3) }[i]++ }
        current.forEach { (path, sha) ->
            val old = previous[path]
            if (old == null) { a++; bump(path, 0); bytes += sizeOf(path) }
            else if (old != sha) { m++; bump(path, 1); bytes += sizeOf(path) }
        }
        previous.keys.forEach { if (it !in current) { r++; bump(it, 2) } }
        val text = counts.entries.sortedByDescending { it.value.sum() }.take(maxGroups)
            .joinToString(",") { "${it.key}:a${it.value[0]}m${it.value[1]}r${it.value[2]}" } +
            (if (counts.size > maxGroups) ",+${counts.size - maxGroups}groups" else "")
        return Dirty(a, m, r, bytes, text)
    }

    /** Process-lifetime wakeup counters (pass invocations by trigger, observer mutation signals). */
    class Wakeups {
        private val byTrigger = ConcurrentHashMap<String, AtomicLong>()
        val mutationSignals = AtomicLong()
        fun pass(trigger: String) { byTrigger.computeIfAbsent(trigger) { AtomicLong() }.incrementAndGet() }
        fun toJson(): JSONObject = JSONObject().also { o ->
            byTrigger.toSortedMap().forEach { (k, v) -> o.put("passes_$k", v.get()) }
            o.put("mutation_signals", mutationSignals.get())
        }
    }

    /** Small rate limiter for the heavier storage walk (default once per 6 h per process). */
    class Every(private val intervalMs: Long) {
        @Volatile private var last = Long.MIN_VALUE
        @Synchronized fun due(now: Long): Boolean = if (last == Long.MIN_VALUE || now - last >= intervalMs) { last = now; true } else false
    }
}
