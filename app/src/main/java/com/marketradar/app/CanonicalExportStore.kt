package com.marketradar.app

import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.security.MessageDigest
import java.nio.file.Files
import java.nio.file.StandardCopyOption
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
    const val MANIFEST_SCHEMA_VERSION = "market_radar_export_manifest_v1_20260913"
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
        val reason: String?,
        val pointerSource: String? = null
    )

    data class ResolvedGeneration(
        val ok: Boolean,
        val generationId: String?,
        val manifestFile: File?,
        val outcomesFile: File?,
        val snapshotsFile: File?,
        val manifest: JSONObject?,
        val reason: String?,
        val pointerSource: String? = null,
    )

    data class PaperReadResult(
        val ok: Boolean,
        val data: ByteArray?,
        val manifest: JSONObject?,
        val reason: String?,
        val pointerSource: String? = null,
    )

    fun sha256Hex(bytes: ByteArray): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(bytes)
        return digest.joinToString("") { b -> "%02x".format(b) }
    }

    private fun canonicalCommitError(
        manifest: JSONObject,
        outcomesBytes: ByteArray,
        snapshotsBytes: ByteArray,
    ): String? {
        return try {
            if (manifest.optString("schema_version") != MANIFEST_SCHEMA_VERSION ||
                manifest.optString("kind") != "canonical_eval_export" ||
                manifest.optString("dataset_label") != "canonical_evaluation_inputs" ||
                manifest.optBoolean("live_training_eligible", true) ||
                manifest.optString("export_cutoff").isBlank()
            ) return "manifest_contract_invalid"
            val outcomes = org.json.JSONArray(String(outcomesBytes, Charsets.UTF_8))
            val snapshots = org.json.JSONArray(String(snapshotsBytes, Charsets.UTF_8))
            val counts = manifest.optJSONObject("row_count") ?: return "row_count_missing"
            if (counts.optInt("outcomes", -1) != outcomes.length() ||
                counts.optInt("snapshots", -1) != snapshots.length()
            ) return "row_count_mismatch"
            if (manifest.optString("status") == "empty" &&
                (outcomes.length() != 0 || snapshots.length() != 0)
            ) return "empty_status_has_rows"
            null
        } catch (_: Exception) {
            "generation_json_not_array"
        }
    }

    private fun paperCommitError(manifest: JSONObject, bytes: ByteArray): String? {
        return try {
            if (manifest.optString("schema_version") != MANIFEST_SCHEMA_VERSION ||
                manifest.optString("kind") != "paper_trades_export" ||
                manifest.optString("dataset_label") != "paper_research_not_live" ||
                manifest.optBoolean("live_training_eligible", true) ||
                manifest.optString("export_cutoff").isBlank()
            ) return "paper_manifest_contract_invalid"
            val rows = org.json.JSONArray(String(bytes, Charsets.UTF_8))
            if (manifest.optInt("row_count", -1) != rows.length()) return "paper_row_count_mismatch"
            if (manifest.optString("status") == "empty" && rows.length() != 0) {
                return "paper_empty_status_has_rows"
            }
            null
        } catch (_: Exception) {
            "paper_json_not_array"
        }
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
                .put("schema_version", MANIFEST_SCHEMA_VERSION)
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
            if (complete) {
                val commitError = canonicalCommitError(manifest, outcomesBytes, snapshotsBytes)
                if (commitError != null) {
                    writeAttemptPointer(root, manifest, ok = false, reason = commitError)
                    return WriteResult(false, generationId, commitError, manifest)
                }
            }
            writeAttemptPointer(root, manifest, ok = complete, reason = if (complete) null else status.optString("status"))
            if (!complete) {
                return WriteResult(false, generationId, "incomplete_not_committed:${status.optString("status")}", manifest)
            }

            // Atomic commit: replace ONLY the small current pointer.
            val pointer = JSONObject()
                .put("schema_version", MANIFEST_SCHEMA_VERSION)
                .put("generation_id", generationId)
                .put("manifest_file", "$GENERATIONS_DIR/$generationId/manifest.json")
                .put("outcomes_file", "$GENERATIONS_DIR/$generationId/outcomes.json")
                .put("snapshots_file", "$GENERATIONS_DIR/$generationId/snapshots.json")
                .put("checksum_sha256", manifest.getJSONObject("checksum_sha256"))
                .put("status", status.optString("status"))
                .put("export_cutoff", manifest.opt("export_cutoff") ?: JSONObject.NULL)
                .put("committed_at", java.time.Instant.now().toString())
            val previousPointer = if (resolveFromPointer(root, CURRENT_POINTER).ok) {
                try { JSONObject(File(root, CURRENT_POINTER).readText()) } catch (_: Exception) { null }
            } else null
            if (faultAfter == "before_current_pointer") error("fault_injection:before_current_pointer")
            if (faultAfter == "pointer_failure") error("fault_injection:pointer_failure")
            atomicWritePointer(File(root, CURRENT_POINTER), pointer)
            if (faultAfter == "after_current_pointer") error("fault_injection:after_current_pointer")
            // Keep the prior verified generation as recovery. On the very
            // first commit, current itself is the only available last-good.
            atomicWritePointer(File(root, LAST_GOOD_POINTER), previousPointer ?: pointer)
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

    private fun resolveFromPointer(root: File, pointerName: String): ResolvedGeneration {
        val pointerFile = File(root, pointerName)
        if (!pointerFile.isFile) {
            return ResolvedGeneration(false, null, null, null, null, null, "${pointerName}_missing", pointerName)
        }
        return try {
            val pointer = JSONObject(pointerFile.readText())
            if (pointer.optString("schema_version") != MANIFEST_SCHEMA_VERSION) {
                return ResolvedGeneration(false, null, null, null, null, null, "pointer_schema_invalid", pointerName)
            }
            val genId = pointer.optString("generation_id").trim()
            if (genId.isBlank() || genId.contains('/') || genId.contains("..")) {
                return ResolvedGeneration(false, null, null, null, null, null, "generation_id_invalid", pointerName)
            }
            val expectedManifest = "$GENERATIONS_DIR/$genId/manifest.json"
            val expectedOutcomes = "$GENERATIONS_DIR/$genId/outcomes.json"
            val expectedSnapshots = "$GENERATIONS_DIR/$genId/snapshots.json"
            if (pointer.optString("manifest_file") != expectedManifest ||
                pointer.optString("outcomes_file") != expectedOutcomes ||
                pointer.optString("snapshots_file") != expectedSnapshots
            ) {
                return ResolvedGeneration(false, genId, null, null, null, null, "pointer_path_mismatch", pointerName)
            }
            val genDir = File(File(root, GENERATIONS_DIR), genId)
            val manifestFile = File(genDir, "manifest.json")
            val outcomesFile = File(genDir, "outcomes.json")
            val snapshotsFile = File(genDir, "snapshots.json")
            if (!manifestFile.isFile || !outcomesFile.isFile || !snapshotsFile.isFile) {
                return ResolvedGeneration(false, genId, manifestFile, outcomesFile, snapshotsFile, null, "generation_files_missing", pointerName)
            }
            val manifest = JSONObject(manifestFile.readText())
            val manifestChecksums = manifest.optJSONObject("checksum_sha256")
            val pointerChecksums = pointer.optJSONObject("checksum_sha256")
            if (manifest.optString("schema_version") != MANIFEST_SCHEMA_VERSION ||
                manifest.optString("generation_id") != genId ||
                manifest.optString("kind") != "canonical_eval_export" ||
                manifest.optString("dataset_label") != "canonical_evaluation_inputs" ||
                manifest.optString("status") !in setOf("complete", "empty") ||
                manifest.optString("outcomes_file") != "outcomes.json" ||
                manifest.optString("snapshots_file") != "snapshots.json" ||
                manifest.optString("export_cutoff").isBlank() ||
                manifest.optBoolean("live_training_eligible", true) ||
                pointer.optString("status") != manifest.optString("status") ||
                pointer.optString("export_cutoff") != manifest.optString("export_cutoff") ||
                pointerChecksums == null || manifestChecksums == null ||
                pointerChecksums.optString("outcomes") != manifestChecksums.optString("outcomes") ||
                pointerChecksums.optString("snapshots") != manifestChecksums.optString("snapshots")
            ) {
                return ResolvedGeneration(false, genId, manifestFile, outcomesFile, snapshotsFile, manifest, "manifest_contract_invalid", pointerName)
            }
            val outcomes = outcomesFile.readBytes()
            val snapshots = snapshotsFile.readBytes()
            val ck = manifest.optJSONObject("checksum_sha256")
            if (ck == null ||
                sha256Hex(outcomes) != ck.optString("outcomes") ||
                sha256Hex(snapshots) != ck.optString("snapshots")
            ) {
                return ResolvedGeneration(false, genId, manifestFile, outcomesFile, snapshotsFile, manifest, "checksum_mismatch", pointerName)
            }
            val outcomeRows = org.json.JSONArray(String(outcomes, Charsets.UTF_8))
            val snapshotRows = org.json.JSONArray(String(snapshots, Charsets.UTF_8))
            val counts = manifest.optJSONObject("row_count")
            if (counts == null || counts.optInt("outcomes", -1) != outcomeRows.length() ||
                counts.optInt("snapshots", -1) != snapshotRows.length() ||
                (manifest.optString("status") == "empty" && (outcomeRows.length() != 0 || snapshotRows.length() != 0))
            ) {
                return ResolvedGeneration(false, genId, manifestFile, outcomesFile, snapshotsFile, manifest, "row_count_mismatch", pointerName)
            }
            ResolvedGeneration(true, genId, manifestFile, outcomesFile, snapshotsFile, manifest, null, pointerName)
        } catch (e: Exception) {
            ResolvedGeneration(false, null, null, null, null, null, e.message ?: "pointer_read_failed", pointerName)
        }
    }

    fun resolveCurrentFiles(root: File, allowLastGoodRecovery: Boolean = false): ResolvedGeneration {
        val current = resolveFromPointer(root, CURRENT_POINTER)
        if (current.ok || !allowLastGoodRecovery) return current
        return resolveFromPointer(root, LAST_GOOD_POINTER)
    }

    fun readCurrent(root: File, allowLastGoodRecovery: Boolean = false): ReadResult {
        val resolved = resolveCurrentFiles(root, allowLastGoodRecovery)
        if (!resolved.ok || resolved.outcomesFile == null || resolved.snapshotsFile == null) {
            return ReadResult(false, null, null, resolved.manifest, resolved.reason, resolved.pointerSource)
        }
        return try {
            ReadResult(
                true,
                resolved.outcomesFile.readBytes(),
                resolved.snapshotsFile.readBytes(),
                resolved.manifest,
                null,
                resolved.pointerSource,
            )
        } catch (e: Exception) {
            ReadResult(false, null, null, resolved.manifest, e.message, resolved.pointerSource)
        }
    }

    fun writeAttemptPointer(root: File, manifest: JSONObject, ok: Boolean, reason: String?): Boolean {
        val pointer = JSONObject(manifest.toString())
            .put("attempt_ok", ok)
            .put("attempt_reason", reason ?: JSONObject.NULL)
            .put("attempted_at", java.time.Instant.now().toString())
        // last_attempt is distinct from last_good / current — may overwrite attempt only.
        return try {
            atomicWritePointer(File(root, LAST_ATTEMPT_POINTER), pointer)
            true
        } catch (_: Exception) {
            false
        }
    }

    fun cleanupOrphans(root: File, keepId: String, maxKeep: Int = 3) {
        val gens = File(root, GENERATIONS_DIR)
        if (!gens.isDirectory) return
        val dirs = gens.listFiles()?.filter { it.isDirectory }?.sortedByDescending { it.name } ?: return
        val protectedIds = mutableSetOf(keepId)
        for (pointerName in listOf(CURRENT_POINTER, LAST_GOOD_POINTER)) {
            try {
                val id = JSONObject(File(root, pointerName).readText()).optString("generation_id")
                if (id.isNotBlank() && !id.contains('/') && !id.contains("..")) protectedIds.add(id)
            } catch (_: Exception) {
            }
        }
        dirs.forEachIndexed { idx, dir ->
            if (dir.name !in protectedIds && idx >= maxKeep) {
                dir.deleteRecursively()
            }
        }
        // Also ignore staging dirs that never got a manifest.
        dirs.forEach { dir ->
            if (dir.name !in protectedIds && !File(dir, "manifest.json").exists()) {
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
        try {
            Files.move(
                tmp.toPath(),
                target.toPath(),
                StandardCopyOption.ATOMIC_MOVE,
                StandardCopyOption.REPLACE_EXISTING,
            )
        } catch (e: Exception) {
            tmp.delete()
            throw IOException("atomic_pointer_replace_failed:${target.name}", e)
        }
    }

    /** Paper research export committed only through its generation pointer. */
    fun writePaperResearchExport(
        root: File,
        rowsJson: String,
        status: JSONObject
    ): WriteResult {
        val generationId = "paper_${System.currentTimeMillis()}_${UUID.randomUUID().toString().take(8)}"
        val gens = File(root, GENERATIONS_DIR)
        gens.mkdirs()
        val genDir = File(gens, generationId)
        genDir.mkdirs()
        val bytes = rowsJson.toByteArray(Charsets.UTF_8)
        val dataFile = File(genDir, "paper_trades.json")
        writeFsynced(dataFile, bytes)
        val ck = sha256Hex(bytes)
        val manifest = JSONObject(status.toString())
            .put("schema_version", MANIFEST_SCHEMA_VERSION)
            .put("generation_id", generationId)
            .put("file", "paper_trades.json")
            .put("checksum_sha256", ck)
            .put("kind", "paper_trades_export")
            .put("dataset_label", "paper_research_not_live")
            .put("live_training_eligible", false)
            .put("canonical_name", "paper_trades.json")
        writeFsynced(File(genDir, "manifest.json"), manifest.toString().toByteArray(Charsets.UTF_8))
        val complete = status.optString("status") in setOf("complete", "empty")
        if (complete) {
            val commitError = paperCommitError(manifest, bytes)
            if (commitError != null) {
                writeAttemptPointer(root, manifest, ok = false, reason = commitError)
                return WriteResult(false, generationId, commitError, manifest)
            }
        }
        writeAttemptPointer(root, manifest, ok = complete, reason = if (complete) null else status.optString("status"))
        if (!complete) {
            return WriteResult(false, generationId, "incomplete_not_committed", manifest)
        }
        val pointer = JSONObject()
            .put("schema_version", MANIFEST_SCHEMA_VERSION)
            .put("generation_id", generationId)
            .put("manifest_file", "$GENERATIONS_DIR/$generationId/manifest.json")
            .put("paper_file", "$GENERATIONS_DIR/$generationId/paper_trades.json")
            .put("checksum_sha256", ck)
            .put("status", status.optString("status"))
            .put("export_cutoff", manifest.opt("export_cutoff") ?: JSONObject.NULL)
            .put("committed_at", java.time.Instant.now().toString())
        val previousPointer = if (readCurrentPaper(root).ok) {
            try { JSONObject(File(root, CURRENT_POINTER).readText()) } catch (_: Exception) { null }
        } else null
        return try {
            atomicWritePointer(File(root, CURRENT_POINTER), pointer)
            atomicWritePointer(File(root, LAST_GOOD_POINTER), previousPointer ?: pointer)
            cleanupOrphans(root, keepId = generationId)
            WriteResult(true, generationId, null, manifest)
        } catch (e: Exception) {
            writeAttemptPointer(root, manifest, ok = false, reason = e.message)
            WriteResult(false, generationId, e.message, manifest)
        }
    }

    fun readCurrentPaper(root: File, allowLastGoodRecovery: Boolean = false): PaperReadResult {
        fun read(pointerName: String): PaperReadResult {
            val pointerFile = File(root, pointerName)
            if (!pointerFile.isFile) return PaperReadResult(false, null, null, "${pointerName}_missing", pointerName)
            return try {
                val pointer = JSONObject(pointerFile.readText())
                val genId = pointer.optString("generation_id").trim()
                val expectedManifest = "$GENERATIONS_DIR/$genId/manifest.json"
                val expectedPaper = "$GENERATIONS_DIR/$genId/paper_trades.json"
                if (pointer.optString("schema_version") != MANIFEST_SCHEMA_VERSION || genId.isBlank() ||
                    genId.contains('/') || genId.contains("..") ||
                    pointer.optString("manifest_file") != expectedManifest ||
                    pointer.optString("paper_file") != expectedPaper
                ) return PaperReadResult(false, null, null, "paper_pointer_invalid", pointerName)
                val genDir = File(File(root, GENERATIONS_DIR), genId)
                val manifestFile = File(genDir, "manifest.json")
                val dataFile = File(genDir, "paper_trades.json")
                if (!manifestFile.isFile || !dataFile.isFile) {
                    return PaperReadResult(false, null, null, "paper_generation_files_missing", pointerName)
                }
                val manifest = JSONObject(manifestFile.readText())
                val data = dataFile.readBytes()
                val manifestChecksum = manifest.optString("checksum_sha256")
                if (manifest.optString("schema_version") != MANIFEST_SCHEMA_VERSION ||
                    manifest.optString("generation_id") != genId ||
                    manifest.optString("kind") != "paper_trades_export" ||
                    manifest.optString("dataset_label") != "paper_research_not_live" ||
                    manifest.optString("status") !in setOf("complete", "empty") ||
                    manifest.optString("file") != "paper_trades.json" ||
                    manifest.optString("export_cutoff").isBlank() ||
                    manifest.optBoolean("live_training_eligible", true) ||
                    manifestChecksum != sha256Hex(data) ||
                    pointer.optString("checksum_sha256") != manifestChecksum ||
                    pointer.optString("status") != manifest.optString("status") ||
                    pointer.optString("export_cutoff") != manifest.optString("export_cutoff")
                ) return PaperReadResult(false, null, manifest, "paper_manifest_contract_invalid", pointerName)
                val rows = org.json.JSONArray(String(data, Charsets.UTF_8))
                if (manifest.optInt("row_count", -1) != rows.length() ||
                    (manifest.optString("status") == "empty" && rows.length() != 0)
                ) {
                    return PaperReadResult(false, null, manifest, "paper_row_count_mismatch", pointerName)
                }
                PaperReadResult(true, data, manifest, null, pointerName)
            } catch (e: Exception) {
                PaperReadResult(false, null, null, e.message ?: "paper_pointer_read_failed", pointerName)
            }
        }
        val current = read(CURRENT_POINTER)
        if (current.ok || !allowLastGoodRecovery) return current
        return read(LAST_GOOD_POINTER)
    }
}
