package com.marketradar.app

import java.io.File
import java.nio.file.Files
import java.nio.file.LinkOption
import java.nio.file.attribute.BasicFileAttributes
import java.util.concurrent.TimeUnit

/** 27 Nov battery/I-O review: decides whether a scheduled recovery pass may skip
 * re-hashing an inventory that was fully, cryptographically verified on an
 * earlier pass in this process.
 *
 * The gate never authorises a commit, prune, restore or deletion. A skipped pass
 * only returns after the normal owner/device/revision claim (lease renewal) and
 * the restore/monitor-resume checks have already run. Any doubt => FULL pass,
 * which keeps the unchanged full SHA-256 before/after checks and head readback.
 *
 * Change signals (union, any one forces FULL): FileObserver/preference mutation
 * counter, per-file stat signature (size + mtime ns + file key) of every portable
 * file, SHA-256 of every serialised portable preference file, file added/removed,
 * revision/manifest/head drift, restore marker, not-ready/not-monitoring state,
 * no baseline (first pass of a process), and a bounded periodic full scrub.
 * Residual gap (documented, tested): an in-place rewrite with identical size,
 * mtime and file key that also escapes the observer is detected only by the scrub.
 */
object RecoveryPassGate {
    // R2: 6 h (was 30 min). A scrub is a full re-hash of the whole inventory (~14 GiB on the phone); it only
    // catches rewrites that keep size, mtime and file key AND escape the observer. FULL passes are additionally
    // bounded by RecoveryBudget.
    const val SCRUB_INTERVAL_MS = 6 * 60 * 60 * 1000L
    const val SKIP = "SKIP_UNCHANGED"

    data class Baseline(val mutation: Long, val signature: Map<String, String>, val revision: Long,
                        val manifestSha256: String, val verifiedAtElapsed: Long)
    data class Observation(val ready: Boolean, val canMonitor: Boolean, val restorePending: Boolean,
                           val webCaptureFailed: Boolean, val revision: Long, val localRevision: Long,
                           val headManifestSha256: String, val localManifestSha256: String,
                           val mutation: Long, val signature: Map<String, String>, val nowElapsed: Long)

    /** Returns [SKIP] or the first reason a FULL pass is required. */
    fun decide(baseline: Baseline?, o: Observation, scrubIntervalMs: Long = SCRUB_INTERVAL_MS): String = when {
        baseline == null -> "NO_VERIFIED_BASELINE"
        o.restorePending -> "RESTORE_PENDING"
        !o.ready -> "NOT_READY"
        !o.canMonitor -> "NOT_MONITOR_READY"
        o.webCaptureFailed -> "WEB_CAPTURE_FAILED"
        o.revision != o.localRevision || o.revision != baseline.revision -> "REVISION_CHANGED"
        !CloudVault.validHash(o.headManifestSha256) || o.headManifestSha256 != baseline.manifestSha256 ||
            o.localManifestSha256 != baseline.manifestSha256 -> "MANIFEST_CHANGED"
        o.mutation != baseline.mutation -> "MUTATION_SIGNAL"
        o.signature.keys != baseline.signature.keys -> "INVENTORY_CHANGED"
        o.signature != baseline.signature -> "CONTENT_SIGNATURE_CHANGED"
        o.nowElapsed - baseline.verifiedAtElapsed >= scrubIntervalMs || o.nowElapsed < baseline.verifiedAtElapsed -> "SCRUB_DUE"
        else -> SKIP
    }

    /** Stat signature of a file without reading its content. Symlinks are not followed. */
    fun fileSignature(file: File): String {
        val a = Files.readAttributes(file.toPath(), BasicFileAttributes::class.java, LinkOption.NOFOLLOW_LINKS)
        return "f:" + a.size() + ":" + a.lastModifiedTime().to(TimeUnit.NANOSECONDS) + ":" + (a.fileKey()?.toString() ?: "-")
    }
    /** Small serialised preference snapshots are hashed (cryptographic, cheap). */
    fun bytesSignature(bytes: ByteArray): String = "p:" + CloudVault.hash(bytes)
}
