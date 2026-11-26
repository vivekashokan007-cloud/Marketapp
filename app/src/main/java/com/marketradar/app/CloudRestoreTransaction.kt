package com.marketradar.app

import java.io.File
import java.io.FileOutputStream
import java.security.MessageDigest
import org.json.JSONObject

/** A crash during activation leaves the marker in place and the app gated.
 * Reapplying the same fully verified staging tree is idempotent. This class
 * deliberately has no trading, network or scheduling side effects.
 */
class CloudRestoreTransaction(private val files: File, private val privateRoot: File) {
    fun activate(stage: File, entries: JSONObject, obsolete: List<File>,
                 preferences: (String, File) -> Unit, installed: (String) -> Unit = {}, committed: () -> Unit = {}) {
        val marker = File(privateRoot, "cloud_restore_pending")
        FileOutputStream(marker).use { it.write(stage.absolutePath.toByteArray()); it.fd.sync() }
        entries.keys().forEach { path ->
            require(CloudVault.validPath(path)) { "UNSAFE_RESTORE_PATH" }
            val input = File(stage, path)
            require(input.canonicalPath.startsWith(stage.canonicalPath + File.separator))
            val entry = entries.getJSONObject(path)
            require(input.length() == entry.getLong("size") && digest(input) == entry.getString("sha256")) { "STAGED_FILE_CHANGED" }
            when {
                path.startsWith("files/") -> {
                    val relative = path.removePrefix("files/")
                    require(CloudDataPolicy.portablePath(relative)) { "FORBIDDEN_RESTORE_FILE" }
                    val target = File(files, relative)
                    require(target.canonicalPath.startsWith(files.canonicalPath + File.separator))
                    target.parentFile!!.mkdirs()
                    val tmp = File(target.parentFile, target.name + ".cloud.tmp")
                    FileOutputStream(tmp).use { out -> input.inputStream().use { it.copyTo(out) }; out.fd.sync() }
                    require(digest(tmp) == entry.getString("sha256")) { "RESTORE_COPY_MISMATCH" }
                    require(tmp.renameTo(target)) { "RESTORE_RENAME_FAILED" }
                }
                path.startsWith("prefs/") && path.endsWith(".json") ->
                    preferences(path.removePrefix("prefs/").removeSuffix(".json"), input)
                else -> error("UNSUPPORTED_RESTORE_ENTRY")
            }
            installed(path)
        }
        obsolete.forEach { source ->
            require(source.canonicalPath.startsWith(files.canonicalPath + File.separator))
            val path = source.relativeTo(files).invariantSeparatorsPath
            val target = File(privateRoot, "cloud_restore_replaced/$path")
            target.parentFile!!.mkdirs()
            if (source.exists()) require(source.renameTo(target)) { "OBSOLETE_CACHE_QUARANTINE_FAILED" }
        }
        committed()
        require(marker.delete()) { "RESTORE_MARKER_REMOVE_FAILED" }
    }
    private fun digest(file: File): String = MessageDigest.getInstance("SHA-256").also { md ->
        file.inputStream().use { input -> val b = ByteArray(32768)
            while (true) { val n = input.read(b); if (n < 0) break; md.update(b,0,n) }
        }
    }.digest().joinToString("") { "%02x".format(it) }
}
