package com.marketradar.app

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.File
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** App-account credentials, NOT Supabase dashboard or broker credentials. Never exported. */
class CloudSession(private val context: Context) {
    companion object { private const val ALIAS = "mr_cloud_session_v1"; private val LOCK = Any() }
    private val file get() = File(context.noBackupFilesDir, "cloud_session_v1")
    private val client = OkHttpClient()
    private fun key(): SecretKey {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").apply {
            init(KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build())
        }.generateKey()
    }
    @Synchronized private fun read(): JSONObject {
        if (!file.exists()) return JSONObject()
        val bytes = file.readBytes(); require(bytes.size > 12) { "SESSION_DAMAGED" }
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, bytes.copyOfRange(0, 12)))
        return JSONObject(String(cipher.doFinal(bytes.copyOfRange(12, bytes.size)), Charsets.UTF_8))
    }
    @Synchronized private fun save(session: JSONObject) {
        if (!session.has("expires_at")) session.put("expires_at", System.currentTimeMillis()/1000 + session.optLong("expires_in", 3600))
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key())
        val tmp = File(file.parentFile, file.name + ".tmp")
        tmp.outputStream().use { it.write(cipher.iv); it.write(cipher.doFinal(session.toString().toByteArray())) }
        require(tmp.renameTo(file)) { "SESSION_SAVE_FAILED" }
    }
    fun signedIn() = try { read().has("refresh_token") } catch (_: Exception) { false }
    fun signIn(email: String, password: String) = synchronized(LOCK) {
        val session = auth("token?grant_type=password", JSONObject().put("email", email).put("password", password))
        require(session.has("access_token") && session.has("refresh_token")) { "SIGN_IN_NOT_COMPLETE" }
        val existingOwner = CloudRecovery.owner(context)
        require(existingOwner.isBlank() || existingOwner == session.getJSONObject("user").getString("id")) { "DIFFERENT_APP_ACCOUNT" }
        save(session)
    }
    fun signUp(email: String, password: String): Boolean = synchronized(LOCK) {
        require(CloudRecovery.owner(context).isBlank()) { "EXISTING_APP_ACCOUNT_SIGN_IN_REQUIRED" }
        val response = auth("signup", JSONObject().put("email", email).put("password", password))
        // Confirmation email may be required. Never treat signup as authenticated by itself.
        if (response.has("access_token") && response.has("refresh_token")) { save(response); return true }
        return false
    }
    fun session(): JSONObject = synchronized(LOCK) {
        var s = read()
        require(s.has("refresh_token")) { "APP_SIGN_IN_REQUIRED" }
        if (s.optLong("expires_at") < System.currentTimeMillis() / 1000 + 120) {
            s = auth("token?grant_type=refresh_token", JSONObject().put("refresh_token", s.getString("refresh_token")))
            require(s.has("refresh_token") && s.has("access_token")) { "SESSION_REFRESH_REQUIRED" }; save(s)
        }
        return s
    }
    private fun auth(path: String, body: JSONObject): JSONObject {
        val request = Request.Builder().url(BuildConfig.SUPABASE_URL + "/auth/v1/" + path)
            .header("apikey", BuildConfig.SUPABASE_ANON_KEY)
            .post(body.toString().toRequestBody("application/json".toMediaType())).build()
        return client.newCall(request).execute().use { r ->
            val result = JSONObject(r.body?.string() ?: "{}")
            require(r.isSuccessful) { "APP_AUTH_HTTP_${r.code}" } // never log response credentials
            result
        }
    }
}
