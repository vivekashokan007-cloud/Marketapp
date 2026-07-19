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

    private val client = OkHttpClient.Builder()
        .connectTimeout(12, TimeUnit.SECONDS)
        .readTimeout(20, TimeUnit.SECONDS)
        .build()

    data class BuildResult(
        val ok: Boolean,
        val tradeRef: String,
        val orders: JSONArray,
        val errorCode: String? = null,
        val errorMessage: String? = null
    )

    fun buildOrders(input: JSONObject): BuildResult {
        val tradeRef = extractTradeRef(input)
        return try {
            val rawLegs = extractLegs(input)
            if (rawLegs.length() == 0) {
                return BuildResult(false, tradeRef, JSONArray(), "NO_LEGS", "No candidate/trade legs were supplied.")
            }

            val orders = mutableListOf<JSONObject>()
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
            }

            val sequenced = orders.sortedWith(
                compareBy<JSONObject> { if (it.optString("transaction_type") == "BUY") 0 else 1 }
                    .thenBy { it.optString("correlation_id") }
            )
            BuildResult(true, tradeRef, JSONArray().apply { sequenced.forEach { put(it) } })
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
                    .put("error_code", built.errorCode ?: JSONObject.NULL)
                    .put("error_message", built.errorMessage ?: JSONObject.NULL)
            }
            "place_sequential" -> {
                val built = buildOrders(payload)
                if (!built.ok) return buildFailure(built)
                val responses = JSONArray()
                for (i in 0 until built.orders.length()) {
                    val order = built.orders.getJSONObject(i)
                    responses.put(dispatch("place", "v2", "POST", "/v2/order/place", order, sandboxToken, tradeRef))
                }
                JSONObject().put("ok", true).put("mode", EXECUTION_MODE).put("trade_ref", tradeRef).put("responses", responses)
            }
            "place_multi" -> {
                val built = buildOrders(payload)
                if (!built.ok) return buildFailure(built)
                dispatch("multi", "v2", "POST", "/v2/order/multi/place", built.orders, sandboxToken, tradeRef)
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

        val price = resolveExecutablePrice(root, leg, token, transactionType)
        if (!price.isFinite() || price <= 0.0) {
            return legError("EXECUTABLE_PRICE_REQUIRED", "Leg $index needs a positive LIMIT price from BUY ask or SELL bid.")
        }

        val correlationId = firstNonBlank(leg.optString("correlation_id"), "${index + 1}")
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
        return JSONObject().put("ok", true).put("order", order)
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
        val startMs = System.currentTimeMillis()
        return try {
            val builder = Request.Builder()
                .url(url)
                .addHeader("Accept", JSON_MEDIA_TYPE)
                .addHeader("Content-Type", JSON_MEDIA_TYPE)
                .addHeader("Authorization", "Bearer $sandboxToken")
            val request = when (method) {
                "POST" -> builder.post(requestJson.toString().toRequestBody(JSON_MEDIA_TYPE.toMediaTypeOrNull())).build()
                "PUT" -> builder.put(requestJson.toString().toRequestBody(JSON_MEDIA_TYPE.toMediaTypeOrNull())).build()
                "DELETE" -> builder.delete().build()
                else -> throw IllegalArgumentException("Unsupported method $method")
            }

            client.newCall(request).execute().use { response ->
                val latencyMs = System.currentTimeMillis() - startMs
                val body = response.body?.string() ?: ""
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
                errorMessage = e.message ?: e.javaClass.simpleName
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
        val persisted = SupabaseClient.insertSandboxOrder(row)
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

    private fun resolveExecutablePrice(root: JSONObject, leg: JSONObject, token: String, transactionType: String): Double {
        val explicit = leg.optDouble("price", Double.NaN)
        if (explicit.isFinite() && explicit > 0.0) return explicit
        val quotes = root.optJSONObject("quotes") ?: root.optJSONObject("quote_map")
        val quote = quotes?.optJSONObject(token)
        val sideKey = if (transactionType == "BUY") "ask" else "bid"
        val altSideKey = if (transactionType == "BUY") "ask_price" else "bid_price"
        val mapped = quote?.let { firstFiniteDouble(it, listOf(sideKey, altSideKey, "ltp")) } ?: Double.NaN
        if (mapped.isFinite() && mapped > 0.0) return mapped
        return firstFiniteDouble(leg, listOf(sideKey, altSideKey, "ltp"))
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

    private fun roundPrice(price: Double): Double = kotlin.math.round(price * 100.0) / 100.0

    private fun legError(code: String, message: String): JSONObject {
        return JSONObject().put("ok", false).put("error_code", code).put("error_message", message)
    }

    private fun firstNonBlank(vararg values: String): String {
        return values.firstOrNull { it.trim().isNotBlank() }?.trim() ?: ""
    }
}
