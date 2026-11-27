package com.marketradar.app

import android.content.Context
import com.marketradar.app.util.LogBuffer
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.time.Instant
import java.time.ZoneId
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.zip.GZIPInputStream

/** Durable phone outbox -> authenticated, owner-scoped raw source table. */
object BnfSourceUploader {
    private val executor = Executors.newSingleThreadScheduledExecutor()
    private val started = AtomicBoolean(false)
    private val ist = ZoneId.of("Asia/Kolkata")

    fun start(context: Context) {
        if (!started.compareAndSet(false, true)) return
        val app = context.applicationContext
        executor.scheduleWithFixedDelay({ runCatching { drain(app) }.onFailure {
            LogBuffer.add('E', "BnfSourceUploader", "DIRECT_CAPTURE_DRAIN_FAIL: ${it.javaClass.simpleName}")
        } }, 5, 30, TimeUnit.SECONDS)
    }

    @Synchronized internal fun drain(c: Context) {
        val session = CloudSession(c)
        val status = c.getSharedPreferences("bnf_source_upload_status", Context.MODE_PRIVATE)
        if (!session.signedIn()) { status.edit().putString("state", "APP_SIGN_IN_REQUIRED").apply(); return }
        val dir = File(c.filesDir, BnfRawCapture.DIRECTORY)
        if (!dir.isDirectory) return
        val acknowledgements = File(c.noBackupFilesDir, "bnf_table_ack_v1").apply { mkdirs() }
        val all = dir.listFiles { file -> file.isFile && file.name.matches(Regex("[0-9]+_[0-9a-fA-F-]{36}\\.json")) }
            ?.sortedBy { it.name } ?: emptyList()
        val pending = all.filter { !File(acknowledgements, it.name.removeSuffix(".json")).exists() }
        var verified = 0
        pending.take(120).forEach { metadata ->
                val id = metadata.name.removeSuffix(".json")
                try {
                    uploadAndVerify(session, metadata, id)
                    val temp = File.createTempFile("ack_", ".tmp", acknowledgements)
                    try {
                        temp.writeText("verified")
                        require(temp.renameTo(File(acknowledgements, id)))
                        verified++
                    } finally { temp.delete() }
                } catch (e: Exception) {
                    val reason = e.message?.takeIf { it.matches(Regex("CAPTURE_[A-Z0-9_]+")) } ?: e.javaClass.simpleName
                    LogBuffer.add('E', "BnfSourceUploader", "DIRECT_CAPTURE_PENDING id=$id reason=$reason")
                    // Keep the original file for the next pass. Continue other captures.
                }
            }
        status.edit().putString("state", if (pending.size == verified) "TABLE_ROWS_VERIFIED" else "UPLOAD_PENDING")
            .putInt("pending", pending.size - verified).putInt("verified", all.size - pending.size + verified)
            .putLong("checked_at_ms", System.currentTimeMillis()).apply()
    }

    private fun uploadAndVerify(session: CloudSession, metaFile: File, id: String) {
        val meta = JSONObject(metaFile.readText())
        require(meta.getString("schema") == BnfRawCapture.DIRECTORY) { "CAPTURE_SCHEMA_MISMATCH" }
        val url = meta.getString("url")
        require(BnfRawCapture.eligible(url)) { "CAPTURE_URL_INELIGIBLE" }
        val bodyFile = meta.optString("body_file", "").takeUnless { it == "null" || it.isBlank() }
        val compressed = bodyFile?.let { name ->
            require(name == "$id.body.gz") { "CAPTURE_BODY_FILE_MISMATCH" }
            val body = File(metaFile.parentFile, name)
            require(body.length() in 1L..4194304L) { "CAPTURE_GZIP_SIZE_LIMIT" }
            body.readBytes().also { require(it.size in 1..4194304) { "CAPTURE_GZIP_SIZE_LIMIT" } }
        }
        var document: JSONObject? = null
        if (compressed != null) {
            val expected = meta.getInt("body_bytes")
            require(expected in 0..(16 * 1024 * 1024)) { "CAPTURE_RAW_SIZE_LIMIT" }
            val raw = GZIPInputStream(compressed.inputStream()).use { gzip ->
                ByteArrayOutputStream().also { output ->
                    val buffer = ByteArray(8192)
                    while (true) {
                        val n = gzip.read(buffer)
                        if (n < 0) break
                        require(output.size() + n <= expected) { "CAPTURE_RAW_LENGTH_OVERFLOW" }
                        output.write(buffer, 0, n)
                    }
                }.toByteArray()
            }
            require(raw.size == expected) { "CAPTURE_RAW_LENGTH_MISMATCH" }
            require(CloudVault.hash(raw) == meta.getString("body_sha256")) { "CAPTURE_BODY_HASH_MISMATCH" }
            document = runCatching { JSONObject(String(raw, Charsets.UTF_8)) }.getOrNull()
        }
        val started = meta.getLong("request_started_ms")
        val received = meta.getLong("request_completed_ms")
        val status = meta.getInt("http_status")
        val source = BnfSourceUrl.parse(url)
        val params = source.params
        val notes = JSONObject().put("url", url).put("capture_schema", BnfRawCapture.DIRECTORY)
            .put("native_sha", BuildConfig.SOURCE_HEAD).put("app_version", BuildConfig.VERSION_NAME)
        for (name in listOf("schema", "source_url", "as_of_date", "status", "source_sha256", "master_decoded_sha256"))
            document?.opt(name)?.let { notes.put(name, it) }
        if (document?.has("data") == true && document?.isNull("data") == true)
            notes.put("missing_reason", "UPSTOX_DATA_NULL")
        val owner = session.session().getJSONObject("user").getString("id")
        val row = JSONObject().put("capture_id", id).put("owner_id", owner)
            .put("source", source.source).put("endpoint", source.endpoint)
            .put("request_params", params)
            .put("instrument_key", params.opt("instrument_key") ?: JSONObject.NULL)
            .put("contract_expiry", params.opt("expiry_date") ?: params.opt("expiry") ?: JSONObject.NULL)
            .put("session_date", Instant.ofEpochMilli(started).atZone(ist).toLocalDate().toString())
            .put("request_started_at", Instant.ofEpochMilli(started).toString())
            .put("response_received_at", Instant.ofEpochMilli(received).toString())
            .put("http_status", status)
            .put("result", when { status == 200 && compressed != null -> "RECEIVED"
                status == 0 -> "NETWORK_ERROR"
                status == 200 -> "INVALID_BODY"
                else -> "HTTP_ERROR" })
            .put("original_bytes", meta.optInt("body_bytes", 0))
            .put("original_sha256", if (compressed == null) JSONObject.NULL else meta.getString("body_sha256"))
            .put("original_body", if (compressed == null) JSONObject.NULL else "\\x" + compressed.joinToString("") { "%02x".format(it) })
            .put("body_encoding", if (compressed == null) "identity" else "gzip")
            .put("stored_bytes", if (compressed == null) JSONObject.NULL else compressed.size)
            .put("notes", notes)
        require(SupabaseClient.saveMarketSourceCapture(row, session.session().getString("access_token"))) {
            "SOURCE_CAPTURE_PENDING_READBACK"
        }
    }
}
