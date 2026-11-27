package com.marketradar.app

import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.util.concurrent.ConcurrentHashMap

/** Owner-directory scoped receipts for immutable objects already uploaded and read back.
 * A receipt is reusable evidence, not a committed checkpoint or a device lease.
 */
class VerifiedRecoveryUploadCache(private val root: File) {
    private val locks = ConcurrentHashMap<String, Any>()
    fun entry(expectedHash: String, uploadAndVerify: () -> JSONObject): JSONObject {
        require(CloudVault.validHash(expectedHash))
        return synchronized(locks.computeIfAbsent(expectedHash) { Any() }) {
            val file = File(root, "$expectedHash.json")
            val existing = runCatching {
                val wrapper = JSONObject(file.readText())
                val body = wrapper.getString("entry")
                require(wrapper.getString("entry_sha256") == CloudVault.hash(body.toByteArray()))
                JSONObject(body).also { require(it.getString("sha256") == expectedHash) }
            }.getOrNull()
            if (existing != null) existing else {
                val entry = uploadAndVerify()
                require(entry.getString("sha256") == expectedHash) { "CACHE_CHANGED_DURING_UPLOAD" }
                require(root.isDirectory || root.mkdirs())
                val body = entry.toString()
                val wrapper = JSONObject().put("entry", body).put("entry_sha256", CloudVault.hash(body.toByteArray()))
                val tmp = File.createTempFile("receipt_", ".tmp", root)
                try {
                    FileOutputStream(tmp).use { output -> output.write(wrapper.toString().toByteArray()); output.fd.sync() }
                    require(tmp.renameTo(file)) { "RECEIPT_SAVE_FAILED" }
                } finally { tmp.delete() }
                entry
            }
        }
    }
}
