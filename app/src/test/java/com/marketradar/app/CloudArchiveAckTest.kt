package com.marketradar.app

import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Test
import java.io.File

/** 28 Nov R3: acknowledged-archive index stops writers recreating already-checkpointed payloads,
 * and retained remote entries keep recoverability on the current head. Cleanup stays disabled. */
class CloudArchiveAckTest {
    private val root = File.createTempFile("archive_ack", "dir").apply { delete(); mkdirs() }
    private val noBackup = File(root, "no_backup").apply { mkdirs() }
    private val files = File(root, "files").apply { mkdirs() }

    @After fun tearDown() { CloudArchive.install(null); CloudArchive.resetForTest(); root.deleteRecursively() }

    @Test fun cleanupStaysDisabled() {
        CloudArchive.install(files, noBackup)
        assertFalse(CloudArchive.cleanupEnabled)
    }

    @Test fun alreadyAcknowledgedPayloadIsNotRewrittenAndANewHashStillIs() {
        CloudArchive.install(files, noBackup)
        assertTrue(CloudArchive.preserveJson("closed_trade_ledger", org.json.JSONArray().put("a")))
        val dir = File(files, CloudArchive.DIRECTORY)
        val first = dir.listFiles()!!.filter { it.extension == "bin" }
        assertEquals(1, first.size)
        val hash = CloudVault.hash(first[0].readBytes())
        CloudArchive.recordAcknowledged(hash)
        first.forEach { it.delete() }
        dir.listFiles()?.forEach { it.delete() }
        assertTrue(CloudArchive.preserveJson("closed_trade_ledger", org.json.JSONArray().put("a")))
        assertEquals(0, dir.listFiles()?.size ?: 0) // identical content is not recreated
        assertTrue(CloudArchive.preserveJson("closed_trade_ledger", org.json.JSONArray().put("b")))
        assertEquals(1, dir.listFiles()!!.count { it.extension == "bin" })
    }

    @Test fun pruneRecordsAckAndLeavesUnreferencedFiles() {
        CloudArchive.install(files, noBackup)
        assertTrue(CloudArchive.preserveJson("closed_trade_ledger", org.json.JSONArray().put("keep")))
        val dir = File(files, CloudArchive.DIRECTORY)
        val bin = dir.listFiles()!!.first { it.extension == "bin" }
        val pending = File(dir, "not_in_manifest.bin").apply { writeText("pending-not-ack") }
        val entries = JSONObject().put("files/${CloudArchive.DIRECTORY}/${bin.name}",
            JSONObject().put("sha256", CloudVault.hash(bin.readBytes())).put("size", bin.length()))
        CloudArchive.pruneAcknowledged(files, entries)
        assertFalse(bin.exists())
        assertTrue(pending.exists()) // unreferenced / pending file is not deleted
        assertTrue(CloudArchive.isAcknowledged(CloudVault.hash("pending-not-ack".toByteArray())).not())
        assertTrue(CloudArchive.preserveJson("closed_trade_ledger", org.json.JSONArray().put("keep")))
        assertFalse(dir.listFiles()!!.any { it.extension == "bin" && it.name != pending.name })
    }

    @Test fun retainedRemoteEntriesKeepArchiveReferencesTheLiveInventoryDropped() {
        val old = JSONObject()
            .put("files/${CloudArchive.DIRECTORY}/a.bin", JSONObject().put("sha256", "a".repeat(64)).put("size", 4))
            .put("files/single_leg_v1/positions.json", JSONObject().put("sha256", "b".repeat(64)).put("size", 8))
        val live = JSONObject().put("files/single_leg_v1/positions.json",
            JSONObject().put("sha256", "b".repeat(64)).put("size", 8))
        val retained = CloudArchive.retainedRemoteEntries(old, live)
        assertEquals(1, retained.length())
        assertTrue(retained.has("files/${CloudArchive.DIRECTORY}/a.bin"))
        assertFalse(retained.has("files/single_leg_v1/positions.json"))
    }

    @Test fun preserveBeforeDiscardOfAnAcknowledgedFileDoesNotCreateALocalCopy() {
        CloudArchive.install(files, noBackup)
        val src = File(files, "research_m0/row.jsonl").apply { parentFile!!.mkdirs(); writeText("row-bytes") }
        val hash = CloudVault.hash(src.readBytes())
        CloudArchive.recordAcknowledged(hash)
        assertTrue(CloudArchive.preserveBeforeDiscard(src))
        val dir = File(files, CloudArchive.DIRECTORY)
        assertEquals(0, dir.listFiles()?.size ?: 0)
        assertTrue(src.exists()) // the caller decides whether to delete the original
    }

    private fun archiveBin(dir: File, sha: String) = dir.listFiles()!!.single { it.name.endsWith("_$sha.bin") }
    private fun archiveMeta(dir: File, sha: String) = dir.listFiles()!!.single { it.name.endsWith("_$sha.json") }

    @Test fun mutationBetweenEarlyHashAndCopyFailsAndKeepsTheSource() {
        CloudArchive.install(files, noBackup)
        val src = File(files, "research_m0/row.jsonl").apply { parentFile!!.mkdirs(); writeText("snapshot-v1") }
        val intended = src.readBytes()
        CloudArchive.testAfterEarlyHash = { it.writeText("snapshot-v2") }
        assertFalse(CloudArchive.preserveBeforeDiscard(src))
        assertEquals("snapshot-v2", src.readText()) // source retained
        val dir = File(files, CloudArchive.DIRECTORY)
        assertEquals(0, dir.listFiles()?.size ?: 0)
        assertFalse(intended.contentEquals(src.readBytes()))
    }

    @Test fun mutationDuringCopyFailsAndKeepsTheSource() {
        CloudArchive.install(files, noBackup)
        val src = File(files, "research_m0/row.jsonl").apply { parentFile!!.mkdirs(); writeBytes(ByteArray(80_000) { 0x41 }) }
        CloudArchive.testDuringCopy = { it.writeBytes(ByteArray(80_000) { 0x42 }) }
        assertFalse(CloudArchive.preserveBeforeDiscard(src))
        assertTrue(src.exists())
        assertTrue(src.readBytes().all { it == 0x42.toByte() })
        val dir = File(files, CloudArchive.DIRECTORY)
        assertEquals(0, dir.listFiles()?.size ?: 0)
    }

    @Test fun preexistingPayloadForTheEarlyHashDoesNotCountAsSuccessAfterARewrite() {
        CloudArchive.install(files, noBackup)
        val src = File(files, "research_m0/row.jsonl").apply { parentFile!!.mkdirs(); writeText("snapshot-v1") }
        assertTrue(CloudArchive.preserveBeforeDiscard(src))
        val dir = File(files, CloudArchive.DIRECTORY)
        val early = CloudVault.hash("snapshot-v1".toByteArray())
        val payload = archiveBin(dir, early)
        assertTrue(payload.exists())
        val originalPayload = payload.readBytes()
        CloudArchive.testAfterEarlyHash = { it.writeText("snapshot-v2") }
        assertFalse(CloudArchive.preserveBeforeDiscard(src))
        assertEquals("snapshot-v2", src.readText())
        assertArrayEquals(originalPayload, payload.readBytes()) // leftover v1 archive is unchanged
        assertFalse(dir.listFiles()!!.any { it.name.contains(CloudVault.hash("snapshot-v2".toByteArray())) })
    }

    @Test fun acknowledgedLookupWithAConcurrentRewriteFailsAndKeepsTheSource() {
        CloudArchive.install(files, noBackup)
        val src = File(files, "research_m0/row.jsonl").apply { parentFile!!.mkdirs(); writeText("snapshot-v1") }
        val early = CloudVault.hash(src.readBytes())
        CloudArchive.recordAcknowledged(early)
        CloudArchive.testAfterEarlyHash = { it.writeText("snapshot-v2") }
        assertFalse(CloudArchive.preserveBeforeDiscard(src))
        assertEquals("snapshot-v2", src.readText())
        val dir = File(files, CloudArchive.DIRECTORY)
        assertEquals(0, dir.listFiles()?.size ?: 0)
    }

    @Test fun successfulPreserveBodyDigestAndLengthMatchIdentityAndMetadata() {
        CloudArchive.install(files, noBackup)
        val bytes = "exact-snapshot-bytes".toByteArray()
        val src = File(files, "research_m0/row.jsonl").apply { parentFile!!.mkdirs(); writeBytes(bytes) }
        assertTrue(CloudArchive.preserveBeforeDiscard(src))
        assertTrue(src.exists())
        val sha = CloudVault.hash(bytes)
        val dir = File(files, CloudArchive.DIRECTORY)
        val payload = archiveBin(dir, sha)
        val meta = org.json.JSONObject(archiveMeta(dir, sha).readText())
        assertArrayEquals(bytes, payload.readBytes())
        assertEquals(sha, CloudVault.hash(payload.readBytes()))
        assertEquals(bytes.size.toLong(), payload.length())
        assertEquals(sha, meta.getString("sha256"))
        assertEquals(bytes.size.toLong(), meta.getLong("bytes"))
        assertEquals("research_m0/row.jsonl", meta.getString("source_path"))
        assertTrue(payload.name.endsWith("_$sha.bin"))
    }
}
