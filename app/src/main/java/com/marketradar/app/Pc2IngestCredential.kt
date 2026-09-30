package com.marketradar.app

import org.json.JSONObject
import java.security.SecureRandom

/**
 * Codex round-2 finding B4: the device credential that authorises compact PC2
 * ingestion.
 *
 * The publishable Supabase key ships inside the APK and authorises nothing on
 * its own. Each install generates a 256-bit random key on first use and keeps
 * it in app-private storage. The server stores only its SHA-256, which the
 * owner registers once in the SQL editor from the hash the app logs. The key
 * itself is never logged.
 *
 * Until the server confirms the credential as registered and active, the
 * compact channel stays off and nothing is enqueued. That is safe only because
 * the legacy PC2 path is authoritative during parity; cutover must require a
 * registered device.
 *
 * Android-free so the rules are unit-testable.
 */
object Pc2IngestCredential {
    const val PREF_DEVICE_KEY = "pc2_ingest_device_key"
    const val PREF_REGISTERED_CONFIRMED = "pc2_ingest_registered_confirmed"

    private val KEY_FORMAT = Regex("[0-9a-f]{64}")

    fun isValidKey(key: String?): Boolean = key != null && KEY_FORMAT.matches(key)

    fun generateKey(random: SecureRandom = SecureRandom()): String {
        val bytes = ByteArray(32)
        random.nextBytes(bytes)
        return bytes.joinToString("") { "%02x".format(it.toInt() and 0xff) }
    }

    /** The only form in which the key may appear in logs or on the server. */
    fun keyHash(key: String): String = Pc2CompactBatch.sha256(key)

    /**
     * Returns the persisted key, creating one when none exists. A new key is
     * returned only once it has been written AND read back, so a failed write can
     * never leave the device using a key that the next process start will not
     * find. Returns null when no key can be persisted.
     */
    fun loadOrCreate(read: () -> String?, write: (String) -> Boolean): String? {
        val existing = read()
        if (isValidKey(existing)) return existing
        val fresh = generateKey()
        if (!write(fresh)) return null
        return fresh.takeIf { read() == it }
    }

    fun ingestRequestBody(deviceKey: String, envelope: JSONObject): JSONObject = JSONObject()
        .put("p_device_key", deviceKey)
        .put("p_policy", JSONObject(envelope.getJSONObject("policy_row").toString()))
        .put("p_batch", JSONObject(envelope.getJSONObject("batch_row").toString()))

    fun statusRequestBody(deviceKey: String): JSONObject =
        JSONObject().put("p_device_key", deviceKey)

    /** True only for an explicit `{"registered":true,"active":true}` answer. */
    fun statusIsActive(responseBody: String?): Boolean {
        val json = runCatching { JSONObject(responseBody ?: return false) }.getOrNull() ?: return false
        return json.optBoolean("registered", false) && json.optBoolean("active", false)
    }
}
