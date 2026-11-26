package com.marketradar.app

import android.util.JsonReader
import android.util.JsonToken
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.DigestInputStream
import java.security.MessageDigest
import java.time.Instant
import java.time.ZoneId
import java.util.concurrent.TimeUnit
import java.util.zip.GZIPInputStream

/** Fetch reference sources once per IST date; failures remain retryable at the next poll. */
object BnfReferenceCapture {
    private val client = OkHttpClient.Builder().connectTimeout(15, TimeUnit.SECONDS).readTimeout(45, TimeUnit.SECONDS).build()
    private val ist = ZoneId.of("Asia/Kolkata")
    const val MASTER = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
    const val MEMBERS = "https://www.niftyindices.com/IndexConstituent/ind_niftybanklist.csv"
    const val MONTHLY = "https://www.niftyindices.com/reports/monthly-reports"
    const val FACTSHEET = "https://www.niftyindices.com/Factsheet/ind_nifty_bank.pdf"

    @Synchronized fun daily(root: File, day: String): JSONObject {
        val dir = File(root, "bnf_reference_v1").apply { mkdirs() }
        val cache = File(dir, "$day.json")
        if (cache.exists()) return JSONObject(cache.readText())
        val began = System.currentTimeMillis()
        val membershipBytes = get(MEMBERS, 1024 * 1024)
        val memberRows = parseMembers(String(membershipBytes, Charsets.UTF_8))
        require(memberRows.length() >= 10) { "MEMBERSHIP_INCOMPLETE" }
        archive(root, began, MEMBERS, membershipBytes)
        val isins = (0 until memberRows.length()).map { memberRows.getJSONObject(it).getString("isin") }.toSet()
        val selected = JSONArray()
        val digest = MessageDigest.getInstance("SHA-256")
        val request = Request.Builder().url(MASTER).build()
        client.newCall(request).execute().use { response ->
            require(response.isSuccessful)
            val raw = GZIPInputStream(response.body!!.byteStream())
            JsonReader(DigestInputStream(raw, digest).reader(Charsets.UTF_8)).use { reader ->
                reader.beginArray()
                while (reader.hasNext()) {
                    val row = readValue(reader) as? JSONObject ?: continue
                    val symbol = row.optString("trading_symbol")
                    val bnf = row.optString("underlying_symbol") == "BANKNIFTY" ||
                        row.optString("underlying_key") == "NSE_INDEX|Nifty Bank" ||
                        symbol.startsWith("BANKNIFTY ") || symbol.startsWith("BANKNIFTY2")
                    if (bnf || row.optString("isin") in isins ||
                        row.optString("instrument_key") in setOf("NSE_INDEX|Nifty Bank", "NSE_INDEX|Nifty 50", "NSE_INDEX|India VIX")) selected.put(row)
                }
                reader.endArray()
                require(reader.peek() == JsonToken.END_DOCUMENT)
            }
        }
        val futures = JSONArray(); val options = JSONArray()
        for (i in 0 until selected.length()) {
            val r = selected.getJSONObject(i)
            val type = r.optString("instrument_type")
            if (r.optString("segment") == "NSE_FO") {
                val expiry = expiryDate(r.opt("expiry"))
                if (expiry != null && expiry >= day) {
                    if (type == "FUT") futures.put(r)
                    if (type in setOf("CE", "PE")) options.put(r)
                }
            }
        }
        require(futures.length() > 0 && options.length() > 0) { "MASTER_BNF_CONTRACTS_MISSING" }
        val result = JSONObject().put("day", day).put("receipt_ms", System.currentTimeMillis())
            .put("source_url", MASTER).put("master_decoded_sha256", digest.digest().joinToString("") { "%02x".format(it) })
            .put("members", memberRows).put("instruments", selected).put("futures", futures).put("options", options)
        require(BnfRawCapture.record(root, "marketradar://capture/instrument-master", began,
            System.currentTimeMillis(), 200, result.toString().toByteArray()))
        val tmp = File.createTempFile("master_", ".tmp", dir)
        try { tmp.writeText(result.toString()); require(tmp.renameTo(cache)) } finally { tmp.delete() }
        return result
    }

    @Synchronized fun monthly(root: File, day: String) {
        val dir = File(root, "bnf_reference_v1").apply { mkdirs() }
        // Daily publication check catches late monthly releases. Cache only a successful check.
        val marker = File(dir, "${day}_weights_checked")
        if (marker.exists()) return
        val began = System.currentTimeMillis()
        val html = get(MONTHLY, 2 * 1024 * 1024)
        archive(root, began, MONTHLY, html)
        val paths = Regex("href=[\"']([^\"']*(?:Market_Capitalisation_and_Weightage|indices_data)[^\"']*\\.zip)[\"']", RegexOption.IGNORE_CASE)
            .findAll(String(html, Charsets.UTF_8)).map { java.net.URI(MONTHLY).resolve(it.groupValues[1]).toString() }.distinct().toList()
        require(paths.isNotEmpty()) { "MONTHLY_WEIGHT_REPORT_LINK_MISSING" }
        var validatedPublications = 0
        for (url in paths) {
            val uri = java.net.URI(url)
            require(uri.host == "www.niftyindices.com" && uri.scheme == "https")
            val zip = get(url, 4 * 1024 * 1024)
            archive(root, began, url, zip)
            java.util.zip.ZipInputStream(zip.inputStream()).use { stream ->
                while (true) {
                    val entry = stream.nextEntry ?: break
                    if (entry.name.matches(Regex("(?i)NIFTY[_ ]Bank[_ ].*\\.pdf"))) {
                        val pdf = java.io.ByteArrayOutputStream().also { out ->
                            val buf = ByteArray(8192)
                            while (true) { val n = stream.read(buf); if (n < 0) break; require(out.size()+n <= 2*1024*1024); out.write(buf,0,n) }
                        }.toByteArray()
                        val text = com.tom_roush.pdfbox.pdmodel.PDDocument.load(pdf).use { document ->
                            com.tom_roush.pdfbox.text.PDFTextStripper().apply { sortByPosition = true }.getText(document)
                        }
                        val membershipFile = File(dir, "$day.json")
                        require(membershipFile.exists())
                        val members = JSONObject(membershipFile.readText()).getJSONArray("members")
                        val weights = BnfWeightPublication.parse(text, members, day)
                            .put("source_url", url).put("zip_entry", entry.name)
                            .put("source_pdf_sha256", CloudVault.hash(pdf)).put("receipt_ms", System.currentTimeMillis())
                        require(BnfRawCapture.record(root, "marketradar://capture/reference", began,
                            System.currentTimeMillis(), 200, weights.toString().toByteArray()))
                        validatedPublications++
                        val latest = File(dir, "weights_latest.json")
                        val tmp = File.createTempFile("weights_", ".tmp", dir)
                        try { tmp.writeText(weights.toString()); require(tmp.renameTo(latest)) } finally { tmp.delete() }
                    }
                }
            }
        }
        require(validatedPublications > 0) { "NO_COMPLETE_BNF_WEIGHT_PUBLICATION" }
        archive(root, began, FACTSHEET, get(FACTSHEET, 2 * 1024 * 1024))
        require(marker.createNewFile())
    }

    private fun archive(root: File, began: Long, url: String, bytes: ByteArray) {
        val envelope = JSONObject().put("source_url", url).put("receipt_ms", System.currentTimeMillis())
            .put("source_sha256", CloudVault.hash(bytes)).put("source_bytes", bytes.size)
            .put("content_base64", java.util.Base64.getEncoder().encodeToString(bytes))
        require(BnfRawCapture.record(root, "marketradar://capture/reference", began,
            System.currentTimeMillis(), 200, envelope.toString().toByteArray()))
    }
    private fun get(url: String, limit: Int): ByteArray = client.newCall(Request.Builder().url(url)
        .header("User-Agent", "MarketRadar/1.0").build()).execute().use { r ->
        require(r.isSuccessful) { "REFERENCE_HTTP_${r.code}" }
        r.body!!.byteStream().use { input ->
            java.io.ByteArrayOutputStream().also { out ->
                val buf = ByteArray(8192)
                while (true) { val n = input.read(buf); if (n < 0) break; require(out.size() + n <= limit); out.write(buf, 0, n) }
            }.toByteArray()
        }
    }
    internal fun expiryDate(value: Any?): String? = try {
        when (value) {
            is Number -> Instant.ofEpochMilli(value.toLong()).atZone(ist).toLocalDate().toString()
            is String -> if (value.matches(Regex("\\d{4}-\\d{2}-\\d{2}"))) value else Instant.ofEpochMilli(value.toLong()).atZone(ist).toLocalDate().toString()
            else -> null
        }
    } catch (_: Exception) { null }
    internal fun parseMembers(csv: String): JSONArray {
        val lines = csv.trim().lines().filter { it.isNotBlank() }
        fun fields(line: String): List<String> {
            val result = mutableListOf<String>(); val field = StringBuilder(); var quote = false; var i = 0
            while (i < line.length) { val c = line[i]
                if (c == '"') { if (quote && i+1 < line.length && line[i+1] == '"') { field.append(c); i++ } else quote = !quote }
                else if (c == ',' && !quote) { result.add(field.toString().trim()); field.clear() } else field.append(c)
                i++
            }; result.add(field.toString().trim()); return result
        }
        val header = fields(lines.first()).map { it.removePrefix("\uFEFF").lowercase() }
        val isinIndex = header.indexOfFirst { it.contains("isin") }; val symbolIndex = header.indexOf("symbol")
        require(isinIndex >= 0 && symbolIndex >= 0)
        val rows = JSONArray(); val seen = mutableSetOf<String>()
        for (line in lines.drop(1)) { val f = fields(line)
            require(f.size > maxOf(isinIndex, symbolIndex)); val isin = f[isinIndex]
            require(isin.matches(Regex("IN[A-Z0-9]{10}")) && seen.add(isin))
            rows.put(JSONObject().put("symbol", f[symbolIndex]).put("isin", isin).put("instrument_key", "NSE_EQ|$isin"))
        }; return rows
    }
    private fun readValue(r: JsonReader): Any = when (r.peek()) {
        JsonToken.BEGIN_OBJECT -> JSONObject().also { out -> r.beginObject(); while (r.hasNext()) { val key = r.nextName(); out.put(key, readValue(r)) }; r.endObject() }
        JsonToken.BEGIN_ARRAY -> JSONArray().also { out -> r.beginArray(); while (r.hasNext()) out.put(readValue(r)); r.endArray() }
        JsonToken.STRING -> r.nextString()
        JsonToken.NUMBER -> r.nextString().let { it.toLongOrNull() ?: it.toDouble() }
        JsonToken.BOOLEAN -> r.nextBoolean()
        JsonToken.NULL -> { r.nextNull(); JSONObject.NULL }
        else -> error("MASTER_JSON_TOKEN")
    }
}
