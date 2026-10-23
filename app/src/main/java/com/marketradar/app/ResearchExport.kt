package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.security.DigestInputStream
import java.security.MessageDigest
import java.time.Instant
import java.time.ZoneId
import java.util.zip.ZipEntry
import java.util.zip.ZipInputStream
import java.util.zip.ZipOutputStream

/**
 * M0 Stage 2 CI-4: read-only capacity/utilisation status of the research protection ledger.
 *
 * PURE READ: never writes, never calls [ResearchProtection.load] (which persists LEDGER_CORRUPT). Unknown, malformed
 * or corrupt input is reported explicitly ("known": false + reason); it is never shown as zero.
 *
 * Accepted formula (CDX-STAGE2-BUILD-AUTH-1): active = STAGED + LAG_EXCEEDED + ARCHIVE_ACKED;
 * U_count = active / CAPACITY; U_bytes = (ledger_bytes + 448*active + 128) / (MAX_BYTES - 768); U = max.
 * ledger_bytes is the ACTUAL size of the sealed protection.json on disk.
 */
object ResearchCapacity {
    const val SCHEMA = "research_m0_capacity_v1"
    val ACTIVE_STATES = listOf("STAGED", "LAG_EXCEEDED", "ARCHIVE_ACKED")

    fun read(dir: File, nowMs: Long): JSONObject {
        val cap = ResearchProtection.CAPACITY; val maxBytes = ResearchProtection.MAX_BYTES.toLong()
        val out = JSONObject().put("schema", SCHEMA).put("computed_ms", nowMs)
            .put("capacity", cap).put("max_bytes", maxBytes)
            .put("entry_growth_reserve_bytes", ResearchProtection.ENTRY_GROWTH_RESERVE_BYTES)
            .put("compaction_reserve_bytes", ResearchProtection.COMPACTION_RESERVE_BYTES)
            .put("reject_reserve_bytes", ResearchProtection.REJECT_RESERVE_BYTES)
            .put("formula", "U=max(active/capacity,(ledger_bytes+448*active+128)/(max_bytes-768)); active=STAGED+LAG_EXCEEDED+ARCHIVE_ACKED")
            .put("cap_refusals_process", ResearchProtection.capRefusals)
        fun unknown(integrity: String, reason: String): JSONObject = out.put("known", false).put("integrity", integrity)
            .put("reason", reason).put("active", JSONObject.NULL).put("ledger_bytes", JSONObject.NULL)
            .put("u_count", JSONObject.NULL).put("u_bytes", JSONObject.NULL).put("u", JSONObject.NULL)
        val ledger = File(dir, "protection.json")
        val status = M0Durable.read(File(dir, "ledgerstatus.json"))
        if (status is M0Durable.Read.Ok && status.json.optString("state") == "LEDGER_CORRUPT")
            return unknown("LEDGER_CORRUPT", "ledgerstatus:" + status.json.optString("reason"))
        if (status is M0Durable.Read.Corrupt) return unknown("LEDGER_CORRUPT", "ledgerstatus_unreadable:" + status.reason)
        val marker = M0Durable.read(File(dir, "protection.established"))
        val r = M0Durable.read(ledger)
        out.put("ledger_mtime_ms", if (ledger.exists()) ledger.lastModified() else JSONObject.NULL)
        out.put("ledger_age_ms", if (ledger.exists()) nowMs - ledger.lastModified() else JSONObject.NULL)
        val o: JSONObject = when (r) {
            is M0Durable.Read.Corrupt -> return unknown("LEDGER_CORRUPT", "ledger_unreadable:" + r.reason)
            is M0Durable.Read.Absent -> if (marker is M0Durable.Read.Absent) {
                return out.put("known", true).put("integrity", "NO_EVIDENCE").put("active", 0).put("ledger_bytes", 0)
                    .put("counts", JSONObject().put("STAGED", 0).put("LAG_EXCEEDED", 0).put("ARCHIVE_ACKED", 0).put("RELEASED", 0))
                    .put("rejected_total", 0).put("identity_conflicts_total", 0)
                    .let { util(it, 0, 0L, cap, maxBytes) }
            } else return unknown("LEDGER_CORRUPT", "established_ledger_lost")
            is M0Durable.Read.Ok -> r.json
        }
        val e = o.optJSONObject("entries") ?: return unknown("MALFORMED", "entries_not_object")
        val c = JSONObject().put("STAGED", 0).put("LAG_EXCEEDED", 0).put("ARCHIVE_ACKED", 0).put("RELEASED", 0)
        for (k in e.keys()) {
            val st = e.optJSONObject(k)?.opt("state") as? String ?: return unknown("MALFORMED", "entry_state_not_string:" + k.take(80))
            c.put(st, c.optInt(st) + 1)
        }
        val active = ACTIVE_STATES.sumOf { c.optInt(it) }
        val rej = o.opt("rejected_total"); val idc = o.opt("identity_conflicts_total")
        if (rej != null && rej !is Number) return unknown("MALFORMED", "rejected_total_not_number")
        if (idc != null && idc !is Number) return unknown("MALFORMED", "identity_conflicts_total_not_number")
        out.put("known", true).put("counts", c).put("active", active).put("ledger_bytes", ledger.length())
            .put("sealed_bytes_recomputed", ResearchProtection.sealedBytes(o))
            .put("rejected_total", (rej as? Number)?.toInt() ?: 0).put("identity_conflicts_total", (idc as? Number)?.toInt() ?: 0)
            .put("compacted_total", o.optJSONObject("compacted")?.optInt("total") ?: 0)
        out.put("integrity", when {
            active >= cap -> "BACKLOG_AT_CAPACITY"
            c.optInt("LAG_EXCEEDED") + out.optInt("rejected_total") > 0 -> "INCOMPLETE_REPLAY"
            c.optInt("STAGED") > 0 -> "BACKLOG"
            e.length() == 0 -> "NO_EVIDENCE"
            else -> "OK"
        })
        return util(out, active, ledger.length(), cap, maxBytes)
    }

    fun util(out: JSONObject, active: Int, ledgerBytes: Long, cap: Int, maxBytes: Long): JSONObject {
        val uCount = active.toDouble() / cap
        val uBytes = (ledgerBytes + ResearchProtection.ENTRY_GROWTH_RESERVE_BYTES.toLong() * active + ResearchProtection.COMPACTION_RESERVE_BYTES) /
            (maxBytes - ResearchProtection.REJECT_RESERVE_BYTES).toDouble()
        return out.put("u_count", uCount).put("u_bytes", uBytes).put("u", maxOf(uCount, uBytes))
    }
}

/**
 * M0 Stage 2 CI-2: read-only LOCAL_DIAGNOSTIC_EXPORT of filesDir/research_m0 to a user-picked SAF document.
 *
 * Read-only contract: the export reads the research directory and never writes into it; it never toggles collection,
 * acknowledges or releases protection, resets counters or mutates the ledger/cohorts (only its own temp file in a
 * separate temp directory, deleted after the copy). Sources: the research directory (all files, recursive, exact
 * bytes, stable relative paths) plus the read-only capacity status and named build metadata. Nothing else.
 *
 * Snapshot: validated optimistic copy. Files are streamed into a temp zip while hashed; afterwards the directory is
 * re-listed and every file re-hashed. Any difference (a concurrent capture write) discards that attempt; after
 * [Limits.maxAttempts] the export is REFUSED_SNAPSHOT_UNSTABLE and nothing is written. Files are never dropped.
 * Corrupt sealed records and missing references are reported in the manifest; bytes are exported as-is (no repair).
 * After the copy the document is re-read through the granted access and every entry is verified against the manifest.
 */
object ResearchExport {
    const val SCHEMA = "research_m0_export_v1"
    const val ROOT = "research_m0/"
    const val MANIFEST = "export_manifest.json"
    const val SUMS = "SHA256SUMS.txt"
    const val PROTOCOL_NAME = "special_session_protocol_v1"

    data class Limits(val maxTotalBytes: Long = 256L * 1024 * 1024, val maxFiles: Int = 20_000, val maxAttempts: Int = 3,
                      val freeMarginBytes: Long = 16L * 1024 * 1024, val bufferBytes: Int = 64 * 1024,
                      val validateMaxBytes: Long = 8L * 1024 * 1024, val retryBackoffMs: Long = 150)

    class Cancelled : Exception("CANCELLED")
    private class Unstable(val changed: List<String>) : Exception("UNSTABLE")

    /** Test-only hook: called after each source file is copied (lets tests write concurrently). Inert by default. */
    @Volatile var afterFileHook: ((String, Int) -> Unit)? = null
    @Volatile var lastResult: JSONObject? = null
    @Volatile var cancelRequested = false
    @Volatile var running = false

    data class Src(val rel: String, val file: File, val size: Long)

    fun list(dir: File): List<Src> {
        if (!dir.isDirectory) return emptyList()
        val root = dir.canonicalFile
        return dir.walkTopDown().filter { it.isFile }.map { f ->
            val rel = f.canonicalFile.relativeTo(root).invariantSeparatorsPath
            if (rel.startsWith("..")) throw IOException("PATH_ESCAPE:" + f.name)
            Src(rel, f, f.length())
        }.sortedBy { it.rel }.toList()
    }

    private fun hex(d: ByteArray) = d.joinToString("") { "%02x".format(it) }

    private fun hashFile(f: File, buf: ByteArray): String {
        val md = MessageDigest.getInstance("SHA-256")
        FileInputStream(f).use { i -> while (true) { val n = i.read(buf); if (n < 0) break; md.update(buf, 0, n) } }
        return hex(md.digest())
    }

    private fun ist(ms: Long) = Instant.ofEpochMilli(ms).atZone(ResearchCalendar.IST).toOffsetDateTime().toString()

    /**
     * Run the export. [openSink] opens the user-granted document for writing (called only once a verified snapshot
     * exists); [openReadback] re-opens it for reading (null = readback unavailable, reported). Returns the result JSON;
     * never throws for expected failures.
     */
    fun export(dir: File, tmpDir: File, identity: JSONObject, nowMs: Long, openSink: () -> OutputStream,
               openReadback: (() -> InputStream)?, cancel: () -> Boolean = { cancelRequested },
               limits: Limits = Limits(), clock: () -> Long = { System.currentTimeMillis() },
               freeSpace: (File) -> Long = { it.usableSpace }): JSONObject {
        val t0 = clock()
        val res = JSONObject().put("schema", "$SCHEMA:result").put("started_ms", nowMs).put("sink_written", false)
        var tmp: File? = null
        var peakTemp = 0L
        try {
            val buf = ByteArray(limits.bufferBytes)
            val failures = JSONArray()
            var snap: Snapshot? = null
            for (attempt in 1..limits.maxAttempts) {
                if (cancel()) throw Cancelled()
                val srcs = list(dir)
                val total = srcs.sumOf { it.size }
                res.put("source_files", srcs.size).put("source_bytes", total)
                if (srcs.size > limits.maxFiles) return finish(res.put("status", "REFUSED_TOO_MANY_FILES"), t0, clock, peakTemp)
                if (total > limits.maxTotalBytes) return finish(res.put("status", "REFUSED_TOO_LARGE"), t0, clock, peakTemp)
                tmpDir.mkdirs()
                val need = total + total / 10 + limits.freeMarginBytes
                val free = freeSpace(tmpDir)
                if (free < need) return finish(res.put("status", "REFUSED_INSUFFICIENT_SPACE").put("free_bytes", free).put("needed_bytes", need), t0, clock, peakTemp)
                val t = File(tmpDir, "research_export_${nowMs}_$attempt.zip.part")
                require(!t.canonicalPath.startsWith(dir.canonicalPath + File.separator)) { "TEMP_INSIDE_RESEARCH_DIR" }
                tmp = t
                try {
                    snap = copySnapshot(dir, srcs, t, identity, nowMs, attempt, failures, limits, buf, cancel)
                    peakTemp = maxOf(peakTemp, t.length())
                    break
                } catch (u: Unstable) {
                    peakTemp = maxOf(peakTemp, t.length())
                    failures.put(JSONObject().put("attempt", attempt).put("changed", JSONArray(u.changed.take(50))).put("changed_count", u.changed.size))
                    t.delete(); tmp = null
                    if (attempt < limits.maxAttempts) Thread.sleep(limits.retryBackoffMs * attempt)
                }
            }
            res.put("snapshot_attempts", failures.length() + (if (snap != null) 1 else 0)).put("snapshot_failures", failures)
            val s = snap ?: return finish(res.put("status", "REFUSED_SNAPSHOT_UNSTABLE"), t0, clock, peakTemp)
            res.put("manifest_sha256", s.manifestSha).put("zip_sha256", s.zipSha).put("zip_bytes", s.zipBytes)
                .put("evidence_status", s.evidenceStatus).put("corrupt_count", s.corrupt).put("missing_reference_count", s.missingRefs)
            if (cancel()) throw Cancelled()
            // copy temp -> granted document
            try {
                openSink().use { out ->
                    res.put("sink_written", true)
                    FileInputStream(s.file).use { i -> while (true) {
                        if (cancel()) throw Cancelled()
                        val n = i.read(buf); if (n < 0) break; out.write(buf, 0, n) } }
                    out.flush()
                }
            } catch (c: Cancelled) { throw c } catch (e: Exception) {
                return finish(res.put("status", "FAILED_IO").put("stage", "write_document").put("error", e.javaClass.simpleName + ":" + (e.message ?: "").take(120)), t0, clock, peakTemp)
            }
            // readback through the granted access
            val rb = if (openReadback == null) JSONObject().put("ok", false).put("reason", "READBACK_UNAVAILABLE")
                     else try { openReadback().use { verifyStream(it, s.zipSha, buf) } } catch (e: Exception) {
                         JSONObject().put("ok", false).put("reason", "READBACK_IO:" + e.javaClass.simpleName) }
            res.put("readback", rb)
            res.put("status", if (rb.optBoolean("ok")) "COMPLETE_VERIFIED" else "WRITTEN_READBACK_FAILED")
            return finish(res, t0, clock, peakTemp)
        } catch (c: Cancelled) {
            return finish(res.put("status", "CANCELLED").put("partial_output",
                if (res.optBoolean("sink_written")) "DOCUMENT_INCOMPLETE_NOT_VALID" else "NONE_WRITTEN"), t0, clock, peakTemp)
        } catch (e: Exception) {
            return finish(res.put("status", "FAILED_IO").put("stage", "snapshot").put("error", e.javaClass.simpleName + ":" + (e.message ?: "").take(120)), t0, clock, peakTemp)
        } finally {
            tmp?.delete()
        }
    }

    private fun finish(r: JSONObject, t0: Long, clock: () -> Long, peakTemp: Long): JSONObject {
        r.put("elapsed_ms", clock() - t0).put("peak_temp_bytes", peakTemp)
        lastResult = r
        return r
    }

    private class Snapshot(val file: File, val zipSha: String, val zipBytes: Long, val manifestSha: String,
                           val evidenceStatus: String, val corrupt: Int, val missingRefs: Int)

    private fun copySnapshot(dir: File, srcs: List<Src>, t: File, identity: JSONObject, nowMs: Long, attempt: Int,
                             failures: JSONArray, limits: Limits, buf: ByteArray, cancel: () -> Boolean): Snapshot {
        val files = JSONArray(); val corrupt = JSONArray(); val errors = JSONArray()
        val sealedBodies = HashMap<String, JSONObject>()
        val shaByRel = LinkedHashMap<String, String>()
        val zipMd = MessageDigest.getInstance("SHA-256")
        val sums = StringBuilder()
        var result: Triple<String, String, Pair<Int, Int>>? = null
        java.security.DigestOutputStream(FileOutputStream(t), zipMd).use { fos ->
            ZipOutputStream(fos).use { zip ->
                srcs.forEachIndexed { idx, s ->
                    if (cancel()) throw Cancelled()
                    val md = MessageDigest.getInstance("SHA-256")
                    zip.putNextEntry(ZipEntry(ROOT + s.rel).also { it.time = 0L })
                    var n = 0L
                    val small = s.size <= limits.validateMaxBytes
                    val keep = if (small) java.io.ByteArrayOutputStream(s.size.toInt().coerceAtLeast(0)) else null
                    try {
                        FileInputStream(s.file).use { i -> while (true) { val k = i.read(buf); if (k < 0) break
                            md.update(buf, 0, k); zip.write(buf, 0, k); keep?.write(buf, 0, k); n += k } }
                    } catch (e: IOException) {
                        if (!s.file.exists()) throw Unstable(listOf(s.rel + ":vanished")) else throw e
                    }
                    zip.closeEntry()
                    val sha = hex(md.digest()); shaByRel[s.rel] = sha
                    val fo = JSONObject().put("path", ROOT + s.rel).put("size", n).put("sha256", sha)
                    val name = s.file.name
                    val sealedCandidate = name.endsWith(".json") || name.endsWith(".established")
                    when {
                        name.endsWith(".tmp") || name.endsWith(".rejected") -> fo.put("record", "TEMP_OR_QUARANTINE_AS_IS")
                        !sealedCandidate -> fo.put("record", "UNSEALED_AS_IS")
                        keep == null -> fo.put("record", "SEALED_NOT_CHECKED_SIZE")
                        else -> when (val v = M0Durable.validate(keep.toString(Charsets.UTF_8.name()), M0Durable.kindOf(name))) {
                            is M0Durable.Read.Ok -> { fo.put("record", "SEALED_OK"); sealedBodies[s.rel] = v.json }
                            is M0Durable.Read.Corrupt -> { fo.put("record", "CORRUPT").put("reason", v.reason)
                                corrupt.put(JSONObject().put("path", ROOT + s.rel).put("reason", v.reason)) }
                            else -> fo.put("record", "UNKNOWN")
                        }
                    }
                    files.put(fo)
                    sums.append(sha).append("  ").append(ROOT).append(s.rel).append('\n')
                    afterFileHook?.invoke(s.rel, idx)
                }
                // consistency check: same listing, same bytes
                val again = list(dir)
                val changed = ArrayList<String>()
                val before = srcs.associateBy { it.rel }
                for (a in again) if (!before.containsKey(a.rel)) changed.add(a.rel + ":added")
                val nowRel = again.associateBy { it.rel }
                for (b in srcs) {
                    val a = nowRel[b.rel]
                    if (a == null) { changed.add(b.rel + ":removed"); continue }
                    if (a.size != b.size) { changed.add(b.rel + ":size"); continue }
                    if (hashFile(a.file, buf) != shaByRel[b.rel]) changed.add(b.rel + ":bytes")
                }
                if (changed.isNotEmpty()) throw Unstable(changed)
                val missing = missingReferences(srcs.map { it.rel }.toSet(), sealedBodies, errors)
                val evidence = when {
                    corrupt.length() > 0 || missing.length() > 0 -> "FINDINGS_REPORTED"
                    errors.length() > 0 -> "REFERENCES_UNCHECKED"
                    else -> "NO_FINDINGS"
                }
                val manifest = JSONObject().put("schema", SCHEMA).put("export_kind", "LOCAL_DIAGNOSTIC_EXPORT")
                    .put("created_ms", nowMs).put("created_ist", ist(nowMs)).put("device_tz", ZoneId.systemDefault().id)
                    .put("build", identity)
                    .put("calendar_version", ResearchCalendar.VERSION).put("collector_schema", ResearchCollector.SCHEMA)
                    .put("protocol_ref", JSONObject().put("name", PROTOCOL_NAME)
                        .put("sha256", JSONObject.NULL).put("note", "protocol file is not embedded in the app; bind offline by name+sha"))
                    .put("scope", JSONObject()
                        .put("included", JSONArray(listOf("filesDir/research_m0/** (every file, exact bytes)", "research capacity status (read-only)", "named build identity")))
                        .put("excluded", JSONArray(listOf("shared preferences", "credentials/tokens", "app logs", "all files outside filesDir/research_m0"))))
                    .put("read_only", true).put("side_effects", "NONE: no toggle, ack, release, counter reset or ledger/cohort write")
                    .put("snapshot", JSONObject().put("strategy", "validated_optimistic_copy_relist_rehash").put("attempt", attempt)
                        .put("previous_attempt_failures", failures))
                    .put("capacity_status", ResearchCapacity.read(dir, nowMs))
                    .put("files", files).put("file_count", files.length()).put("total_bytes", srcs.sumOf { it.size })
                    .put("corrupt", corrupt).put("missing_references", missing).put("errors", errors)
                    .put("completeness", if (errors.length() == 0) "ALL_FILES_INCLUDED" else "ALL_FILES_INCLUDED_REFERENCE_CHECK_ERRORS")
                    .put("evidence_status", evidence)
                val mBytes = manifest.toString(2).toByteArray(Charsets.UTF_8)
                val mSha = M0Durable.sha256(mBytes)
                zip.putNextEntry(ZipEntry(MANIFEST).also { it.time = 0L }); zip.write(mBytes); zip.closeEntry()
                sums.append(mSha).append("  ").append(MANIFEST).append('\n')
                zip.putNextEntry(ZipEntry(SUMS).also { it.time = 0L }); zip.write(sums.toString().toByteArray(Charsets.UTF_8)); zip.closeEntry()
                zip.finish()
                fos.flush()
                result = Triple(mSha, evidence, corrupt.length() to missing.length())
            }
        }
        val (mSha, evidence, counts) = result ?: throw IOException("SNAPSHOT_NOT_FINISHED")
        return Snapshot(t, hex(zipMd.digest()), t.length(), mSha, evidence, counts.first, counts.second)
    }

    /** Manifests -> imap versions; protection entries -> envelope / manifest files. Missing targets are findings. */
    private fun missingReferences(rels: Set<String>, bodies: Map<String, JSONObject>, errors: JSONArray): JSONArray {
        val out = JSONArray()
        for ((rel, m) in bodies) {
            if (!rel.startsWith("manifest_")) continue
            val idx = m.optJSONObject("indices") ?: continue
            for (i in idx.keys()) { val sl = idx.optJSONObject(i)?.optJSONObject("slots") ?: continue
                for (k in sl.keys()) { val a = sl.optJSONObject(k)?.optJSONArray("attempts") ?: continue
                    for (j in 0 until a.length()) {
                        val v = a.optJSONObject(j)?.optJSONObject("capture")?.optString("imap_version")?.takeIf { it.isNotBlank() && it != "null" } ?: continue
                        val f = "imap_" + v.replace(Regex("[^A-Za-z0-9_.-]"), "_").take(80) + ".json"
                        if (f !in rels) out.put(JSONObject().put("from", ROOT + rel).put("ref", "imap:$v").put("expected", ROOT + f))
                    } } }
        }
        val prot = bodies["protection.json"]
        if (prot == null && "protection.json" in rels) errors.put("protection.json not checked (corrupt or too large)")
        prot?.optJSONObject("entries")?.let { e ->
            for (id in e.keys()) {
                val x = e.optJSONObject(id) ?: continue
                if (x.optString("state") == "RELEASED") continue
                when {
                    id.startsWith("env|") -> { val f = "env_" + id.removePrefix("env|").replace(Regex("[^A-Za-z0-9_.-]"), "_").take(80) + ".json"
                        if (f !in rels) out.put(JSONObject().put("from", ROOT + "protection.json").put("ref", id).put("expected", ROOT + f)) }
                    id.startsWith("manifest|") -> { val f = "manifest_" + id.removePrefix("manifest|").replace(Regex("[^A-Za-z0-9_.-]"), "_").take(80) + ".json"
                        if (f !in rels) out.put(JSONObject().put("from", ROOT + "protection.json").put("ref", id).put("expected", ROOT + f)) }
                }
            }
        }
        return out
    }

    /** Independent readback: whole-stream SHA-256 must equal [expectedZipSha]; every entry re-hashed vs the manifest. */
    fun verifyStream(input: InputStream, expectedZipSha: String?, buf: ByteArray = ByteArray(64 * 1024)): JSONObject {
        val md = MessageDigest.getInstance("SHA-256")
        val dis = DigestInputStream(input, md)
        val got = LinkedHashMap<String, Pair<String, Long>>()
        var manifestBytes: ByteArray? = null
        ZipInputStream(dis).let { z ->
            while (true) {
                val e = z.nextEntry ?: break
                val emd = MessageDigest.getInstance("SHA-256"); var n = 0L
                val keep = if (e.name == MANIFEST) java.io.ByteArrayOutputStream() else null
                while (true) { val k = z.read(buf); if (k < 0) break; emd.update(buf, 0, k); keep?.write(buf, 0, k); n += k }
                got[e.name] = hex(emd.digest()) to n
                if (keep != null) manifestBytes = keep.toByteArray()
            }
            while (dis.read(buf) >= 0) { }
        }
        val zipSha = hex(md.digest())
        val r = JSONObject().put("zip_sha256", zipSha).put("entries", got.size)
        val mb = manifestBytes ?: return r.put("ok", false).put("reason", "MANIFEST_ABSENT")
        val m = try { JSONObject(String(mb, Charsets.UTF_8)) } catch (_: Exception) { return r.put("ok", false).put("reason", "MANIFEST_UNPARSEABLE") }
        val files = m.optJSONArray("files") ?: return r.put("ok", false).put("reason", "MANIFEST_NO_FILES")
        val bad = JSONArray()
        val listed = HashSet<String>()
        for (i in 0 until files.length()) {
            val f = files.getJSONObject(i); val p = f.getString("path"); listed.add(p)
            val g = got[p]
            if (g == null) bad.put("$p:missing") else if (g.first != f.getString("sha256") || g.second != f.getLong("size")) bad.put("$p:mismatch")
        }
        for (k in got.keys) if (k.startsWith(ROOT) && k !in listed) bad.put("$k:unlisted")
        r.put("files_verified", files.length() - bad.length()).put("mismatches", bad)
        if (expectedZipSha != null && expectedZipSha != zipSha) bad.put("zip_sha256_mismatch")
        return r.put("ok", bad.length() == 0)
    }
}
