package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.nio.file.Files
import java.io.File
import java.util.concurrent.Executors
import java.util.concurrent.Callable
import java.util.concurrent.atomic.AtomicInteger

class VerifiedRecoveryUploadCacheTest {
    private fun receipt(hash: String) = JSONObject().put("sha256", hash).put("size", 1).put("chunks", org.json.JSONArray())
    @Test fun restartReusesVerifiedReceiptButChangedContentRequiresNewUpload() {
        val root = Files.createTempDirectory("verified-receipts").toFile()
        try {
            val a = CloudVault.hash("old".toByteArray()); val b = CloudVault.hash("new".toByteArray())
            val calls = AtomicInteger()
            VerifiedRecoveryUploadCache(root).entry(a) { calls.incrementAndGet(); receipt(a) }
            VerifiedRecoveryUploadCache(root).entry(a) { error("ALREADY_VERIFIED") }
            VerifiedRecoveryUploadCache(root).entry(b) { calls.incrementAndGet(); receipt(b) }
            assertEquals(2, calls.get())
        } finally { root.deleteRecursively() }
    }
    @Test fun failedOrMismatchedUploadNeverCreatesAReusableReceipt() {
        val root = Files.createTempDirectory("failed-receipts").toFile()
        try {
            val hash = CloudVault.hash("original".toByteArray()); val cache = VerifiedRecoveryUploadCache(root)
            assertThrows(IllegalStateException::class.java) { cache.entry(hash) { error("READBACK_FAILED") } }
            assertThrows(IllegalArgumentException::class.java) { cache.entry(hash) { receipt(CloudVault.hash("changed".toByteArray())) } }
            assertFalse(File(root, "$hash.json").exists())
        } finally { root.deleteRecursively() }
    }
    @Test fun concurrentDuplicateContentsOnlyUploadOnceAndCorruptedReceiptRetries() {
        val root = Files.createTempDirectory("concurrent-receipts").toFile(); val pool = Executors.newFixedThreadPool(6)
        try {
            val hash = CloudVault.hash("same".toByteArray()); val cache = VerifiedRecoveryUploadCache(root); val count = AtomicInteger()
            val jobs = (1..30).map { Callable { cache.entry(hash) { count.incrementAndGet(); receipt(hash) } } }
            pool.invokeAll(jobs).forEach { assertEquals(hash, it.get().getString("sha256")) }
            assertEquals(1, count.get())
            File(root, "$hash.json").writeText("corrupted")
            VerifiedRecoveryUploadCache(root).entry(hash) { count.incrementAndGet(); receipt(hash) }
            assertEquals(2, count.get())
            val otherOwner = VerifiedRecoveryUploadCache(File(root, "other-owner"))
            otherOwner.entry(hash) { count.incrementAndGet(); receipt(hash) }
            assertEquals(3, count.get())
        } finally { pool.shutdownNow(); root.deleteRecursively() }
    }
    @Test fun receiptRequiresActualCloudReadbackAndRestoresOriginalBytes() {
        val root = Files.createTempDirectory("real-receipt").toFile()
        val objects = java.util.concurrent.ConcurrentHashMap<String, ByteArray>()
        var corrupt = true
        val writes = AtomicInteger()
        val remote = object: CloudVault.Remote {
            override fun put(hash: String, bytes: ByteArray) { writes.incrementAndGet(); objects.putIfAbsent(hash, bytes.copyOf()) }
            override fun get(hash: String): ByteArray = if (corrupt) byteArrayOf(0) else objects.getValue(hash).copyOf()
        }
        val vault = CloudVault(remote)
        val bytes = ByteArray(CloudVault.CHUNK + 17) { (it * 37).toByte() }
        val hash = CloudVault.hash(bytes)
        try {
            assertThrows(IllegalArgumentException::class.java) {
                VerifiedRecoveryUploadCache(root).entry(hash) { vault.store(bytes.inputStream()) }
            }
            assertFalse(File(root, "$hash.json").exists())
            corrupt = false
            VerifiedRecoveryUploadCache(root).entry(hash) { vault.store(bytes.inputStream()) }
            val writesBeforeRestart = writes.get()
            val restoredEntry = VerifiedRecoveryUploadCache(root).entry(hash) { error("VERIFIED_RECEIPT_NOT_REUSED") }
            val out = java.io.ByteArrayOutputStream()
            vault.restore(restoredEntry, out)
            assertArrayEquals(bytes, out.toByteArray())
            assertEquals(writesBeforeRestart, writes.get())
        } finally { root.deleteRecursively() }
    }

}
