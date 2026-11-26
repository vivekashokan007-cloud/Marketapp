package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/** Recovery data policy. Credentials and process/device leases are never portable. */
object CloudDataPolicy {
    /** Android may expose the app root through /data/user/0 or another root alias.
     * Permit that root alias while rejecting symlinks introduced beneath it.
     */
    fun ownedFile(root: File, file: File): Boolean = try {
        val relative = file.relativeTo(root).invariantSeparatorsPath
        CloudVault.validPath(relative) && file.canonicalPath == File(root.canonicalFile, relative).absolutePath
    } catch (_: Exception) { false }
    private val secret = Regex("(^|[_-])(token|auth_token|access_token|refresh_token|password|secret|api_key|apikey|sandbox_token|authorization|cookie|bearer)([_-]|$)", RegexOption.IGNORE_CASE)
    fun secretKey(key: String) = secret.containsMatchIn(key) || key.startsWith("g1_") || key.startsWith("cloud_")
    fun portablePreference(key: String): Boolean = !secretKey(key) &&
        !key.contains("lease", true) && !key.contains("owner_pid", true) &&
        key !in setOf("service_running", "last_process_start_ms", "execution_sandbox_enabled", "execution_mode",
            "next_market_open_ms", "last_web_url", "last_version")
    fun portablePath(path: String): Boolean = CloudVault.validPath(path) &&
        path.split('/').none { secret.containsMatchIn(it) } &&
        path.substringBefore('/') !in setOf("chaquopy", "forensics", "cloud_vault_v1", "cloud_session_v1", "datastore") &&
        !path.endsWith(".lock") && !path.endsWith(".tmp") && !path.endsWith(".lck")
    fun clean(value: Any?): Any? = when(value) {
        is JSONObject -> JSONObject().also { out -> value.keys().forEach { key ->
            if (!secretKey(key)) out.put(key, clean(value.opt(key)))
        } }
        is JSONArray -> JSONArray().also { out -> for (i in 0 until value.length()) out.put(clean(value.opt(i))) }
        else -> value
    }
    fun cleanStorage(values: JSONObject): JSONObject = JSONObject().also { out ->
        values.keys().forEach { key ->
            if (!secretKey(key) && key != "sb-auth-token" && !key.startsWith("sb-")) {
                val text = values.getString(key)
                val parsed = try { org.json.JSONTokener(text).nextValue() } catch (_: Exception) { null }
                out.put(key, if (parsed is JSONObject || parsed is JSONArray) clean(parsed).toString() else text)
            }
        }
    }
}
