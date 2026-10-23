package com.marketradar.app

import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.nio.channels.FileChannel
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.nio.file.StandardOpenOption
import java.security.MessageDigest

/**
 * M0 crash-safe sealed file store (Batch 1 W5c/W5d).
 *
 * Write: temp file -> write -> fsync(temp) -> atomic rename over the target (no delete-then-rename) -> fsync(dir).
 * A committed file is always either the complete old or the complete new sealed record.
 *
 * Every record is SEALED: {m0_sealed, kind, owner, sha256(body), body}. A read returns ABSENT, OK or CORRUPT
 * (unparseable, wrong owner/kind, digest mismatch); CORRUPT is never collapsed into ABSENT/empty.
 *
 * Recovery ([recover]): a leftover "<name>.tmp" is NEVER promoted unless the target is absent AND the temp validates
 * (schema, digest, owner, kind-from-name). Otherwise the temp is quarantined as "<name>.tmp.rejected" (bounded).
 *
 * Fault injection (TEST ONLY): when [crashAt] is set (System property "m0.crashAt" in a child JVM) the process
 * halts at that boundary. Inert in production (property never set). This supports PROCESS-CRASH recovery tests;
 * it does not establish hardware power-loss durability.
 */
object M0Durable {
    const val OWNER = "research_m0"
    const val SEALED = 1
    val BOUNDARIES = listOf("mid_tmp_write", "after_tmp_write", "after_tmp_fsync", "after_rename", "after_dir_fsync")
    @Volatile var crashAt: String? = System.getProperty("m0.crashAt")
    @Volatile var crashFile: String? = System.getProperty("m0.crashFile")
    const val MAX_QUARANTINE = 20
    /** Cumulative bytes / fsync'd writes issued by this process (cost measurement; never persisted). */
    @Volatile var bytesWritten = 0L
    @Volatile var writes = 0L

    sealed class Read {
        object Absent : Read()
        data class Ok(val body: String, val json: JSONObject) : Read()
        data class Corrupt(val reason: String) : Read()
    }

    fun sha256(b: ByteArray): String = MessageDigest.getInstance("SHA-256").digest(b).joinToString("") { "%02x".format(it) }
    fun sha256(s: String): String = sha256(s.toByteArray(Charsets.UTF_8))

    private fun boundary(name: String, f: File) {
        val c = crashAt ?: return
        if (c == name && (crashFile == null || crashFile == f.name)) Runtime.getRuntime().halt(77)
    }

    fun kindOf(name: String): String = name.removeSuffix(".tmp").removeSuffix(".json").substringBefore("_").substringBefore(".")

    fun seal(kind: String, body: String): String =
        JSONObject().put("m0_sealed", SEALED).put("kind", kind).put("owner", OWNER).put("sha256", sha256(body)).put("body", body).toString()

    fun write(f: File, body: String) {
        val dir = f.parentFile ?: throw IllegalStateException("NO_PARENT")
        dir.mkdirs()
        val bytes = seal(kindOf(f.name), body).toByteArray(Charsets.UTF_8)
        val tmp = File(dir, f.name + ".tmp")
        bytesWritten += bytes.size; writes++
        FileOutputStream(tmp).use { out ->
            if (crashAt == "mid_tmp_write" && (crashFile == null || crashFile == f.name)) {
                out.write(bytes, 0, bytes.size / 2); out.fd.sync(); boundary("mid_tmp_write", f)
            }
            out.write(bytes)
            boundary("after_tmp_write", f)
            out.fd.sync()
        }
        boundary("after_tmp_fsync", f)
        Files.move(tmp.toPath(), f.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
        boundary("after_rename", f)
        fsyncDir(dir)
        boundary("after_dir_fsync", f)
    }

    fun fsyncDir(dir: File) {
        try { FileChannel.open(dir.toPath(), StandardOpenOption.READ).use { it.force(true) } } catch (_: Exception) {
            // Some filesystems refuse directory fsync; the rename is still atomic. Reported by on-device validation.
        }
    }

    fun validate(text: String, expectedKind: String): Read {
        val o = try { JSONObject(text) } catch (_: Exception) { return Read.Corrupt("unparseable") }
        if (o.optInt("m0_sealed", 0) != SEALED) return Read.Corrupt("not_sealed")
        if (o.optString("owner") != OWNER) return Read.Corrupt("wrong_owner")
        if (o.optString("kind") != expectedKind) return Read.Corrupt("wrong_kind")
        val body = o.optString("body", "")
        if (sha256(body) != o.optString("sha256")) return Read.Corrupt("digest_mismatch")
        val j = try { JSONObject(body) } catch (_: Exception) { return Read.Corrupt("body_unparseable") }
        return Read.Ok(body, j)
    }

    fun read(f: File): Read {
        if (!f.exists()) return Read.Absent
        val t = try { f.readText() } catch (e: Exception) { return Read.Corrupt("io_error:" + e.javaClass.simpleName) }
        return validate(t, kindOf(f.name))
    }

    /** Resolve leftover temp files. Returns a report {promoted:[...], quarantined:[...]}. */
    fun recover(dir: File): JSONObject {
        val promoted = org.json.JSONArray(); val quarantined = org.json.JSONArray()
        dir.listFiles { x -> x.name.endsWith(".tmp") }?.sortedBy { it.name }?.forEach { tmp ->
            val target = File(dir, tmp.name.removeSuffix(".tmp"))
            val v = try { validate(tmp.readText(), kindOf(target.name)) } catch (_: Exception) { Read.Corrupt("io") }
            if (!target.exists() && v is Read.Ok) {
                Files.move(tmp.toPath(), target.toPath(), StandardCopyOption.ATOMIC_MOVE)
                fsyncDir(dir); promoted.put(target.name)
            } else {
                val q = File(dir, tmp.name + ".rejected")
                Files.move(tmp.toPath(), q.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
                quarantined.put(tmp.name + ":" + (if (v is Read.Corrupt) v.reason else "target_committed"))
            }
        }
        dir.listFiles { x -> x.name.endsWith(".rejected") }?.sortedByDescending { it.lastModified() }?.drop(MAX_QUARANTINE)?.forEach { it.delete() }
        return JSONObject().put("promoted", promoted).put("quarantined", quarantined)
    }
}
