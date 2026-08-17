package com.marketradar.app

import android.content.Context
import android.content.SharedPreferences
import com.marketradar.app.util.LogBuffer
import java.io.File
import java.util.UUID

object BrainResultStore {
    private const val TAG = "BrainResultStore"
    private const val DIR_NAME = "brain_results"
    private const val PREF_BRAIN_RESULT_REF = "brain_result_ref"
    private const val PREF_BRAIN_RESULT_BYTES = "brain_result_bytes"
    private const val PREF_BRAIN_RESULT_SAVED_MS = "brain_result_saved_ms"
    private const val PREF_CANDIDATES_REF = "candidates_ref"
    private const val PREF_CANDIDATES_BYTES = "candidates_bytes"

    fun save(
        context: Context,
        prefs: SharedPreferences,
        brainResult: String,
        candidates: String?
    ): Boolean {
        return try {
            val dir = directory(context)
            val stamp = System.currentTimeMillis().toString()
            val token = UUID.randomUUID().toString().replace("-", "")
            val brainName = "brain_${stamp}_$token.json"
            val brainFile = atomicWrite(dir, brainName, brainResult)
            val editor = prefs.edit()
                .putString(PREF_BRAIN_RESULT_REF, brainFile.name)
                .putLong(PREF_BRAIN_RESULT_BYTES, brainFile.length())
                .putLong(PREF_BRAIN_RESULT_SAVED_MS, System.currentTimeMillis())
                .remove("brain_result")

            if (candidates != null) {
                val candidatesFile = atomicWrite(dir, "candidates_${stamp}_$token.json", candidates)
                editor
                    .putString(PREF_CANDIDATES_REF, candidatesFile.name)
                    .putLong(PREF_CANDIDATES_BYTES, candidatesFile.length())
                    .remove("candidates")
            } else {
                editor
                    .remove(PREF_CANDIDATES_REF)
                    .remove(PREF_CANDIDATES_BYTES)
                    .remove("candidates")
            }

            val ok = editor.commit()
            if (ok) prune(dir)
            val candidatesBytes = prefs.getLong(PREF_CANDIDATES_BYTES, 0L)
            LogBuffer.add(
                if (ok) 'I' else 'W',
                TAG,
                "BRAIN_RESULT_FILE_SAVE: ok=$ok brainBytes=${brainFile.length()} candidatesBytes=$candidatesBytes"
            )
            ok
        } catch (e: Exception) {
            LogBuffer.add('E', TAG, "BRAIN_RESULT_FILE_SAVE_FAIL: ${e.message}")
            false
        }
    }

    fun readBrainResult(context: Context, prefs: SharedPreferences): String {
        return readByRef(context, prefs.getString(PREF_BRAIN_RESULT_REF, null))
            ?: prefs.getString("brain_result", "null")
            ?: "null"
    }

    fun readCandidates(context: Context, prefs: SharedPreferences): String {
        return readByRef(context, prefs.getString(PREF_CANDIDATES_REF, null))
            ?: prefs.getString("candidates", "[]")
            ?: "[]"
    }

    fun hasBrainResult(context: Context, prefs: SharedPreferences): Boolean {
        if (readByRef(context, prefs.getString(PREF_BRAIN_RESULT_REF, null)) != null) return true
        return prefs.getString("brain_result", "null") != "null"
    }

    fun clear(context: Context, prefs: SharedPreferences.Editor): SharedPreferences.Editor {
        val dir = directory(context)
        dir.listFiles()?.forEach { file ->
            if (file.isFile && file.extension == "json") {
                runCatching { file.delete() }
            }
        }
        return prefs
            .remove(PREF_BRAIN_RESULT_REF)
            .remove(PREF_BRAIN_RESULT_BYTES)
            .remove(PREF_BRAIN_RESULT_SAVED_MS)
            .remove(PREF_CANDIDATES_REF)
            .remove(PREF_CANDIDATES_BYTES)
            .remove("brain_result")
            .remove("candidates")
    }

    private fun readByRef(context: Context, ref: String?): String? {
        if (ref.isNullOrBlank()) return null
        return try {
            val file = File(directory(context), ref)
            if (!file.isFile || file.length() <= 0L) null else file.readText(Charsets.UTF_8)
        } catch (e: Exception) {
            LogBuffer.add('W', TAG, "BRAIN_RESULT_FILE_READ_FAIL: ref=$ref error=${e.message}")
            null
        }
    }

    private fun directory(context: Context): File {
        return File(context.filesDir, DIR_NAME).apply { mkdirs() }
    }

    private fun atomicWrite(dir: File, name: String, value: String): File {
        val tmp = File(dir, "$name.tmp")
        val target = File(dir, name)
        tmp.writeText(value, Charsets.UTF_8)
        if (target.exists()) target.delete()
        if (!tmp.renameTo(target)) {
            throw IllegalStateException("rename failed for $name")
        }
        return target
    }

    private fun prune(dir: File) {
        val files = dir.listFiles { file -> file.isFile && file.extension == "json" }
            ?.sortedByDescending { it.lastModified() }
            ?: return
        files.drop(8).forEach { file -> runCatching { file.delete() } }
    }
}
