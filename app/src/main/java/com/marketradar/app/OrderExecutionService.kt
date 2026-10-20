package com.marketradar.app

import android.util.Log
import com.marketradar.app.util.LogBuffer
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.net.URLEncoder
import java.util.concurrent.TimeUnit

object OrderExecutionService {
    const val EXECUTION_MODE = "SANDBOX"
    const val SANDBOX_TOKEN = "SANDBOX_TOKEN"

    private const val TAG = "OrderExecutionService"
    private const val SANDBOX_BASE_URL = "https://sandbox.upstox.com"
    private const val JSON_MEDIA_TYPE = "application/json"

    // G3: an order POST is never retried implicitly. OkHttp's default retryOnConnectionFailure(true) can re-send a
    // request after a connection failure; a timed-out/uncertain submission must surface as UNKNOWN, not be repeated.
    private val client = OkHttpClient.Builder()
        .connectTimeout(12, TimeUnit.SECONDS)
        .readTimeout(20, TimeUnit.SECONDS)
        .retryOnConnectionFailure(false)
        .followRedirects(false)
        .followSslRedirects(false)
        .build()

    /** HTTP result seen by [dispatch]; `code == 0` is never produced by a transport (no response is an exception). */
    internal data class TransportResponse(val code: Int, val message: String, val body: String)

    /** Transport seam. Production = OkHttp to the locked sandbox URL; JVM tests install a fake (never a live call). */
    internal fun interface Transport {
        fun execute(method: String, url: String, body: String?, bearerToken: String): TransportResponse
    }

    private val okHttpTransport = Transport { method, url, body, bearerToken ->
        val builder = Request.Builder()
            .url(url)
            .addHeader("Accept", JSON_MEDIA_TYPE)
            .addHeader("Content-Type", JSON_MEDIA_TYPE)
            .addHeader("Authorization", "Bearer $bearerToken")
        val request = when (method) {
            "POST" -> builder.post((body ?: "").toRequestBody(JSON_MEDIA_TYPE.toMediaTypeOrNull())).build()
            "PUT" -> builder.put((body ?: "").toRequestBody(JSON_MEDIA_TYPE.toMediaTypeOrNull())).build()
            "DELETE" -> builder.delete().build()
            else -> throw IllegalArgumentException("Unsupported method $method")
        }
        client.newCall(request).execute().use { response ->
            TransportResponse(response.code, response.message, response.body?.string() ?: "")
        }
    }

    @Volatile internal var transport: Transport = okHttpTransport
    @Volatile internal var persistRow: (JSONObject) -> Boolean = { row -> SupabaseClient.insertSandboxOrder(row) }
    @Volatile internal var clock: () -> Long = { System.currentTimeMillis() }

    internal fun resetSeamsForTest() {
        transport = okHttpTransport
        persistRow = { row -> SupabaseClient.insertSandboxOrder(row) }
        clock = { System.currentTimeMillis() }
    }

    data class BuildResult(
        val ok: Boolean,
        val tradeRef: String,
        val orders: JSONArray,
        val errorCode: String? = null,
        val errorMessage: String? = null,
        /** G1: per-order price provenance, keyed by correlation_id. Never sent to the broker. */
        val pricing: JSONArray = JSONArray()
    )

    /** Never claimed by any response of this service: placement acknowledgement is not a fill. */
    const val FILL_STATUS_UNRECONCILED = "UNKNOWN_NOT_RECONCILED"

    // G1 price provenance. A LIMIT price is either derived from a validated executable quote side, or it is the
    // caller's explicit requested LIMIT. LTP is never an executable price, and an unlabelled price is never guessed.
    const val PRICE_SOURCE_QUOTE = "QUOTE_DERIVED_LIMIT"
    const val PRICE_SOURCE_REQUESTED = "REQUESTED_LIMIT"
    private val REQUESTED_LABELS = setOf("requested_limit", "requested", "explicit_limit", "explicit")
    private val QUOTE_LABELS = setOf("quote", "quote_derived", "quote_derived_limit", "executable_quote")

    internal data class PriceResolution(
        val price: Double,
        val source: String?,
        val errorCode: String?,
        val errorMessage: String?,
        val provenance: JSONObject
    )

    fun buildOrders(input: JSONObject): BuildResult {
        val tradeRef = extractTradeRef(input)
        return try {
            val rawLegs = extractLegs(input)
            if (rawLegs.length() == 0) {
                return BuildResult(false, tradeRef, JSONArray(), "NO_LEGS", "No candidate/trade legs were supplied.")
            }

            val orders = mutableListOf<JSONObject>()
            val pricing = mutableListOf<JSONObject>()
            for (i in 0 until rawLegs.length()) {
                val leg = rawLegs.optJSONObject(i)
                    ?: return BuildResult(false, tradeRef, JSONArray(), "BAD_LEG", "Leg $i is not a JSON object.")
                val order = buildLegOrder(input, leg, tradeRef, i)
                if (!order.optBoolean("ok", false)) {
                    return BuildResult(
                        false,
                        tradeRef,
                        JSONArray(),
                        order.optString("error_code", "LEG_VALIDATION_FAILED"),
                        order.optString("error_message", "Leg validation failed.")
                    )
                }
                orders += order.getJSONObject("order")
                pricing += order.getJSONObject("pricing")
            }
            val correlationIds = orders.map { it.optString("correlation_id") }
            if (correlationIds.toSet().size != correlationIds.size) {
                return BuildResult(false, tradeRef, JSONArray(), "DUPLICATE_CORRELATION_ID", "Every order line needs a unique correlation_id.")
            }

            val sequenced = orders.sortedWith(
                compareBy<JSONObject> { if (it.optString("transaction_type") == "BUY") 0 else 1 }
                    .thenBy { it.optString("correlation_id") }
            )
            BuildResult(true, tradeRef, JSONArray().apply { sequenced.forEach { put(it) } }, pricing = JSONArray().apply { pricing.forEach { put(it) } })
        } catch (e: Exception) {
            BuildResult(false, tradeRef, JSONArray(), "BUILD_EXCEPTION", e.message ?: e.javaClass.simpleName)
        }
    }

    fun runDebugAction(payload: JSONObject, sandboxToken: String): JSONObject {
        val action = payload.optString("action", "build").trim().lowercase()
        val tradeRef = extractTradeRef(payload)
        return when (action) {
            "build" -> {
                val built = buildOrders(payload)
                JSONObject()
                    .put("ok", built.ok)
                    .put("mode", EXECUTION_MODE)
                    .put("trade_ref", built.tradeRef)
                    .put("orders", built.orders)
                    .put("pricing", built.pricing)
                    .put("error_code", built.errorCode ?: JSONObject.NULL)
                    .put("error_message", built.errorMessage ?: JSONObject.NULL)
            }
            "place_sequential" -> {
                val built = buildOrders(payload)
                if (!built.ok) return buildFailure(built)
                placeSequential(built, sandboxToken, tradeRef)
            }
            "place_multi" -> {
                val built = buildOrders(payload)
                if (!built.ok) return buildFailure(built)
                val raw = dispatch("multi", "v2", "POST", "/v2/order/multi/place", built.orders, sandboxToken, tradeRef)
                val placement = parseMultiPlacement(raw.optInt("http_status", 0), raw.opt("response_json"), built.orders)
                if (raw.isNull("error_code") && placement.getString("placement_status") != PLACEMENT_ALL_ACCEPTED) {
                    raw.put("error_code", "PLACEMENT_" + placement.getString("placement_status"))
                        .put("error_message", "Multi-order placement not fully accepted; see placement.lines and placement.anomalies.")
                }
                raw.put("placement", placement)
                    .put("placement_status", placement.getString("placement_status"))
                    // G2: order_ids = broker IDs of accepted lines only; ok = every line accepted (placement only).
                    .put("order_ids", placement.getJSONArray("accepted_order_ids"))
                    // R1: dispatch's verdict (2xx and no error) AND the per-line classification; neither can override the other.
                    .put("ok", raw.optBoolean("ok", false) && placement.getString("placement_status") == PLACEMENT_ALL_ACCEPTED)
                    .put("placement_anomalies", placement.getJSONArray("anomalies"))
                    .put("pricing", built.pricing)
                    .put("fill_status", FILL_STATUS_UNRECONCILED)
                    .put("fill_implied", false)
                    .put("dependent_live_short_entry_authorized", false)
            }
            "modify" -> {
                val requestJson = payload.optJSONObject("request") ?: payload
                dispatch("modify", "v2", "PUT", "/v2/order/modify", requestJson, sandboxToken, tradeRef)
            }
            "cancel" -> {
                val orderId = payload.optString("order_id").trim()
                if (orderId.isBlank()) {
                    JSONObject()
                        .put("ok", false)
                        .put("mode", EXECUTION_MODE)
                        .put("error_code", "ORDER_ID_REQUIRED")
                        .put("error_message", "order_id is required for cancel.")
                } else {
                    val encoded = URLEncoder.encode(orderId, "UTF-8")
                    dispatch("cancel", "v2", "DELETE", "/v2/order/cancel?order_id=$encoded", JSONObject().put("order_id", orderId), sandboxToken, tradeRef)
                }
            }
            else -> JSONObject()
                .put("ok", false)
                .put("mode", EXECUTION_MODE)
                .put("error_code", "UNKNOWN_ACTION")
                .put("error_message", "Unsupported sandbox action: $action")
        }
    }

    private fun buildFailure(built: BuildResult): JSONObject {
        return JSONObject()
            .put("ok", false)
            .put("mode", EXECUTION_MODE)
            .put("trade_ref", built.tradeRef)
            .put("orders", built.orders)
            .put("error_code", built.errorCode ?: "BUILD_FAILED")
            .put("error_message", built.errorMessage ?: "Order build failed.")
    }

    private fun buildLegOrder(root: JSONObject, leg: JSONObject, tradeRef: String, index: Int): JSONObject {
        val token = firstNonBlank(
            leg.optString("instrument_token"),
            leg.optString("instrument_key"),
            leg.optString("token")
        )
        if (token.isBlank()) return legError("INSTRUMENT_TOKEN_REQUIRED", "Leg $index has no instrument token/key.")

        val transactionType = normalizeTransactionType(
            firstNonBlank(
                leg.optString("transaction_type"),
                leg.optString("transactionType"),
                leg.optString("action"),
                leg.optString("side")
            )
        )
        if (transactionType == null) return legError("TRANSACTION_TYPE_REQUIRED", "Leg $index has no BUY/SELL transaction type.")

        val lotSize = resolveLotSize(root, leg, token)
        if (lotSize <= 0) return legError("LOT_SIZE_REQUIRED", "Leg $index has no validated lot size from payload/instrument metadata.")

        val lots = firstPositiveInt(leg, root, listOf("lots", "lot_count", "lotCount"))
        val explicitQty = leg.optInt("quantity", 0)
        val quantity = when {
            lots > 0 -> lots * lotSize
            explicitQty > 0 && explicitQty % lotSize == 0 -> explicitQty
            else -> 0
        }
        if (quantity <= 0) return legError("QUANTITY_INVALID", "Leg $index quantity must be positive and a lot-size multiple.")
        if (quantity % lotSize != 0) return legError("QUANTITY_LOT_MISMATCH", "Leg $index quantity is not a multiple of lot size $lotSize.")

        val resolved = resolveLimitPrice(root, leg, token, transactionType, index)
        if (resolved.errorCode != null) return legError(resolved.errorCode, resolved.errorMessage ?: resolved.errorCode)
        val price = resolved.price

        val correlationId = firstNonBlank(leg.optString("correlation_id"), "${index + 1}")
        if (correlationId.length > 20) return legError("CORRELATION_ID_INVALID", "Leg $index correlation_id exceeds 20 characters.")
        val tag = firstNonBlank(root.optString("tag"), tradeRef).take(20)
        val order = JSONObject()
            .put("correlation_id", correlationId)
            .put("quantity", quantity)
            .put("product", "D")
            .put("validity", "DAY")
            .put("price", roundPrice(price))
            .put("tag", tag)
            .put("instrument_token", token)
            .put("order_type", "LIMIT")
            .put("transaction_type", transactionType)
            .put("disclosed_quantity", 0)
            .put("trigger_price", 0)
            .put("is_amo", root.optBoolean("is_amo", leg.optBoolean("is_amo", false)))
            .put("slice", root.optBoolean("slice", leg.optBoolean("slice", false)))
        val pricing = JSONObject(resolved.provenance.toString())
            .put("correlation_id", correlationId)
            .put("instrument_token", token)
            .put("transaction_type", transactionType)
            .put("limit_price", roundPrice(price))
        return JSONObject().put("ok", true).put("order", order).put("pricing", pricing)
    }

    private fun dispatch(
        api: String,
        apiVersion: String,
        method: String,
        path: String,
        requestJson: Any,
        sandboxToken: String,
        tradeRef: String
    ): JSONObject {
        assertSandboxOnly()
        if (sandboxToken.isBlank()) {
            return persistAndReturn(
                tradeRef = tradeRef,
                api = api,
                apiVersion = apiVersion,
                requestJson = requestJson,
                responseJson = JSONObject().put("error", "SANDBOX_TOKEN_REQUIRED"),
                httpStatus = 0,
                latencyMs = 0,
                errorCode = "SANDBOX_TOKEN_REQUIRED",
                errorMessage = "Sandbox token is required."
            )
        }

        val url = SANDBOX_BASE_URL + path
        if (!url.startsWith("$SANDBOX_BASE_URL/")) throw SecurityException("Order execution is locked to Upstox sandbox in this phase.")
        val startMs = System.currentTimeMillis()
        return try {
            // Exactly one transport call per dispatch: no retry loop here or in the client (G3).
            transport.execute(method, url, if (method == "DELETE") null else requestJson.toString(), sandboxToken).let { response ->
                val latencyMs = System.currentTimeMillis() - startMs
                val body = response.body
                val parsed = parseJson(body)
                val error = extractError(parsed, response.code, response.message)
                persistAndReturn(
                    tradeRef = tradeRef,
                    api = api,
                    apiVersion = apiVersion,
                    requestJson = requestJson,
                    responseJson = parsed,
                    httpStatus = response.code,
                    latencyMs = latencyMs,
                    errorCode = error.first,
                    errorMessage = error.second
                )
            }
        } catch (e: Exception) {
            val latencyMs = System.currentTimeMillis() - startMs
            persistAndReturn(
                tradeRef = tradeRef,
                api = api,
                apiVersion = apiVersion,
                requestJson = requestJson,
                responseJson = JSONObject().put("exception", e.message ?: e.javaClass.simpleName),
                httpStatus = 0,
                latencyMs = latencyMs,
                errorCode = "DISPATCH_EXCEPTION",
                errorMessage = (e.message ?: e.javaClass.simpleName) + " (outcome UNKNOWN: the request may have reached the broker; not retried)"
            )
        }
    }

    private fun persistAndReturn(
        tradeRef: String,
        api: String,
        apiVersion: String,
        requestJson: Any,
        responseJson: Any,
        httpStatus: Int,
        latencyMs: Long,
        errorCode: String?,
        errorMessage: String?
    ): JSONObject {
        val orderIds = extractOrderIds(responseJson)
        val row = JSONObject()
            .put("trade_ref", tradeRef)
            .put("api", api)
            .put("api_version", apiVersion)
            .put("request_json", requestJson)
            .put("response_json", responseJson)
            .put("http_status", httpStatus)
            .put("latency_ms", latencyMs)
            .put("order_ids", orderIds)
            .put("error_code", errorCode ?: JSONObject.NULL)
            .put("error_message", errorMessage ?: JSONObject.NULL)
        val persisted = try { persistRow(row) } catch (_: Exception) { false }
        val ok = httpStatus in 200..299 && errorCode == null
        if (!persisted) LogBuffer.add('E', TAG, "sandbox_orders persist failed for $api/$apiVersion trade_ref=$tradeRef")
        return JSONObject()
            .put("ok", ok)
            .put("mode", EXECUTION_MODE)
            .put("trade_ref", tradeRef)
            .put("api", api)
            .put("api_version", apiVersion)
            .put("http_status", httpStatus)
            .put("latency_ms", latencyMs)
            .put("order_ids", orderIds)
            .put("response_json", responseJson)
            .put("persisted", persisted)
            .put("error_code", errorCode ?: JSONObject.NULL)
            .put("error_message", errorMessage ?: JSONObject.NULL)
    }

    private fun assertSandboxOnly() {
        if (EXECUTION_MODE != "SANDBOX" || SANDBOX_BASE_URL != "https://sandbox.upstox.com") {
            throw SecurityException("Order execution is locked to Upstox sandbox in this phase.")
        }
    }

    private fun extractLegs(input: JSONObject): JSONArray {
        input.optJSONArray("legs")?.let { return it }
        input.optJSONArray("candidate_legs")?.let { return it }
        input.optJSONArray("orders")?.let { return it }
        input.optJSONObject("candidate")?.optJSONArray("legs")?.let { return it }
        input.optJSONObject("trade")?.optJSONArray("legs")?.let { return it }
        return JSONArray()
    }

    private fun extractTradeRef(input: JSONObject): String {
        return firstNonBlank(
            input.optString("trade_ref"),
            input.optString("trade_id"),
            input.optString("candidate_id"),
            input.optJSONObject("candidate")?.optString("candidate_id") ?: "",
            input.optJSONObject("trade")?.optString("trade_id") ?: "",
            "sandbox-debug-${System.currentTimeMillis()}"
        )
    }

    private fun resolveLotSize(root: JSONObject, leg: JSONObject, token: String): Int {
        val meta = root.optJSONObject("instrument_lot_sizes")
        val fromMap = meta?.optInt(token, 0) ?: 0
        if (fromMap > 0) return fromMap
        val legLot = leg.optInt("lot_size", leg.optInt("lotSize", 0))
        if (legLot > 0) return legLot
        return root.optInt("lot_size", root.optInt("lotSize", 0))
    }

    /** Default and ceiling for quote age (G1). A caller may tighten it with `max_quote_age_ms`, never widen it. */
    const val MAX_QUOTE_AGE_MS = 10_000L
    private const val MAX_QUOTE_FUTURE_SKEW_MS = 2_000L

    /**
     * G1: resolve the LIMIT price and record its provenance. This fails closed instead of guessing.
     * - REQUESTED_LIMIT: a positive leg `price` AND an explicit label (`price_source` on the leg or root, e.g.
     *   "requested_limit"). It is the caller's requested price only: never an executable-quote or fill claim.
     * - QUOTE_DERIVED_LIMIT: no leg price. The required side (BUY ask / SELL bid) comes from ONE quote object
     *   (`quotes`/`quote_map`[token] or `leg.quote`) that passes identity, timestamp/age and crossed-book checks.
     * - A positive leg price with no label (or with a quote label) is PRICE_SOURCE_AMBIGUOUS. LTP is never used as
     *   a price, so a missing/zero/invalid required side with a positive LTP is refused.
     */
    internal fun resolveLimitPrice(root: JSONObject, leg: JSONObject, token: String, transactionType: String, index: Int): PriceResolution {
        fun fail(code: String, message: String) = PriceResolution(Double.NaN, null, code, message, JSONObject())
        val label = firstNonBlank(leg.optString("price_source"), root.optString("price_source")).lowercase()
        val legPriceRaw = if (leg.has("price") && !leg.isNull("price")) leg.opt("price") else null
        val legPrice = leg.optDouble("price", Double.NaN)
        val legPricePresent = legPriceRaw != null && !(legPrice.isFinite() && legPrice == 0.0)

        if (label in REQUESTED_LABELS) {
            if (!(legPrice.isFinite() && legPrice > 0.0)) {
                return fail("REQUESTED_PRICE_INVALID", "Leg $index is labelled requested_limit but has no positive price.")
            }
            return PriceResolution(legPrice, PRICE_SOURCE_REQUESTED, null, null, JSONObject()
                .put("price_source", PRICE_SOURCE_REQUESTED)
                .put("requested_price", legPrice)
                .put("executable_quote", false)
                .put("fill_claim", false))
        }
        if (label.isNotEmpty() && label !in QUOTE_LABELS) {
            return fail("PRICE_SOURCE_UNKNOWN", "Leg $index price_source '$label' is not a recognised provenance.")
        }
        if (legPricePresent) {
            return fail(
                "PRICE_SOURCE_AMBIGUOUS",
                "Leg $index carries a price without price_source=requested_limit; refusing to guess whether it is a requested LIMIT or a quote/LTP value."
            )
        }

        val rootQuote = (root.optJSONObject("quotes") ?: root.optJSONObject("quote_map"))?.optJSONObject(token)
        val legQuote = leg.optJSONObject("quote")
        if (rootQuote != null && legQuote != null) {
            return fail("QUOTE_SOURCE_AMBIGUOUS", "Leg $index has both a root quote and a leg quote; supply exactly one.")
        }
        val quote = rootQuote ?: legQuote
        val sideKey = if (transactionType == "BUY") "ask" else "bid"
        if (quote == null) {
            return fail("EXECUTABLE_QUOTE_REQUIRED", "Leg $index has no quote object with a $sideKey; LTP is not an executable price.")
        }
        val quoteId = firstNonBlank(quote.optString("instrument_key"), quote.optString("instrument_token"), quote.optString("token"))
        if (quoteId.isNotEmpty() && quoteId != token) {
            return fail("QUOTE_IDENTITY_MISMATCH", "Leg $index quote identity $quoteId does not match $token.")
        }
        if (quoteId.isEmpty() && rootQuote == null) {
            return fail("QUOTE_IDENTITY_REQUIRED", "Leg $index quote has no instrument identity.")
        }
        if (quote.has("valid") && !quote.optBoolean("valid", false)) {
            return fail("QUOTE_INVALID", "Leg $index quote is marked invalid.")
        }
        // R3 (Codex PR #20): the default quote contract needs BOTH sides finite and positive with ask >= bid. A negative
        // side is always invalid. One-sided books are not executable quotes; no single-side exception is adopted here.
        val oppositeKey = if (sideKey == "ask") "bid" else "ask"
        fun readSide(key: String): Pair<String, Double> {
            val k = listOf(key, "${key}_price").firstOrNull { quote.has(it) && !quote.isNull(it) } ?: return "ABSENT" to Double.NaN
            val v = quote.optDouble(k, Double.NaN)
            return when {
                !v.isFinite() -> "INVALID" to v
                v < 0.0 -> "NEGATIVE" to v
                v == 0.0 -> "INVALID" to v
                else -> "OK" to v
            }
        }
        val (reqState, side) = readSide(sideKey)
        when (reqState) {
            "ABSENT" -> return fail("QUOTE_SIDE_REQUIRED", "Leg $index quote has no $sideKey (LTP is never substituted).")
            "NEGATIVE" -> return fail("QUOTE_SIDE_NEGATIVE", "Leg $index $sideKey is negative.")
            "INVALID" -> return fail("QUOTE_SIDE_INVALID", "Leg $index $sideKey is zero or invalid (LTP is never substituted).")
        }
        val (oppState, opposite) = readSide(oppositeKey)
        when (oppState) {
            "ABSENT" -> return fail("QUOTE_OPPOSITE_SIDE_REQUIRED", "Leg $index quote has no $oppositeKey; a one-sided book is not an executable quote.")
            "NEGATIVE" -> return fail("QUOTE_SIDE_NEGATIVE", "Leg $index $oppositeKey is negative.")
            "INVALID" -> return fail("QUOTE_OPPOSITE_SIDE_INVALID", "Leg $index $oppositeKey is zero or invalid; a one-sided book is not an executable quote.")
        }
        val bid = if (sideKey == "bid") side else opposite
        val ask = if (sideKey == "ask") side else opposite
        if (ask < bid) {
            return fail("QUOTE_CROSSED", "Leg $index quote is crossed (bid $bid > ask $ask).")
        }
        val tsMs = listOf("ts_ms", "quote_ts_ms", "timestamp_ms").firstNotNullOfOrNull { k ->
            quote.optLong(k, 0L).takeIf { it > 0L }
        } ?: return fail("QUOTE_TIMESTAMP_REQUIRED", "Leg $index quote has no timestamp; freshness cannot be validated.")
        val requestedMax = root.optLong("max_quote_age_ms", 0L)
        val maxAge = if (requestedMax in 1 until MAX_QUOTE_AGE_MS) requestedMax else MAX_QUOTE_AGE_MS
        val ageMs = clock() - tsMs
        if (ageMs > maxAge || ageMs < -MAX_QUOTE_FUTURE_SKEW_MS) {
            return fail("QUOTE_STALE", "Leg $index quote age ${ageMs}ms is outside 0..${maxAge}ms.")
        }
        return PriceResolution(side, PRICE_SOURCE_QUOTE, null, null, JSONObject()
            .put("price_source", PRICE_SOURCE_QUOTE)
            .put("quote_side", sideKey)
            .put("quote_side_value", side)
            .put("quote_bid", bid)
            .put("quote_ask", ask)
            .put("quote_ts_ms", tsMs)
            .put("quote_age_ms", ageMs)
            .put("max_quote_age_ms", maxAge)
            .put("identity_checked", true)
            .put("executable_quote", true)
            .put("fill_claim", false))
    }

    private fun firstPositiveInt(leg: JSONObject, root: JSONObject, keys: List<String>): Int {
        for (key in keys) {
            val legValue = leg.optInt(key, 0)
            if (legValue > 0) return legValue
            val rootValue = root.optInt(key, 0)
            if (rootValue > 0) return rootValue
        }
        return 0
    }

    private fun firstFiniteDouble(obj: JSONObject, keys: List<String>): Double {
        for (key in keys) {
            val value = obj.optDouble(key, Double.NaN)
            if (value.isFinite() && value > 0.0) return value
        }
        return Double.NaN
    }

    private fun normalizeTransactionType(value: String): String? {
        return when (value.trim().uppercase()) {
            "BUY", "B" -> "BUY"
            "SELL", "S" -> "SELL"
            else -> null
        }
    }

    private fun parseJson(body: String): Any {
        val trimmed = body.trim()
        if (trimmed.isBlank()) return JSONObject()
        return try {
            if (trimmed.startsWith("[")) JSONArray(trimmed) else JSONObject(trimmed)
        } catch (_: Exception) {
            JSONObject().put("raw", trimmed)
        }
    }

    private fun extractError(responseJson: Any, httpStatus: Int, httpMessage: String): Pair<String?, String?> {
        if (httpStatus in 200..299) return null to null
        val obj = responseJson as? JSONObject
        val errors = obj?.optJSONArray("errors")
        val firstError = errors?.optJSONObject(0)
        val code = firstNonBlank(
            firstError?.optString("errorCode") ?: "",
            firstError?.optString("error_code") ?: "",
            obj?.optString("code") ?: "",
            obj?.optString("error_code") ?: "",
            "HTTP_$httpStatus"
        )
        val message = firstNonBlank(
            firstError?.optString("message") ?: "",
            obj?.optString("message") ?: "",
            obj?.optString("error") ?: "",
            httpMessage
        )
        return code to message
    }

    private fun extractOrderIds(responseJson: Any): JSONArray {
        val out = JSONArray()
        fun visit(value: Any?) {
            when (value) {
                is JSONObject -> {
                    val id = value.optString("order_id")
                    if (id.isNotBlank()) out.put(id)
                    val ids = value.optJSONArray("order_ids")
                    if (ids != null) for (i in 0 until ids.length()) out.put(ids.optString(i))
                    value.optJSONObject("data")?.let { visit(it) }
                    value.optJSONArray("data")?.let { visit(it) }
                    value.optJSONObject("metadata")?.let { visit(it) }
                }
                is JSONArray -> for (i in 0 until value.length()) visit(value.opt(i))
            }
        }
        visit(responseJson)
        return out
    }

    // ---- G2: multi-order placement, parsed per line --------------------------------------------------------------
    const val PLACEMENT_ALL_ACCEPTED = "ALL_ACCEPTED"
    const val PLACEMENT_PARTIAL = "PARTIAL"
    const val PLACEMENT_ALL_REJECTED = "ALL_REJECTED"
    const val PLACEMENT_UNKNOWN = "UNKNOWN"
    const val PLACEMENT_STOPPED_AFTER_REJECTION = "STOPPED_AFTER_REJECTION"
    const val PLACEMENT_STOPPED_AFTER_UNCERTAIN = "STOPPED_AFTER_UNCERTAIN"
    const val LINE_ACCEPTED = "ACCEPTED"
    const val LINE_REJECTED = "REJECTED"
    const val LINE_PARTIAL = "PARTIAL"
    const val LINE_UNKNOWN = "UNKNOWN"

    private val SLICE_SUFFIX = Regex("^(.+)_(\\d+)$")

    /** Anomalies that make the whole outcome UNKNOWN even when some IDs were observed (never PARTIAL or ALL_*). */
    private val CONTRADICTION_ANOMALIES = setOf(
        "NO_HTTP_RESPONSE", "UNCERTAIN_TRANSPORT_STATUS", "REJECTION_STATUS_WITH_ORDER_IDS",
        "DUPLICATE_BROKER_ORDER_ID", "MULTIPLE_IDS_FOR_UNSLICED_LINE"
    )

    /**
     * Parse the multi-order response body and every order line, not merely the HTTP class.
     * - Accepted broker IDs are always preserved, including IDs whose correlation_id cannot be matched.
     * - Per-line rejections and unknown outcomes are kept.
     * - ALL_ACCEPTED requires every requested line accepted and no anomaly (summary/status/HTTP mismatch,
     *   malformed entries, unverifiable slice counts).
     * - Nothing here implies a fill.
     */
    internal fun parseMultiPlacement(httpStatus: Int, body: Any?, requested: JSONArray): JSONObject {
        val requestedIds = (0 until requested.length()).map { requested.getJSONObject(it).optString("correlation_id") }
        val sliceRequested = (0 until requested.length()).associate {
            requested.getJSONObject(it).optString("correlation_id") to requested.getJSONObject(it).optBoolean("slice", false)
        }
        val ids = linkedMapOf<String, MutableList<String>>().apply { requestedIds.forEach { put(it, mutableListOf()) } }
        val errs = linkedMapOf<String, MutableList<JSONObject>>().apply { requestedIds.forEach { put(it, mutableListOf()) } }
        val anomalies = linkedSetOf<String>()
        val unmatchedIds = JSONArray()
        val batchErrors = JSONArray()
        // R2: raw response records are retained verbatim as reconciliation evidence.
        val rawData = JSONArray()
        val rawErrors = JSONArray()

        fun resolveLine(cid: String): String? {
            if (cid in ids) return cid
            val m = SLICE_SUFFIX.matchEntire(cid) ?: return null
            val base = m.groupValues[1]
            return if (base in ids && sliceRequested[base] == true) base else null
        }

        val obj = body as? JSONObject
        val is2xx = httpStatus in 200..299
        val bodyStatus = obj?.optString("status", "")?.lowercase() ?: ""
        var dataPresent = false
        when {
            httpStatus == 0 -> anomalies += "NO_HTTP_RESPONSE"
            obj == null -> anomalies += "MALFORMED_BODY"
            obj.has("raw") && obj.length() == 1 -> anomalies += "MALFORMED_BODY"
            else -> {
                val data: JSONArray? = when (val d = obj.opt("data")) {
                    is JSONArray -> d
                    is JSONObject -> JSONArray().put(d)
                    else -> null
                }
                dataPresent = data != null
                if (data != null) for (i in 0 until data.length()) {
                    rawData.put(data.opt(i))
                    val e = data.opt(i) as? JSONObject
                    if (e == null) { anomalies += "MALFORMED_DATA_ENTRY"; continue }
                    val oid = e.optString("order_id").trim()
                    val cid = e.optString("correlation_id").trim()
                    if (oid.isEmpty()) { anomalies += "DATA_ENTRY_WITHOUT_ORDER_ID"; continue }
                    val line = resolveLine(cid)
                    if (line == null) { unmatchedIds.put(oid); anomalies += "UNMATCHED_ACCEPTED_ORDER_ID"; continue }
                    if (oid in ids.getValue(line)) { anomalies += "DUPLICATE_RESPONSE_RECORD"; continue }  // never a 2nd placement
                    ids.getValue(line) += oid
                }
                val errors = obj.optJSONArray("errors")
                if (errors != null) for (i in 0 until errors.length()) {
                    rawErrors.put(errors.opt(i))
                    val e = errors.opt(i) as? JSONObject
                    if (e == null) { anomalies += "MALFORMED_ERROR_ENTRY"; continue }
                    val err = JSONObject()
                        .put("error_code", firstNonBlank(e.optString("error_code"), e.optString("errorCode"), "UNSPECIFIED"))
                        .put("message", e.optString("message"))
                    val cid = e.optString("correlation_id").trim()
                    val line = if (cid.isEmpty()) null else resolveLine(cid)
                    if (line == null) batchErrors.put(err.put("correlation_id", cid)) else errs.getValue(line) += err
                }
                if (is2xx && !dataPresent && (errors == null || errors.length() == 0)) anomalies += "MALFORMED_2XX"
            }
        }

        val summary = obj?.optJSONObject("summary")
        val payloadErrors = summary?.optInt("payload_error", 0) ?: 0
        val requestRejectedWhole = (httpStatus in 400..499 && httpStatus != 408) || (payloadErrors > 0 && ids.values.all { it.isEmpty() })

        // R2: coherent identity. A broker ID belongs to exactly one requested line, and an unsliced accepted line has
        // exactly one broker ID. Every unmatched/batch error affects the aggregate.
        val linesById = linkedMapOf<String, MutableSet<String>>()
        ids.forEach { (line, list) -> list.forEach { linesById.getOrPut(it) { linkedSetOf() } += line } }
        for (i in 0 until unmatchedIds.length()) linesById.getOrPut(unmatchedIds.getString(i)) { linkedSetOf() } += "?unmatched"
        val reusedIdLines = linesById.filterValues { it.size > 1 }.values.flatten().toSet()
        if (linesById.values.any { it.size > 1 }) anomalies += "DUPLICATE_BROKER_ORDER_ID"
        val multiIdLines = ids.filter { (line, list) -> sliceRequested[line] != true && list.size > 1 }.keys
        if (multiIdLines.isNotEmpty()) anomalies += "MULTIPLE_IDS_FOR_UNSLICED_LINE"
        if (batchErrors.length() > 0) anomalies += "UNMATCHED_OR_BATCH_ERROR"

        // R1: one conservative classification. Only a 2xx transport status can support acceptance. 408/5xx/other
        // non-2xx statuses are uncertain; a rejection status (4xx) that carries order IDs is contradictory.
        val anyIds = ids.values.any { it.isNotEmpty() } || unmatchedIds.length() > 0
        val rejectionStatus = httpStatus in 400..499 && httpStatus != 408
        if (httpStatus != 0 && !is2xx) {
            if (rejectionStatus && anyIds) anomalies += "REJECTION_STATUS_WITH_ORDER_IDS"
            else if (!rejectionStatus) anomalies += "UNCERTAIN_TRANSPORT_STATUS"
        }
        val transportBlocksAcceptance = !is2xx

        val lines = JSONArray()
        var accepted = 0; var rejected = 0; var partial = 0; var unknown = 0
        for (cid in requestedIds) {
            val lineIds = ids.getValue(cid); val lineErrs = errs.getValue(cid)
            val outcome = when {
                lineIds.isNotEmpty() && (cid in reusedIdLines || cid in multiIdLines || transportBlocksAcceptance) -> LINE_UNKNOWN
                lineIds.isNotEmpty() && lineErrs.isEmpty() -> LINE_ACCEPTED
                lineIds.isNotEmpty() -> LINE_PARTIAL
                lineErrs.isNotEmpty() -> LINE_REJECTED
                requestRejectedWhole && "NO_HTTP_RESPONSE" !in anomalies -> LINE_REJECTED
                else -> LINE_UNKNOWN
            }
            when (outcome) { LINE_ACCEPTED -> accepted++; LINE_REJECTED -> rejected++; LINE_PARTIAL -> partial++; else -> unknown++ }
            if (sliceRequested[cid] == true && lineIds.isNotEmpty()) anomalies += "SLICE_COUNT_UNVERIFIED"
            lines.put(JSONObject()
                .put("correlation_id", cid)
                .put("outcome", outcome)
                .put("order_ids", JSONArray(lineIds))
                .put("errors", JSONArray(lineErrs))
                .put("slice_requested", sliceRequested[cid] == true)
                .put("rejected_with_request", outcome == LINE_REJECTED && lineErrs.isEmpty()))
        }

        if (summary != null && "NO_HTTP_RESPONSE" !in anomalies) {
            val acceptedEntries = linesById.size  // distinct broker IDs, never duplicate records
            val lineErrorEntries = errs.values.sumOf { it.size }
            val noSlices = sliceRequested.values.none { it }
            if ((noSlices && summary.has("total") && summary.optInt("total", -1) != requestedIds.size) ||
                (summary.has("success") && summary.optInt("success", -1) != acceptedEntries) ||
                (noSlices && summary.optInt("success", 0) > requestedIds.size) ||
                (summary.has("error") && payloadErrors == 0 && summary.optInt("error", -1) != lineErrorEntries)) {
                anomalies += "SUMMARY_MISMATCH"
            }
        }
        val n = requestedIds.size
        if (obj != null && bodyStatus.isNotEmpty() && "MALFORMED_BODY" !in anomalies) {
            val expected = when {
                accepted == n -> "success"
                accepted + partial == 0 && rejected == n -> "error"
                else -> "partial_success"
            }
            if (bodyStatus !in setOf("success", "partial_success", "error")) anomalies += "UNKNOWN_BODY_STATUS"
            else if (bodyStatus != expected && unknown == 0) anomalies += "BODY_STATUS_MISMATCH"
        }
        if (httpStatus == 207 && accepted == n) anomalies += "HTTP_STATUS_MISMATCH"
        if (httpStatus == 200 && accepted != n && accepted + partial > 0) anomalies += "HTTP_STATUS_MISMATCH"

        // Distinct observed broker IDs (a reused ID is one observed order, not two). Raw records keep the full detail.
        val allAcceptedIds = JSONArray().apply { linesById.keys.forEach { put(it) } }
        val contradictory = anomalies.any { it in CONTRADICTION_ANOMALIES }
        val placementStatus = when {
            contradictory -> PLACEMENT_UNKNOWN
            anomalies.isEmpty() && is2xx && n > 0 && accepted == n -> PLACEMENT_ALL_ACCEPTED
            anomalies.isEmpty() && n > 0 && rejected == n && allAcceptedIds.length() == 0 -> PLACEMENT_ALL_REJECTED
            allAcceptedIds.length() > 0 && accepted < n -> PLACEMENT_PARTIAL
            else -> PLACEMENT_UNKNOWN
        }
        return JSONObject()
            .put("placement_status", placementStatus)
            .put("http_status", httpStatus)
            .put("body_status", bodyStatus.ifEmpty { JSONObject.NULL })
            .put("lines", lines)
            .put("accepted_order_ids", allAcceptedIds)
            .put("unmatched_accepted_order_ids", unmatchedIds)
            .put("batch_errors", batchErrors)
            .put("raw_data_records", rawData)
            .put("raw_error_records", rawErrors)
            .put("anomalies", JSONArray(anomalies.toList()))
            .put("summary_reported", summary ?: JSONObject.NULL)
            .put("counts", JSONObject().put("requested", n).put("accepted", accepted).put("rejected", rejected)
                .put("partial", partial).put("unknown", unknown))
            .put("fill_status", FILL_STATUS_UNRECONCILED)
            .put("fill_implied", false)
    }

    // ---- G3: single-order submission outcome and sequential stop rule ---------------------------------------------

    /** ACCEPTED only for 2xx + status success (when given) + a broker order_id. 408/5xx/no response/contradictions are UNKNOWN. */
    internal fun classifySinglePlacement(httpStatus: Int, body: Any?): JSONObject {
        val obj = body as? JSONObject
        val dataObj = obj?.optJSONObject("data")
        val orderId = firstNonBlank(dataObj?.optString("order_id") ?: "", obj?.optString("order_id") ?: "")
        val status = obj?.optString("status", "")?.lowercase() ?: ""
        val hasErrors = (obj?.optJSONArray("errors")?.length() ?: 0) > 0
        val (outcome, reason) = when {
            httpStatus == 0 -> LINE_UNKNOWN to "NO_HTTP_RESPONSE"
            httpStatus in 200..299 && orderId.isNotEmpty() && (status.isEmpty() || status == "success") && !hasErrors -> LINE_ACCEPTED to "ACKNOWLEDGED"
            httpStatus in 200..299 -> LINE_UNKNOWN to "MALFORMED_OR_CONTRADICTORY_2XX"
            httpStatus == 408 -> LINE_UNKNOWN to "HTTP_408"
            httpStatus in 400..499 && orderId.isEmpty() -> LINE_REJECTED to "HTTP_$httpStatus"
            httpStatus in 400..499 -> LINE_UNKNOWN to "REJECTION_WITH_ORDER_ID"
            else -> LINE_UNKNOWN to "HTTP_$httpStatus"
        }
        return JSONObject()
            .put("outcome", outcome)
            .put("reason", reason)
            .put("order_id", orderId.ifEmpty { JSONObject.NULL })
            .put("fill_status", FILL_STATUS_UNRECONCILED)
            .put("fill_implied", false)
    }

    /**
     * Sandbox debug helper. Submit in build order (BUY legs first) and stop the whole remaining sequence after the first
     * REJECTED or UNKNOWN submission. Earlier accepted broker IDs are retained. No automatic retry, including on timeout.
     * Even ALL_ACCEPTED means placement acknowledged only: no fill, and no authority for a dependent live short entry.
     */
    private fun placeSequential(built: BuildResult, sandboxToken: String, tradeRef: String): JSONObject {
        val responses = JSONArray(); val submissions = JSONArray(); val acceptedIds = JSONArray()
        var stopOutcome: String? = null; var stoppedAt = -1
        val n = built.orders.length()
        for (i in 0 until n) {
            val order = built.orders.getJSONObject(i)
            val raw = dispatch("place", "v2", "POST", "/v2/order/place", order, sandboxToken, tradeRef)
            val cls = classifySinglePlacement(raw.optInt("http_status", 0), raw.opt("response_json"))
            raw.put("placement", cls)
            responses.put(raw)
            submissions.put(JSONObject()
                .put("index", i)
                .put("correlation_id", order.optString("correlation_id"))
                .put("outcome", cls.getString("outcome"))
                .put("reason", cls.getString("reason"))
                .put("order_id", cls.opt("order_id")))
            if (cls.getString("outcome") == LINE_ACCEPTED) {
                acceptedIds.put(cls.getString("order_id"))
            } else {
                stopOutcome = cls.getString("outcome"); stoppedAt = i
                break
            }
        }
        val notSubmitted = JSONArray()
        if (stoppedAt >= 0) for (j in stoppedAt + 1 until n) notSubmitted.put(built.orders.getJSONObject(j).optString("correlation_id"))
        val status = when (stopOutcome) {
            null -> PLACEMENT_ALL_ACCEPTED
            LINE_REJECTED -> PLACEMENT_STOPPED_AFTER_REJECTION
            else -> PLACEMENT_STOPPED_AFTER_UNCERTAIN
        }
        return JSONObject()
            .put("ok", stopOutcome == null)
            .put("mode", EXECUTION_MODE)
            .put("trade_ref", tradeRef)
            .put("placement_status", status)
            .put("responses", responses)
            .put("submissions", submissions)
            .put("accepted_order_ids", acceptedIds)
            .put("order_ids", acceptedIds)
            .put("not_submitted_correlation_ids", notSubmitted)
            .put("stopped_at_index", if (stoppedAt >= 0) stoppedAt else JSONObject.NULL)
            .put("auto_retry", false)
            .put("pricing", built.pricing)
            .put("fill_status", FILL_STATUS_UNRECONCILED)
            .put("fill_implied", false)
            .put("dependent_live_short_entry_authorized", false)
            .put("error_code", if (stopOutcome == null) JSONObject.NULL else "SEQUENCE_STOPPED_$stopOutcome")
            .put("error_message", if (stopOutcome == null) JSONObject.NULL
                else "Stopped after submission ${stoppedAt + 1}/$n ($stopOutcome); ${notSubmitted.length()} remaining not submitted; earlier accepted IDs retained.")
    }

    private fun roundPrice(price: Double): Double = kotlin.math.round(price * 100.0) / 100.0

    private fun legError(code: String, message: String): JSONObject {
        return JSONObject().put("ok", false).put("error_code", code).put("error_message", message)
    }

    private fun firstNonBlank(vararg values: String): String {
        return values.firstOrNull { it.trim().isNotBlank() }?.trim() ?: ""
    }
}
