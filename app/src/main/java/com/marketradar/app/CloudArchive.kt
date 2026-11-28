package com.marketradar.app

import java.io.File
import java.io.FileOutputStream
import java.security.MessageDigest
import org.json.JSONArray
import org.json.JSONObject

/** Save-before-trim outbox. Never substitutes a successful local copy for remote acknowledgement. */
object CloudArchive {
    const val DIRECTORY = "cloud_history_outbox_v1"
    const val ACK_SCHEMA = "mr.archive_ack.v1"
    private const val ACK_FILE = "cloud_archive_ack_v1.json"
    private const val MAX_ACK_HASHES = 50_000
    @Volatile private var root: File? = null
    @Volatile private var ackRoot: File? = null
    private val ackLock = Any()
    /** Destructive eviction of anything other than already-acknowledged outbox files stays off
     * until measured eligibility and a reviewed build approval. [pruneAcknowledged] is the only
     * local deletion, and only for files whose bytes already match a confirmed checkpoint entry. */
    @Volatile var cleanupEnabled: Boolean = false
    fun install(filesDir: File?, noBackupDir: File? = null) {
        root = filesDir?.canonicalFile
        ackRoot = noBackupDir?.canonicalFile
        if (filesDir == null) synchronized(ackLock) { ackMemo = null; cleanupEnabled = false }
    }
    fun bindAckRoot(noBackupDir: File) { ackRoot = noBackupDir.canonicalFile }
    fun preserveJson(name: String, value: Any): Boolean {
        val base = root ?: return true
        return try {
            require(name.matches(Regex("[a-z0-9_]+")))
            val bytes = CloudDataPolicy.clean(value).toString().toByteArray()
            val hash = CloudVault.hash(bytes)
            if (isAcknowledged(hash)) return true
            val relative = "preference_history/$name.json"
            val id = CloudVault.hash(relative.toByteArray()).take(16) + "_" + hash
            val dir = File(base, DIRECTORY).apply { mkdirs() }
            require(dir.usableSpace > bytes.size + 4L * 1024 * 1024)
            val payload = File(dir, "$id.bin")
            if (!payload.exists()) {
                val tmp = File.createTempFile("archive_", ".tmp", dir)
                FileOutputStream(tmp).use { it.write(bytes); it.fd.sync() }; require(tmp.renameTo(payload))
            }
            val metadata = File(dir, "$id.json")
            if (!metadata.exists()) {
                val tmp = File.createTempFile("archive_", ".tmp", dir)
                FileOutputStream(tmp).use { it.write(JSONObject().put("source_path",relative).put("sha256",hash)
                    .put("bytes",bytes.size).toString().toByteArray()); it.fd.sync() }; require(tmp.renameTo(metadata))
            }
            true
        } catch (_: Exception) { false }
    }
    fun preserveBeforeDiscard(file: File): Boolean {
        val base = root ?: return true // Android-free legacy test calls; Application always installs before writers.
        return try {
            val source = file.canonicalFile
            if (!source.exists()) return true
            require(source.path.startsWith(base.path + File.separator))
            val relative = source.relativeTo(base).invariantSeparatorsPath
            require(CloudDataPolicy.portablePath(relative))
            if (relative.startsWith("$DIRECTORY/")) return false
            val hash = sha256(source)
            if (isAcknowledged(hash)) return true
            val dir = File(base, DIRECTORY).apply { mkdirs() }
            require(dir.usableSpace > source.length() + 4L * 1024 * 1024) { "PENDING_ARCHIVE_DISK_FULL" }
            val tmp = File.createTempFile("archive_", ".tmp", dir)
            source.inputStream().use { input ->
                FileOutputStream(tmp).use { out ->
                    val b = ByteArray(32768)
                    while (true) { val n = input.read(b); if (n < 0) break; out.write(b,0,n) }
                    out.fd.sync()
                }
            }
            val name = CloudVault.hash(relative.toByteArray()).take(16) + "_" + hash
            val payload = File(dir, "$name.bin")
            if (payload.exists()) tmp.delete() else require(tmp.renameTo(payload))
            val metadata = File(dir, "$name.json")
            if (!metadata.exists()) {
                val m = File(dir, "$name.json.tmp")
                FileOutputStream(m).use { it.write(JSONObject().put("source_path",relative).put("sha256",hash)
                    .put("bytes",payload.length()).toString().toByteArray()); it.fd.sync() }
                require(m.renameTo(metadata))
            }
            true
        } catch (_: Exception) { false } // caller must retain the original on failure
    }
    /** Historical checkpoint index retains the immutable cloud references after this local eviction. */
    fun pruneAcknowledged(filesDir: File, entries: JSONObject) {
        val dir = File(filesDir, DIRECTORY)
        dir.listFiles()?.filter { it.isFile && !it.name.endsWith(".tmp") }?.forEach { file ->
            val path = "files/$DIRECTORY/${file.name}"
            val entry = entries.optJSONObject(path) ?: return@forEach
            val hash = sha256(file)
            if (hash == entry.optString("sha256")) {
                recordAcknowledged(hash)
                file.delete()
            }
        }
        recordAcknowledgedEntries(entries)
    }
    /** Paths in [oldEntries] under the archive directory that are no longer live locally.
     * The current head should keep these so a later FULL pass cannot drop recoverability
     * or publish an A/B manifest that only differs by the prune. */
    fun retainedRemoteEntries(oldEntries: JSONObject, liveEntries: JSONObject): JSONObject {
        val out = JSONObject()
        oldEntries.keys().forEach { path ->
            if (path.startsWith("files/$DIRECTORY/") && !liveEntries.has(path)) {
                val entry = oldEntries.optJSONObject(path) ?: return@forEach
                if (CloudVault.validHash(entry.optString("sha256"))) out.put(path, entry)
            }
        }
        return out
    }
    fun isAcknowledged(sha256: String): Boolean {
        if (!CloudVault.validHash(sha256)) return false
        return synchronized(ackLock) { ackHashes().contains(sha256) }
    }
    fun recordAcknowledged(sha256: String) {
        if (!CloudVault.validHash(sha256)) return
        synchronized(ackLock) {
            val set = ackHashes()
            if (!set.add(sha256)) return
            persistAck(set)
        }
    }
    fun recordAcknowledgedEntries(entries: JSONObject) {
        val hashes = entries.keys().asSequence().filter { it.startsWith("files/$DIRECTORY/") }
            .mapNotNull { entries.optJSONObject(it)?.optString("sha256") }
            .filter { CloudVault.validHash(it) }.toList()
        if (hashes.isEmpty()) return
        synchronized(ackLock) {
            val set = ackHashes()
            var changed = false
            hashes.forEach { if (set.add(it)) changed = true }
            if (changed) persistAck(set)
        }
    }
    private fun ackFile(): File? = ackRoot?.let { File(it, ACK_FILE) }
    private var ackMemo: MutableSet<String>? = null
    private fun ackHashes(): MutableSet<String> {
        ackMemo?.let { return it }
        val file = ackFile()
        val set = linkedSetOf<String>()
        if (file != null && file.isFile) runCatching {
            val o = JSONObject(file.readText())
            if (o.optString("schema") == ACK_SCHEMA) {
                val arr = o.optJSONArray("sha256") ?: JSONArray()
                for (i in 0 until arr.length()) {
                    val h = arr.optString(i)
                    if (CloudVault.validHash(h)) set.add(h)
                }
            }
        }
        ackMemo = set
        return set
    }
    private fun persistAck(set: MutableSet<String>) {
        val file = ackFile() ?: return
        while (set.size > MAX_ACK_HASHES) set.remove(set.first())
        val arr = JSONArray()
        set.forEach { arr.put(it) }
        val tmp = File(file.parentFile.apply { mkdirs() }, "$ACK_FILE.tmp")
        FileOutputStream(tmp).use { it.write(JSONObject().put("schema", ACK_SCHEMA).put("count", set.size)
            .put("sha256", arr).toString().toByteArray()); it.fd.sync() }
        if (file.exists()) file.delete()
        require(tmp.renameTo(file))
    }
    internal fun resetForTest() { synchronized(ackLock) { ackMemo = null; ackRoot = null; cleanupEnabled = false } }
    private fun sha256(file: File): String = MessageDigest.getInstance("SHA-256").also { md ->
        file.inputStream().use { input ->
            val b = ByteArray(32768)
            while (true) { val n = input.read(b); if (n < 0) break; md.update(b, 0, n) }
        }
    }.digest().joinToString("") { "%02x".format(it) }
}
