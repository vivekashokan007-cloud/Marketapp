package com.marketradar.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.nio.file.Files
import java.io.ByteArrayOutputStream

class CloudVaultTest {
    @Test fun androidRootAliasesAreAllowedButChildSymlinksAreRejected() {
        val parent = Files.createTempDirectory("android-root-alias").toFile()
        try {
            val real = java.io.File(parent,"real").apply { mkdirs() }
            java.io.File(real,"state.json").writeText("state")
            val alias = Files.createSymbolicLink(java.io.File(parent,"alias").toPath(),real.toPath()).toFile()
            assertTrue(CloudDataPolicy.ownedFile(alias,java.io.File(alias,"state.json")))
            val other = java.io.File(parent,"outside").apply { writeText("must not upload") }
            val child = Files.createSymbolicLink(java.io.File(real,"child.json").toPath(),other.toPath()).toFile()
            assertFalse(CloudDataPolicy.ownedFile(real,child))
        } finally { parent.deleteRecursively() }
    }
    @Test fun networkDelayAndWallClockChangesCannotExtendADeviceLease() {
        val reply = JSONObject().put("server_now","2026-11-26T03:00:00Z").put("lease_until","2026-11-26T03:02:00Z")
        assertEquals(116_000L,CloudLeaseClock.deadline(reply,1000,2000))
        // Even a response delayed beyond its server lease gives no usable time.
        assertEquals(130_000L,CloudLeaseClock.deadline(reply,0,130_000))
    }
    class Memory: CloudVault.Remote {
        val objects = mutableMapOf<String, ByteArray>()
        var refuse = false
        override fun put(hash: String, bytes: ByteArray) { check(!refuse) { "OFFLINE" }; objects.putIfAbsent(hash, bytes.copyOf()) }
        override fun get(hash: String) = objects[hash]?.copyOf() ?: error("MISSING_OBJECT")
    }
    @Test fun freshDeviceRecoversPositionsHistorySettingsAndLargeResearchExactly() {
        val remote = Memory(); val vault = CloudVault(remote)
        val history = JSONArray().also { rows -> repeat(351) { rows.put(JSONObject().put("id", it).put("status", "CLOSED")) } }.toString().toByteArray()
        val research = ByteArray(CloudVault.CHUNK * 2 + 77) { (it * 71).toByte() }
        val originals = mapOf("files/single_leg_v1/positions.json" to "{\"OPEN\":1}".toByteArray(),
            "prefs/market_radar.json" to history, "files/research_m0/session.jsonl" to research,
            "prefs/cloud_web_cache_v1.json" to "{\"storage\":{\"theme\":\"dark\"}}".toByteArray())
        val entries = JSONObject()
        originals.forEach { (path, bytes) -> entries.put(path, vault.store(bytes.inputStream())) }
        val root = vault.publishManifest(entries, 543)
        val downloaded = CloudVault(remote).manifest(root, 543)
        val parent = Files.createTempDirectory("fresh-phone").toFile(); val stage = java.io.File(parent, "stage")
        try {
            CloudVault(remote).stage(downloaded, stage)
            originals.forEach { (path, bytes) -> assertArrayEquals(bytes, java.io.File(stage, path).readBytes()) }
            assertEquals(351, JSONArray(java.io.File(stage, "prefs/market_radar.json").readText()).length())
        } finally { parent.deleteRecursively() }
    }
    @Test fun interruptedUploadCannotProduceManifestAndRetryUsesImmutableBytes() {
        val remote = Memory(); val vault = CloudVault(remote)
        val first = vault.store("retained first checkpoint".byteInputStream())
        val root = vault.publishManifest(JSONObject().put("files/position.json", first), 543)
        remote.refuse = true
        assertThrows(IllegalStateException::class.java) { vault.store("new work".byteInputStream()) }
        val out = ByteArrayOutputStream()
        vault.restore(vault.manifest(root, 543).getJSONObject("entries").getJSONObject("files/position.json"), out)
        assertEquals("retained first checkpoint", out.toString("UTF-8"))
        remote.refuse = false
        val count = remote.objects.size
        vault.store("retained first checkpoint".byteInputStream())
        assertEquals(count, remote.objects.size)
    }
    @Test fun missingOrCorruptChunksNeverActivateARecovery() {
        val remote = Memory(); val vault = CloudVault(remote)
        val entry = vault.store(ByteArray(CloudVault.CHUNK + 15) { it.toByte() }.inputStream())
        val hash = entry.getJSONArray("chunks").getJSONObject(1).getString("hash")
        val original = remote.objects.remove(hash)!!
        assertThrows(IllegalStateException::class.java) { vault.restore(entry, ByteArrayOutputStream()) }
        remote.objects[hash] = original.copyOf().also { it[0] = 0 }
        assertThrows(IllegalArgumentException::class.java) { vault.restore(entry, ByteArrayOutputStream()) }
    }
    @Test fun downgradeAndTraversalAndExpansionBombsFailClosed() {
        val remote = Memory(); val vault = CloudVault(remote)
        val root = vault.publishManifest(JSONObject(), 544)
        assertThrows(IllegalArgumentException::class.java) { vault.manifest(root, 543) }
        val unsafe = vault.publishManifest(JSONObject().put("files/../outside", JSONObject()), 543)
        assertThrows(IllegalArgumentException::class.java) { vault.manifest(unsafe, 543) }
        assertFalse(CloudVault.validPath("/absolute")); assertFalse(CloudVault.validPath("a\\b"))
        val entry = vault.store("123456".byteInputStream()); entry.getJSONArray("chunks").getJSONObject(0).put("size", 1)
        assertThrows(IllegalArgumentException::class.java) { vault.restore(entry, ByteArrayOutputStream()) }
    }
    @Test fun secretsAndDeviceLeasesNeverBecomePortableState() {
        val original = JSONObject().put("mr2_token", "broker secret").put("sb-auth-token", "auth secret")
            .put("mr2_context", "{\"auth_token\":\"nested secret\",\"spot\":63300}").put("mr2_theme", "dark")
        val clean = CloudDataPolicy.cleanStorage(original)
        assertFalse(clean.has("mr2_token")); assertFalse(clean.has("sb-auth-token"))
        assertFalse(clean.toString().contains("secret")); assertTrue(clean.toString().contains("63300"))
        assertFalse(CloudDataPolicy.portablePreference("lease_owner_pid"))
        assertFalse(CloudDataPolicy.portablePreference("g1_supabase_user_access_token"))
        assertFalse(CloudDataPolicy.portablePath("forensics/logbuffer.jsonl"))
        assertTrue(CloudDataPolicy.portablePath("single_leg_v1/positions.json"))
        assertTrue(CloudDataPolicy.portablePath("research_m0/session_20261126.jsonl"))
        assertTrue("History queues are recovery data, never credentials", CloudDataPolicy.portablePath("cloud_history_outbox_v1/payload.bin"))
        assertFalse(CloudDataPolicy.portablePath("cloud_session_v1/credentials.json"))
    }
    @Test fun trimKeepsAnUploadQueueUntilTheExactArchiveBytesAreInAVerifiedCheckpoint() {
        val root = Files.createTempDirectory("before-trim").toFile()
        try {
            CloudArchive.install(root)
            val file = java.io.File(root, "evaluation_local_cache/session.jsonl")
            file.parentFile.mkdirs(); file.writeText("old observations\n")
            assertTrue(CloudArchive.preserveBeforeDiscard(file))
            file.writeText("trimmed cache\n")
            val queue = java.io.File(root, CloudArchive.DIRECTORY)
            val archive = queue.listFiles()!!.first { it.name.endsWith(".bin") }
            assertEquals("old observations\n",archive.readText())
            CloudArchive.pruneAcknowledged(root, JSONObject())
            assertTrue(archive.exists())
            val entry = JSONObject().put("sha256",CloudVault.hash(archive.readBytes()))
            CloudArchive.pruneAcknowledged(root,JSONObject().put("files/${CloudArchive.DIRECTORY}/${archive.name}",entry))
            assertFalse(archive.exists())
            assertEquals("trimmed cache\n",file.readText())
        } finally { CloudArchive.install(null);root.deleteRecursively() }
    }
    @Test fun interruptedActivationRemainsGatedAndReappliesWithoutAnEntryOrExitSideEffect() {
        val root = Files.createTempDirectory("restore-transaction").toFile()
        try {
            val files = java.io.File(root,"files").apply { mkdirs() }
            val privateRoot = java.io.File(root,"private").apply { mkdirs() }
            val obsolete = java.io.File(files,"old-cache.json").apply { writeText("previous cache") }
            val remote = Memory(); val vault = CloudVault(remote)
            val entries = JSONObject().put("files/single_leg_v1/positions.json",vault.store("OPEN exact-contract position".byteInputStream()))
                .put("prefs/market_radar.json",vault.store("{\"capital\":1000000}".byteInputStream()))
            val manifest = vault.manifest(vault.publishManifest(entries,543),543)
            val stage = java.io.File(root,"stage");vault.stage(manifest,stage)
            val prefs = mutableMapOf<String,String>()
            val tx = CloudRestoreTransaction(files,privateRoot)
            assertThrows(IllegalStateException::class.java) {
                tx.activate(stage,entries,listOf(obsolete),{name,file->prefs[name]=file.readText()},
                    installed={error("SIMULATED_PROCESS_DEATH")})
            }
            assertTrue(java.io.File(privateRoot,"cloud_restore_pending").exists())
            tx.activate(stage,entries,listOf(obsolete),{name,file->prefs[name]=file.readText()})
            assertFalse(java.io.File(privateRoot,"cloud_restore_pending").exists())
            assertEquals("OPEN exact-contract position",java.io.File(files,"single_leg_v1/positions.json").readText())
            assertEquals("{\"capital\":1000000}",prefs["market_radar"])
            assertFalse(obsolete.exists())
            assertEquals("previous cache",java.io.File(privateRoot,"cloud_restore_replaced/old-cache.json").readText())
        } finally { root.deleteRecursively() }
    }
}
