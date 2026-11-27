package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.io.File
import java.nio.file.Files

/** 27 Nov handoff 'Testing and delivery boundaries' item 2 probes that are feasible on the JVM.
 * Source-contract probes pin orderings that need an Android runtime to exercise; on-device behaviour stays UNPROVEN.
 */
class LiveSessionRegression20261127Test {
    private fun src(name: String) = listOf(File("src/main/java/com/marketradar/app/$name"),
        File("app/src/main/java/com/marketradar/app/$name")).first { it.exists() }.readText()
    private fun onStart(): String = src("MarketWatchService.kt")
        .substringAfter("override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {")
        .substringBefore("private fun restoreNotificationAgentState()")

    @Test fun foregroundPromotionPrecedesEveryStopCloudLeaseBackoffAndNullIntentExit() {
        val start = onStart()
        val promote = start.indexOf("startLiveForeground(NOTIFICATION_ID")
        assertTrue(promote >= 0)
        val promotionCatch = start.substringBefore("if (!CloudRecovery.canMonitor()")
        // Every return after the promotion try/catch comes after startLiveForeground.
        val tail = start.substring(promotionCatch.length)
        assertTrue(Regex("return START_(NOT_)?STICKY").findAll(tail).count() >= 5)
        for (gate in listOf("!CloudRecovery.canMonitor()", "intent?.action == \"STOP\"", "foregroundStartBackoffRemainingMs() > 0L", "if (!lease.claimed)"))
            assertTrue(gate, start.indexOf(gate) > promote)
        // Null intents (START_STICKY redelivery) never dereference the intent.
        assertFalse(start.contains("intent!!"))
        assertFalse(Regex("intent\\.action").containsMatchIn(start))
        // Only the failed-promotion catch may return before promotion, and it stops without self-heal.
        val beforeCanMonitor = promotionCatch.substringAfter("} catch (t: Throwable) {")
        assertTrue(beforeCanMonitor.indexOf("selfHealAllowed = false") in 0 until beforeCanMonitor.indexOf("return START_NOT_STICKY"))
    }

    @Test fun intentionalStopAndRejectedStartsNeverScheduleARestartAlarm() {
        val start = onStart()
        val blocks = start.split("return START_NOT_STICKY").dropLast(1)
        assertEquals(5, blocks.size) // promotion failure, cloud not ready, STOP, backoff, lease refused
        blocks.forEach { block ->
            val branch = block.substringAfterLast("{")
            assertTrue(branch, branch.contains("selfHealAllowed = false"))
        }
        val stop = start.substringAfter("if (intent?.action == \"STOP\") {").substringBefore("return START_NOT_STICKY")
        assertTrue(stop.indexOf("selfHealAllowed = false") < stop.indexOf("stopPolling()"))
        assertTrue(stop.contains("cancelPollAlarm()"))
        val service = src("MarketWatchService.kt")
        val heal = service.substringAfter("private fun scheduleSelfHealRestart() {").substringBefore("override fun onDestroy()")
        assertTrue(heal.trimStart().startsWith("if (!selfHealAllowed || !CloudRecovery.canMonitor()) return"))
        assertEquals(1, Regex("scheduleSelfHealRestart\\(\\)").findAll(service).count() - 1) // only onDestroy calls it
    }

    @Test fun sourceUrlParsingKeepsLiteralSeparatorsAndTheOriginalUrl() {
        val pipe = BnfSourceUrl.parse("https://api.upstox.com/v2/market-quote/quotes?instrument_key=NSE_EQ|INE040A01034,NSE_INDEX|Nifty Bank")
        assertEquals("NSE_EQ|INE040A01034,NSE_INDEX|Nifty Bank", pipe.params.getString("instrument_key"))
        assertEquals("/v2/market-quote/quotes", pipe.endpoint)
        val eq = BnfSourceUrl.parse("https://api.upstox.com/v2/option/chain?instrument_key=NSE_INDEX|Nifty Bank&expiry_date=2026-12-29&note=a=b")
        assertEquals("a=b", eq.params.getString("note"))
        val empty = BnfSourceUrl.parse("https://api.upstox.com/v2/option/contract?instrument_key=NSE_INDEX|Nifty Bank&expiry_date=")
        assertEquals("", empty.params.getString("expiry_date"))
        // Behaviour probe only (not a diagnosis of the single 27 Nov IllegalArgumentException ID):
        // a malformed percent escape still throws, and the uploader keeps the file pending.
        assertTrue(BnfRawCapture.eligible("https://api.upstox.com/v2/option/chain?instrument_key=NSE_INDEX|Nifty Bank&expiry_date=2026-12-29&x=%zz"))
        val decoderFailure = assertThrows(IllegalArgumentException::class.java) { BnfSourceUrl.parse("https://api.upstox.com/v2/option/chain?instrument_key=NSE_INDEX|Nifty Bank&expiry_date=2026-12-29&x=%zz") }
        assertTrue(decoderFailure.stackTrace.any { it.className.contains("URLDecoder") })
        val uploader = src("BnfSourceUploader.kt")
        assertTrue(uploader.contains("val notes = JSONObject().put(\"url\", url)")) // raw URL stored unchanged
        assertTrue(uploader.contains("val source = BnfSourceUrl.parse(url)"))
        assertFalse(uploader.contains("URI(url)"))
    }

    @Test fun sourceRowsAreIdempotentAndAcknowledgedOnlyAfterExactReadback() {
        val client = src("SupabaseClient.kt")
        val save = client.substringAfter("fun saveMarketSourceCapture(").substringBefore("fun saveChainRows(")
        assertTrue(save.contains("mr_market_source_captures?on_conflict=capture_id"))
        assertTrue(save.contains("resolution=ignore-duplicates"))
        assertTrue(save.contains("select=capture_id,original_sha256,stored_bytes&capture_id=eq.\$id"))
        assertTrue(save.contains("rows.length() == 1"))
        assertTrue(save.contains("it.optString(\"original_sha256\") == row.optString(\"original_sha256\")"))
        assertTrue(save.contains("it.optInt(\"stored_bytes\", -1) == row.optInt(\"stored_bytes\", -1)"))
        val drain = src("BnfSourceUploader.kt").substringAfter("internal fun drain(").substringBefore("private fun uploadAndVerify(")
        assertTrue(drain.indexOf("uploadAndVerify(session, metadata, id)") < drain.indexOf("require(temp.renameTo(File(acknowledgements, id)))"))
        assertFalse(drain.contains(".delete()") && drain.replace("temp.delete()", "").contains(".delete()")) // outbox never deleted here
        assertTrue(src("SupabaseClient.kt").contains("require(!AuthAccess.isDisallowedClientSecret(userToken))"))
    }

    @Test fun pruneDeletesOnlyAcknowledgedArchiveCopiesNeverPaperStateOrPendingCaptures() {
        val root = Files.createTempDirectory("prune").toFile()
        try {
            CloudArchive.install(root)
            val paper = File(root, "single_leg_v1/positions.json").apply { parentFile.mkdirs(); writeText("{\"open\":1}") }
            val raw = File(root, "${BnfRawCapture.DIRECTORY}/1_00000001-aaaa-bbbb-cccc-dddddddddddd.json").apply { parentFile.mkdirs(); writeText("{}") }
            val queue = File(root, CloudArchive.DIRECTORY).apply { mkdirs() }
            val acked = File(queue, "a_1.bin").apply { writeText("acked") }
            val wrongHash = File(queue, "a_2.bin").apply { writeText("changed") }
            val unacked = File(queue, "a_3.bin").apply { writeText("pending") }
            val partial = File(queue, "archive_9.tmp").apply { writeText("partial") }
            val entries = JSONObject()
                .put("files/${CloudArchive.DIRECTORY}/a_1.bin", JSONObject().put("sha256", CloudVault.hash("acked".toByteArray())))
                .put("files/${CloudArchive.DIRECTORY}/a_2.bin", JSONObject().put("sha256", CloudVault.hash("original".toByteArray())))
                .put("files/single_leg_v1/positions.json", JSONObject().put("sha256", CloudVault.hash(paper.readBytes())))
                .put("files/${raw.relativeTo(root).invariantSeparatorsPath}", JSONObject().put("sha256", CloudVault.hash(raw.readBytes())))
            CloudArchive.pruneAcknowledged(root, entries)
            assertFalse(acked.exists())
            assertTrue(wrongHash.exists()); assertTrue(unacked.exists()); assertTrue(partial.exists())
            assertTrue(paper.exists()); assertTrue(raw.exists()) // acknowledged entries outside the archive are never pruned
            assertFalse(CloudDataPolicy.portablePath("${BnfRawCapture.DIRECTORY}/x.json")) // raw outbox is not checkpoint data
            assertTrue(CloudDataPolicy.portablePath("single_leg_v1/positions.json"))
            // Discard of a working file is refused unless its exact bytes were archived first.
            assertTrue(CloudArchive.preserveBeforeDiscard(paper))
            assertTrue(queue.listFiles()!!.any { it.name.endsWith(".bin") && it.readText() == "{\"open\":1}" })
        } finally { CloudArchive.install(null); root.deleteRecursively() }
    }
}
