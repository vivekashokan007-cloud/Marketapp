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
    data class Breakdown(val label: String, val groups: Map<String, Usage>, val totalFiles: Long,
                         val totalBytes: Long, val symlinksSkipped: Long, val truncated: Boolean) {
        fun toJson(): JSONObject = JSONObject().put("root", label).put("files", totalFiles).put("bytes", totalBytes)
            .put("symlinks_skipped", symlinksSkipped).put("truncated", truncated)
            .put("groups", JSONArray().also { a -> groups.forEach { (k, v) ->
                a.put(JSONObject().put("name", k).put("files", v.files).put("bytes", v.bytes)) } })
        fun logLine(): String = "STORAGE_BREAKDOWN root=$label files=$totalFiles bytes=$totalBytes truncated=$truncated " +
            groups.entries.joinToString(" ") { "${it.key}=${it.value.files}f/${it.value.bytes}b" }
    }

    /** Per top-level child of [root]: file count and bytes. Symlinks are not followed or counted.
     * The walk stops after [maxEntries] visited entries and then reports truncated=true.
     * Groups beyond [maxGroups] (largest first) are folded into "other".
     */
    fun topLevelUsage(root: File?, label: String, maxEntries: Int = MAX_WALK_ENTRIES, maxGroups: Int = MAX_GROUPS): Breakdown {
        if (root == null || !root.isDirectory) return Breakdown(label, emptyMap(), 0, 0, 0, false)
        val rootPath = root.toPath()
        val files = HashMap<String, Long>(); val bytes = HashMap<String, Long>()
        var visited = 0L; var symlinks = 0L; var truncated = false
        fun group(p: Path): String {
            val rel = rootPath.relativize(p)
            return if (rel.nameCount <= 1) "(root_files)"
                else safeName(rel.getName(0).toString())
        }
        try {
            Files.walkFileTree(rootPath, object : SimpleFileVisitor<Path>() {
                override fun preVisitDirectory(dir: Path, attrs: BasicFileAttributes): FileVisitResult {
                    if (++visited > maxEntries) { truncated = true; return FileVisitResult.TERMINATE }
                    return FileVisitResult.CONTINUE
                }
                override fun visitFile(file: Path, attrs: BasicFileAttributes): FileVisitResult {
                    if (++visited > maxEntries) { truncated = true; return FileVisitResult.TERMINATE }
                    if (attrs.isSymbolicLink) { symlinks++; return FileVisitResult.CONTINUE }
                    if (attrs.isRegularFile) {
                        val g = group(file)
                        files[g] = (files[g] ?: 0L) + 1
                        bytes[g] = (bytes[g] ?: 0L) + attrs.size()
                    }
                    return FileVisitResult.CONTINUE
                }
                override fun visitFileFailed(file: Path, exc: java.io.IOException): FileVisitResult = FileVisitResult.CONTINUE
            })
        } catch (_: Exception) { truncated = true }
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
        return Breakdown(label, out, files.values.sum(), bytes.values.sum(), symlinks, truncated)
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
        @Volatile var committed = false
        @Volatile var dirty: Dirty? = null
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
            .put("gate_reason", gateReason).put("outcome", outcome).put("committed", committed).put("total_ms", totalMs)
            .put("stages_ms", JSONObject().also { o -> stages.forEach { (k, v) -> if (k != "done") o.put(k, v) } })
            .put("bytes_hashed", bytesHashed.get()).put("files_hashed", filesHashed.get())
            .put("bytes_uploaded", bytesUploaded.get()).put("files_uploaded", filesUploaded.get())
            .put("files_reused_manifest", filesReusedManifest.get()).put("files_reused_receipt", filesReusedReceipt.get())
            .put("portable_files", portableFiles).put("portable_bytes", portableBytes)
            .put("dirty", dirty?.let { JSONObject().put("added", it.added).put("modified", it.modified)
                .put("removed", it.removed).put("bytes", it.bytes).put("groups", it.groups) } ?: JSONObject.NULL)
        @Synchronized fun logLine(totalMs: Long): String = "CLOUD_PASS_STATS trigger=$trigger mode=$mode gate=${gateReason.ifBlank { "-" }} " +
            "outcome=$outcome committed=$committed total_ms=$totalMs " +
            stages.filterKeys { it != "done" }.entries.joinToString(" ") { "${it.key}_ms=${it.value}" } +
            " hashed=${bytesHashed.get()}b/${filesHashed.get()}f uploaded=${bytesUploaded.get()}b/${filesUploaded.get()}f " +
            "reused_manifest=${filesReusedManifest.get()} reused_receipt=${filesReusedReceipt.get()} " +
            "portable=${portableBytes}b/${portableFiles}f" +
            (dirty?.let { " dirty=a${it.added}/m${it.modified}/r${it.removed}/${it.bytes}b groups=${it.groups.ifBlank { "-" }}" } ?: "")
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
