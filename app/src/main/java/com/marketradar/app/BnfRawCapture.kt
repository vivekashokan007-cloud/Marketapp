package com.marketradar.app

import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.util.UUID
import java.util.zip.GZIPOutputStream

/** Immutable, lossless Upstox response evidence. These files are included by CloudRecovery. */
object BnfRawCapture {
    const val DIRECTORY = "bnf_raw_capture_v1"

    fun eligible(url: String): Boolean = url.startsWith("https://api.upstox.com/") &&
        (url.contains("/option/chain?") && (url.contains("Nifty%20Bank", true) ||
         url.contains("Nifty+Bank", true) || url.contains("Nifty Bank", true)) ||
         url.contains("/option/contract?") && url.contains("Nifty", true) ||
         url.contains("/market-quote/quotes?") ||
         url.contains("/market/max-pain?") || url.contains("/market/fii?") ||
         url.contains("/market/dii?") || url.contains("/market/oi?") ||
         url.contains("/market/change-oi?") || url.contains("/market/pcr?") ||
         url.contains("/market-quote/option-greek?") ||
         url.contains("/market/status/") || url.contains("/market/timings/"))

    /** Returns false on any storage failure; the caller must surface the capture gap. */
    fun record(root: File, url: String, startedMs: Long, completedMs: Long,
               status: Int, body: ByteArray?, error: String? = null): Boolean = try {
        require(eligible(url))
        val dir = File(root, DIRECTORY).apply { require(isDirectory || mkdirs()) }
        val id = "${startedMs}_${UUID.randomUUID()}"
        val hash = body?.let { CloudVault.hash(it) }
        val payload = body?.let {
            require(dir.usableSpace > it.size.toLong() * 2 + 4L * 1024 * 1024)
            val temp = File.createTempFile("capture_", ".tmp", dir)
            try {
                FileOutputStream(temp).use { file ->
                    // Closing GZIPOutputStream also closes the underlying file descriptor.
                    // Finish the compressed member before syncing the still-open descriptor.
                    GZIPOutputStream(file).apply { write(it); finish(); flush() }
                    file.fd.sync()
                }
                val target = File(dir, "$id.body.gz")
                require(temp.renameTo(target))
                target
            } finally { temp.delete() }
        }
        val metadata = JSONObject()
            .put("schema", "bnf_raw_capture_v1")
            .put("url", url)
            .put("request_started_ms", startedMs)
            .put("request_completed_ms", completedMs)
            .put("http_status", status)
            .put("body_bytes", body?.size ?: 0)
            .put("body_sha256", hash ?: JSONObject.NULL)
            .put("body_file", payload?.name ?: JSONObject.NULL)
            .put("error", error ?: JSONObject.NULL)
        val tmp = File.createTempFile("capture_", ".tmp", dir)
        try {
            FileOutputStream(tmp).use { file ->
                file.write(metadata.toString().toByteArray(Charsets.UTF_8))
                file.fd.sync()
            }
            require(tmp.renameTo(File(dir, "$id.json")))
        } finally { tmp.delete() }
        true
    } catch (_: Exception) { false }
}
