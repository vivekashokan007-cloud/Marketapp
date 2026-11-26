package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.InputStream
import java.io.OutputStream
import java.security.MessageDigest
import java.util.zip.GZIPInputStream
import java.util.zip.GZIPOutputStream

/** Immutable, account-private chunks; the server head is advanced only after readback.
 * Android-free so interrupted uploads, corruption and fresh-device restore can be tested.
 * A checkpoint is a recovery source, not an execution acknowledgement for a trade.
 */
class CloudVault(private val remote: Remote) {
    interface Remote {
        fun put(hash: String, bytes: ByteArray)
        fun get(hash: String): ByteArray
    }
    companion object {
        const val FORMAT = 1
        const val CHUNK = 1024 * 1024
        const val MAX_MANIFEST = 8 * 1024 * 1024
        fun hash(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256")
            .digest(bytes).joinToString("") { "%02x".format(it) }
        fun validPath(path: String): Boolean = path.isNotBlank() && path.length <= 1024 &&
            !path.startsWith("/") && !path.contains('\\') && !path.contains('\u0000') &&
            path.split('/').all { it.isNotEmpty() && it != "." && it != ".." }
        fun validHash(hash: String) = hash.matches(Regex("[a-f0-9]{64}"))
    }
    private fun upload(bytes: ByteArray): String {
        val h = hash(bytes)
        remote.put(h, bytes) // create-only; a duplicate must still pass hash readback
        require(hash(remote.get(h)) == h) { "CLOUD_READBACK_MISMATCH" }
        return h
    }
    fun store(input: InputStream): JSONObject {
        val chunks = JSONArray()
        var total = 0L
        val digest = MessageDigest.getInstance("SHA-256")
        input.use { stream ->
            val buffer = ByteArray(CHUNK)
            while (true) {
                var n = 0
                while (n < buffer.size) {
                    val count = stream.read(buffer, n, buffer.size - n)
                    if (count < 0) break
                    require(count > 0) { "ZERO_LENGTH_READ" }
                    n += count
                }
                if (n == 0) break
                digest.update(buffer, 0, n); total += n
                val packed = java.io.ByteArrayOutputStream().also { output ->
                    GZIPOutputStream(output).use { it.write(buffer, 0, n) }
                }.toByteArray()
                chunks.put(JSONObject().put("hash", upload(packed)).put("size", n))
            }
        }
        return JSONObject().put("chunks", chunks).put("size", total)
            .put("sha256", digest.digest().joinToString("") { "%02x".format(it) })
    }
    fun restore(entry: JSONObject, output: OutputStream) {
        val digest = MessageDigest.getInstance("SHA-256")
        var total = 0L
        val chunks = entry.getJSONArray("chunks")
        output.use { target ->
            for (i in 0 until chunks.length()) {
                val chunk = chunks.getJSONObject(i)
                val h = chunk.getString("hash")
                require(validHash(h)) { "BAD_CHUNK_HASH" }
                val expected = chunk.getInt("size")
                require(expected in 1..CHUNK) { "BAD_CHUNK_SIZE" }
                val packed = remote.get(h)
                require(packed.size <= CHUNK + 4096 && hash(packed) == h) { "CHUNK_CORRUPT" }
                val raw = GZIPInputStream(packed.inputStream()).use { input ->
                    val bounded = java.io.ByteArrayOutputStream()
                    val buf = ByteArray(8192)
                    while (bounded.size() <= expected) {
                        val n = input.read(buf, 0, minOf(buf.size, expected + 1 - bounded.size()))
                        if (n < 0) break
                        if (n == 0) break
                        bounded.write(buf, 0, n)
                    }
                    bounded.toByteArray()
                }
                require(raw.size == expected) { "EXPANSION_SIZE_MISMATCH" }
                digest.update(raw); target.write(raw); total += raw.size
            }
        }
        require(total == entry.getLong("size")) { "FILE_SIZE_MISMATCH" }
        require(digest.digest().joinToString("") { "%02x".format(it) } == entry.getString("sha256")) { "FILE_CORRUPT" }
    }
    fun publishManifest(entries: JSONObject, version: Int): String {
        val manifest = JSONObject().put("format", FORMAT).put("version_code", version).put("entries", entries)
        val bytes = manifest.toString().toByteArray()
        require(bytes.size <= MAX_MANIFEST) { "MANIFEST_LIMIT_NOT_BACKED_UP" }
        return upload(bytes)
    }
    fun manifest(h: String, version: Int): JSONObject {
        require(validHash(h)) { "BAD_MANIFEST_HASH" }
        val bytes = remote.get(h)
        require(bytes.size <= MAX_MANIFEST && hash(bytes) == h) { "MANIFEST_CORRUPT" }
        val manifest = JSONObject(String(bytes, Charsets.UTF_8))
        require(manifest.getInt("format") == FORMAT) { "UNSUPPORTED_CLOUD_FORMAT" }
        require(manifest.getInt("version_code") <= version) { "NEWER_APP_REQUIRED" }
        val entries = manifest.getJSONObject("entries")
        entries.keys().forEach { require(validPath(it)) { "UNSAFE_RESTORE_PATH" } }
        return manifest
    }
    /** All objects are verified into staging before a caller may activate any of them. */
    fun stage(manifest: JSONObject, directory: File) {
        require(!directory.exists()) { "STAGE_ALREADY_EXISTS" }
        require(directory.mkdirs()) { "STAGE_CREATE_FAILED" }
        val entries = manifest.getJSONObject("entries")
        var requiredBytes = 0L
        entries.keys().forEach {
            val size = entries.getJSONObject(it).getLong("size")
            require(size >= 0) { "BAD_FILE_SIZE" }
            requiredBytes = Math.addExact(requiredBytes, size)
        }
        require(requiredBytes <= directory.usableSpace - 8L * 1024 * 1024) { "RESTORE_NEEDS_MORE_CACHE_SPACE" }
        entries.keys().forEach { path ->
            require(validPath(path)) { "UNSAFE_RESTORE_PATH" }
            val target = File(directory, path)
            require(target.canonicalPath.startsWith(directory.canonicalPath + File.separator))
            target.parentFile!!.mkdirs()
            restore(entries.getJSONObject(path), target.outputStream())
        }
    }
}
