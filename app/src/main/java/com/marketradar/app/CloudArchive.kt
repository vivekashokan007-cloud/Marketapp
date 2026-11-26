package com.marketradar.app

import java.io.File
import java.io.FileOutputStream
import java.security.MessageDigest
import org.json.JSONObject

/** Save-before-trim outbox. Never substitutes a successful local copy for remote acknowledgement. */
object CloudArchive {
    const val DIRECTORY = "cloud_history_outbox_v1"
    @Volatile private var root: File? = null
    fun install(filesDir: File?) { root = filesDir?.canonicalFile }
    fun preserveJson(name: String, value: Any): Boolean {
        val base = root ?: return true
        return try {
            require(name.matches(Regex("[a-z0-9_]+")))
            val bytes = CloudDataPolicy.clean(value).toString().toByteArray()
            val hash = CloudVault.hash(bytes)
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
            val dir = File(base, DIRECTORY).apply { mkdirs() }
            require(dir.usableSpace > source.length() + 4L * 1024 * 1024) { "PENDING_ARCHIVE_DISK_FULL" }
            val tmp = File.createTempFile("archive_", ".tmp", dir)
            val digest = MessageDigest.getInstance("SHA-256")
            FileOutputStream(tmp).use { out -> source.inputStream().use { input ->
                val b = ByteArray(32768)
                while (true) { val n = input.read(b); if (n < 0) break; out.write(b,0,n); digest.update(b,0,n) }
                out.fd.sync()
            } }
            val hash = digest.digest().joinToString("") { "%02x".format(it) }
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
            val hash = MessageDigest.getInstance("SHA-256").also { md -> file.inputStream().use { input ->
                val b=ByteArray(32768); while(true) { val n=input.read(b); if(n<0)break; md.update(b,0,n) }
            } }.digest().joinToString("") { "%02x".format(it) }
            if (hash == entry.optString("sha256")) file.delete()
        }
    }
}
