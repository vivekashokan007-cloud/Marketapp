package com.marketradar.app

import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.security.MessageDigest
import java.util.UUID

/**
 * R3.6: Immutable generation directories + atomic current pointer.
 *
 * Writers never claim multi-file atomicity via independent renameTo/overwrite.
 * Readers load through [current] pointer and verify checksums before use.
 * [last_good] and [last_attempt] remain distinct.
 *
 * Android storage may not guarantee ATOMIC_MOVE across filesystems; the contract
 * is: only the small current pointer is swapped, and readers verify generation
 * integrity. Direct overwrite of committed data bytes is never used while
 * claiming atomicity.
 */
object CanonicalExportStore {
    const val CURRENT_POINTER = "current.json"
    const val LAST_GOOD_POINTER = "last_good.json"
    const val LAST_ATTEMPT_POINTER = "last_attempt.json"
    const val GENERATIONS_DIR = "generations"

    data class WriteResult(
        val ok: Boolean,
        val generationId: String?,
        val reason: String?,
        val manifest: JSONObject?
    )

    data class ReadResult(
        val ok: Boolean,
        val outcomes: ByteArray?,
        val snapshots: ByteArray?,
        val manifest: JSONObject?,
        val reason: String?
    )

    fun sha256Hex(bytes: ByteArray): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(bytes)
        return digest.joinToString("") { b -> "%02x".format(b) }
    }

    fun writeGeneration(
        root: File,
        outcomesBytes: ByteArray,
        snapshotsBytes: ByteArray,
        status: JSONObject,
        cutoff: String? = null,
        /** Test hook: throw after named stage. */
        faultAfter: String? = null
    ): WriteResult {
        val generationId = status.optString("generation_id").ifBlank {
            "gen_${System.currentTimeMillis()}_${UUID.randomUUID().toString().take(8)}"
        }
        val gens = File(root, GENERATIONS_DIR)
        if (!gens.exists()) gens.mkdirs()
        val genDir = File(gens, generationId)
        if (!genDir.mkdirs() && !genDir.isDirectory) {
            return WriteResult(false, generationId, "generation_dir_create_failed", null)
        }
        try {
            val outcomesFile = File(genDir, "outcomes.json")
            val snapshotsFile = File(genDir, "snapshots.json")
            val manifestFile = File(genDir, "manifest.json")

            writeFsynced(outcomesFile, outcomesBytes)
            if (faultAfter == "after_outcomes") error("fault_injection:after_outcomes")

            writeFsynced(snapshotsFile, snapshotsBytes)
            if (faultAfter == "after_snapshots") error("fault_injection:after_snapshots")

            val outcomesCk = sha256Hex(outcomesBytes)
            val snapshotsCk = sha256Hex(snapshotsBytes)
            val manifest = JSONObject(status.toString())
                .put("generation_id", generationId)
                .put("outcomes_file", "outcomes.json")
                .put("snapshots_file", "snapshots.json")
                .put("checksum_sha256", JSONObject()
                    .put("outcomes", outcomesCk)
                    .put("snapshots", snapshotsCk))
                .put("export_cutoff", cutoff ?: status.opt("export_cutoff") ?: JSONObject.NULL)
                .put("immutable", true)
                .put("atomic_contract", "generation_dir_plus_current_pointer")
                .put("android_atomic_move_guaranteed", false)

            val manifestBytes = manifest.toString().toByteArray(Charsets.UTF_8)
            writeFsynced(manifestFile, manifestBytes)
            if (faultAfter == "after_manifest") error("fault_injection:after_manifest")

            // Validate checksums before commit pointer.
            if (sha256Hex(outcomesFile.readBytes()) != outcomesCk ||
                sha256Hex(snapshotsFile.readBytes()) != snapshotsCk
            ) {
                writeAttemptPointer(root, manifest, ok = false, reason = "checksum_revalidate_failed")
                return WriteResult(false, generationId, "checksum_revalidate_failed", manifest)
            }

            val complete = status.optString("status") in setOf("complete", "empty")
            writeAttemptPointer(root, manifest, ok = complete, reason = if (complete) null else status.optString("status"))
            if (!complete) {
                return WriteResult(false, generationId, "incomplete_not_committed:${status.optString("status")}", manifest)
            }

            // Atomic commit: replace ONLY the small current pointer.
            val pointer = JSONObject()
                .put("generation_id", generationId)
                .put("manifest_file", "$GENERATIONS_DIR/$generationId/manifest.json")
                .put("outcomes_file", "$GENERATIONS_DIR/$generationId/outcomes.json")
                .put("snapshots_file", "$GENERATIONS_DIR/$generationId/snapshots.json")
                .put("checksum_sha256", manifest.getJSONObject("checksum_sha256"))
                .put("status", status.optString("status"))
                .put("committed_at", java.time.Instant.now().toString())
            if (faultAfter == "before_current_pointer") error("fault_injection:before_current_pointer")
            atomicWritePointer(File(root, CURRENT_POINTER), pointer)
            atomicWritePointer(File(root, LAST_GOOD_POINTER), pointer)
            cleanupOrphans(root, keepId = generationId)
            return WriteResult(true, generationId, null, manifest)
        } catch (e: Exception) {
            val err = JSONObject(status.toString())
                .put("generation_id", generationId)
                .put("status", "incomplete_error")
                .put("error", e.message ?: "exception")
            writeAttemptPointer(root, err, ok = false, reason = e.message)
            return WriteResult(false, generationId, e.message, err)
        }
    }

    fun readCurrent(root: File): ReadResult {
        val current = File(root, CURRENT_POINTER)
        if (!current.exists()) {
            return ReadResult(false, null, null, null, "current_pointer_missing")
        }
        return try {
            val pointer = JSONObject(current.readText())
            val genId = pointer.optString("generation_id")
            val genDir = File(File(root, GENERATIONS_DIR), genId)
            val manifestFile = File(genDir, "manifest.json")
            val outcomesFile = File(genDir, "outcomes.json")
            val snapshotsFile = File(genDir, "snapshots.json")
            if (!manifestFile.exists() || !outcomesFile.exists() || !snapshotsFile.exists()) {
                return ReadResult(false, null, null, null, "generation_files_missing")
            }
            val manifest = JSONObject(manifestFile.readText())
            val outcomes = outcomesFile.readBytes()
            val snapshots = snapshotsFile.readBytes()
            val ck = manifest.optJSONObject("checksum_sha256")
            if (ck == null ||
                sha256Hex(outcomes) != ck.optString("outcomes") ||
                sha256Hex(snapshots) != ck.optString("snapshots")
            ) {
                return ReadResult(false, null, null, manifest, "checksum_mismatch")
            }
            ReadResult(true, outcomes, snapshots, manifest, null)
        } catch (e: Exception) {
            ReadResult(false, null, null, null, e.message)
        }
    }

    fun writeAttemptPointer(root: File, manifest: JSONObject, ok: Boolean, reason: String?) {
        val pointer = JSONObject(manifest.toString())
            .put("attempt_ok", ok)
            .put("attempt_reason", reason ?: JSONObject.NULL)
            .put("attempted_at", java.time.Instant.now().toString())
        // last_attempt is distinct from last_good / current — may overwrite attempt only.
        atomicWritePointer(File(root, LAST_ATTEMPT_POINTER), pointer)
    }

    fun cleanupOrphans(root: File, keepId: String, maxKeep: Int = 3) {
        val gens = File(root, GENERATIONS_DIR)
        if (!gens.isDirectory) return
        val dirs = gens.listFiles()?.filter { it.isDirectory }?.sortedByDescending { it.name } ?: return
        dirs.forEachIndexed { idx, dir ->
            if (dir.name != keepId && idx >= maxKeep) {
                dir.deleteRecursively()
            }
        }
        // Also ignore staging dirs that never got a manifest.
        dirs.forEach { dir ->
            if (dir.name != keepId && !File(dir, "manifest.json").exists()) {
                dir.deleteRecursively()
            }
        }
    }

    fun writeFsynced(file: File, bytes: ByteArray) {
        FileOutputStream(file).use { fos ->
            fos.write(bytes)
            fos.fd.sync()
        }
    }

    fun atomicWritePointer(target: File, payload: JSONObject) {
        val tmp = File(target.absolutePath + ".tmp")
        writeFsynced(tmp, payload.toString().toByteArray(Charsets.UTF_8))
        if (!tmp.renameTo(target)) {
            // Honest non-ATOMIC_MOVE fallback: still only touches the small pointer file,
            // never the generation data set. Labelled in manifest.android_atomic_move_guaranteed=false.
            target.writeBytes(tmp.readBytes())
            tmp.delete()
        }
    }

    /** Paper research export: sole canonical name paper_trades.json. */
    fun writePaperResearchExport(
        root: File,
        rowsJson: String,
        status: JSONObject
    ): WriteResult {
        val generationId = "paper_${System.currentTimeMillis()}_${UUID.randomUUID().toString().take(8)}"
        val gens = File(root, "paper_generations")
        gens.mkdirs()
        val genDir = File(gens, generationId)
        genDir.mkdirs()
        val bytes = rowsJson.toByteArray(Charsets.UTF_8)
        val dataFile = File(genDir, "paper_trades.json")
        writeFsynced(dataFile, bytes)
        val ck = sha256Hex(bytes)
        val manifest = JSONObject(status.toString())
            .put("generation_id", generationId)
            .put("file", "paper_trades.json")
            .put("checksum_sha256", ck)
            .put("kind", "paper_trades_export")
            .put("dataset_label", "paper_research_not_live")
            .put("live_training_eligible", false)
            .put("canonical_name", "paper_trades.json")
        writeFsynced(File(genDir, "manifest.json"), manifest.toString().toByteArray(Charsets.UTF_8))
        val complete = status.optString("status") in setOf("complete", "empty")
        writeAttemptPointer(root, manifest, ok = complete, reason = if (complete) null else status.optString("status"))
        if (!complete) {
            return WriteResult(false, generationId, "incomplete_not_committed", manifest)
        }
        // Promote canonical paper_trades.json via generation + pointer; no ambiguous app_trades.json.
        val canonical = File(root, "paper_trades.json")
        val tmp = File(root, "paper_trades.json.tmp")
        writeFsynced(tmp, bytes)
        if (!tmp.renameTo(canonical)) {
            writeFsynced(canonical, bytes)
            tmp.delete()
        }
        writeFsynced(File(root, "paper_trades_export_status.json"), manifest.toString().toByteArray(Charsets.UTF_8))
        // Remove ambiguous duplicate if present (deprecated; trainers must not consume).
        val ambiguous = File(root, "app_trades.json")
        if (ambiguous.exists()) {
            val deprecated = File(root, "app_trades.json.deprecated")
            ambiguous.renameTo(deprecated)
        }
        val legacyStatus = File(root, "app_trades_export_status.json")
        if (legacyStatus.exists()) legacyStatus.delete()
        return WriteResult(true, generationId, null, manifest)
    }
}
