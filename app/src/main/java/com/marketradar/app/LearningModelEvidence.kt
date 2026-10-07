package com.marketradar.app

import org.json.JSONObject
import java.io.File
import java.security.MessageDigest

/** Installed-file evidence only; never claims that the in-memory predictor reloaded. */
object LearningModelEvidence {
    fun read(file: File): JSONObject {
        if (!file.isFile) return JSONObject().put("status", "MISSING")
        return try {
            val bytes = file.readBytes()
            val model = JSONObject(String(bytes, Charsets.UTF_8))
            val hash = MessageDigest.getInstance("SHA-256").digest(bytes)
                .joinToString("") { "%02x".format(it) }
            JSONObject().put("status", "INSTALLED_FILE_ONLY")
                .put("sha256", hash).put("size_bytes", bytes.size)
                .put("version", model.opt("version") ?: JSONObject.NULL)
                .put("n_train", model.opt("n_train") ?: JSONObject.NULL)
                .put("loaded_predictor_verified", false)
        } catch (_: Exception) {
            JSONObject().put("status", "UNREADABLE")
        }
    }
}
