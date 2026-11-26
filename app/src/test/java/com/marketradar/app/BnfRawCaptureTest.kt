package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.nio.file.Files
import java.util.zip.GZIPInputStream

class BnfRawCaptureTest {
    @Test fun preservesOriginalBytesIncludingUnknownFutureFieldsAndNulls() {
        val root = Files.createTempDirectory("bnf-capture").toFile()
        try {
            val url = "https://api.upstox.com/v2/option/chain?instrument_key=NSE_INDEX%7CNifty+Bank&expiry_date=2026-12-01"
            val raw = """{"data":[{"call_options":{"market_data":{"bid_qty":10,"ask_qty":null,"future_field":{"x":1}}}}],"vendor_new":true} """.toByteArray()
            assertTrue(BnfRawCapture.eligible(url))
            assertTrue(BnfRawCapture.record(root, url, 100, 200, 200, raw))
            val files = root.resolve(BnfRawCapture.DIRECTORY).listFiles()!!.toList()
            val meta = JSONObject(files.single { it.extension == "json" }.readText())
            val restored = GZIPInputStream(files.single { it.name.endsWith(".body.gz") }.inputStream()).use { it.readBytes() }
            assertArrayEquals(raw, restored)
            assertEquals(CloudVault.hash(raw), meta.getString("body_sha256"))
            assertEquals(200, meta.getInt("http_status"))
            assertEquals(raw.size, meta.getInt("body_bytes"))
            assertFalse(meta.toString().contains("Bearer"))
        } finally { root.deleteRecursively() }
    }

    @Test fun recordsHttpFailureWithoutManufacturingABody() {
        val root = Files.createTempDirectory("bnf-capture-failure").toFile()
        try {
            val url = "https://api.upstox.com/v2/option/contract?instrument_key=NSE_INDEX%7CNifty+Bank"
            assertTrue(BnfRawCapture.record(root, url, 100, 200, 429, null, "HTTP_429"))
            val meta = JSONObject(root.resolve(BnfRawCapture.DIRECTORY).listFiles()!!.single().readText())
            assertEquals(429, meta.getInt("http_status"))
            assertTrue(meta.isNull("body_sha256"))
        } finally { root.deleteRecursively() }
    }

    @Test fun dedicatedNullResultAndGreekResponseAreCapturedLosslessly() {
        val root = Files.createTempDirectory("bnf-capture-analytics").toFile()
        try {
            val nullUrl = "https://api.upstox.com/v2/market/change-oi?instrument_key=NSE_INDEX%7CNifty+Bank&expiry=2027-02-23&date=2026-11-26&interval=2"
            val greekUrl = "https://api.upstox.com/v3/market-quote/option-greek?instrument_key=NSE_FO%7C59945"
            val nullBody = """{"status":"success","data":null}""".toByteArray()
            assertTrue(BnfRawCapture.record(root, nullUrl, 100, 200, 200, nullBody))
            assertTrue(BnfRawCapture.record(root, greekUrl, 201, 300, 200, "{}".toByteArray()))
            val stored = root.resolve(BnfRawCapture.DIRECTORY).listFiles()!!.filter { it.name.endsWith(".body.gz") }
                .map { GZIPInputStream(it.inputStream()).use { gz -> gz.readBytes() } }
            assertTrue(stored.any { it.contentEquals(nullBody) })
            assertEquals(2, stored.size)
        } finally { root.deleteRecursively() }
    }
}
