package com.marketradar.app

import com.google.protobuf.util.JsonFormat
import com.upstox.marketdatafeederv3udapi.rpc.proto.MarketDataFeedV3
import okhttp3.*
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.Base64
import java.util.UUID
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/** Observational feed only. Raw Protobuf and decoded fields share one durable batch. */
class BnfWebSocketCapture(private val root: File) {
    private val executor = Executors.newSingleThreadScheduledExecutor()
    private val client = OkHttpClient.Builder().readTimeout(0, TimeUnit.MILLISECONDS).build()
    private var socket: WebSocket? = null
    private var generation = 0L
    private var connectedIdentity = ""
    private var frames = JSONArray()
    private var bytes = 0
    private var started = 0L
    private var lastAttempt = 0L
    init { event("PROCESS_START_POSSIBLE_PRIOR_GAP", emptyList()); executor.scheduleWithFixedDelay({ synchronized(this) { runCatching { flush() } } }, 15, 15, TimeUnit.SECONDS) }

    @Synchronized fun ensure(token: String, keys: List<String>) {
        if (bytes >= 1024 * 1024) { flush(); if (bytes >= 1024 * 1024) return }
        val selected = keys.distinct().take(32)
        if (selected.isEmpty()) return
        val identity = CloudVault.hash((token + selected.joinToString()).toByteArray())
        if (identity == connectedIdentity && socket != null) return
        val now = System.currentTimeMillis()
        if (now - lastAttempt < 30_000) return
        lastAttempt = now
        generation++; val ownGeneration = generation
        socket?.cancel(); socket = null; connectedIdentity = identity
        executor.execute {
            try {
                val req = Request.Builder().url("https://api.upstox.com/v3/feed/market-data-feed/authorize")
                    .header("Authorization", "Bearer $token").header("Accept", "application/json").build()
                val url = client.newCall(req).execute().use { r ->
                    require(r.isSuccessful)
                    JSONObject(r.body!!.string()).getJSONObject("data").getString("authorized_redirect_uri")
                }
                val uri = java.net.URI(url)
                require(uri.scheme == "wss" && uri.host.endsWith(".upstox.com"))
                synchronized(this) {
                    if (generation != ownGeneration) return@execute
                    socket = client.newWebSocket(Request.Builder().url(url).build(), object: WebSocketListener() {
                        override fun onOpen(ws: WebSocket, response: Response) {
                            synchronized(this@BnfWebSocketCapture) {
                                if (generation != ownGeneration) { ws.cancel(); return }
                                val subscription = JSONObject().put("guid", UUID.randomUUID().toString()).put("method", "sub")
                                    .put("data", JSONObject().put("mode", "full").put("instrumentKeys", JSONArray(selected)))
                                if (!ws.send(subscription.toString().toByteArray().toByteString())) {
                                    event("SUBSCRIPTION_SEND_FAILED", selected); socket = null; ws.cancel(); return
                                }
                                event("CONNECTED", selected)
                            }
                        }
                        override fun onMessage(ws: WebSocket, payload: ByteString) {
                            synchronized(this@BnfWebSocketCapture) {
                                if (generation != ownGeneration) return
                                val received = System.currentTimeMillis()
                                val raw = payload.toByteArray()
                                val row = BnfFeedDecoder.frame(raw, received)
                                if (started == 0L) started = received
                                frames.put(row); bytes += row.toString().length
                                if (bytes >= 256 * 1024) flush()
                                if (bytes >= 1024 * 1024) {
                                    event("STORAGE_FAILURE_CAPTURE_PAUSED", selected)
                                    ws.cancel(); socket = null
                                }
                            }
                        }
                        override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
                            synchronized(this@BnfWebSocketCapture) {
                                if (generation == ownGeneration) { socket = null; event("CONNECTION_FAILED", selected); flush() }
                            }
                        }
                        override fun onClosed(ws: WebSocket, code: Int, reason: String) {
                            synchronized(this@BnfWebSocketCapture) {
                                if (generation == ownGeneration) { socket = null; event("DISCONNECTED", selected); flush() }
                            }
                        }
                    })
                }
            } catch (_: Exception) { synchronized(this) { if (generation == ownGeneration) { socket = null; event("AUTH_OR_CONNECT_FAILED", selected); flush() } } }
        }
    }
    private fun event(state: String, keys: List<String>) {
        if (started == 0L) started = System.currentTimeMillis()
        frames.put(JSONObject().put("receipt_ms", System.currentTimeMillis()).put("connection_state", state)
            .put("subscribed_keys", JSONArray(keys)).put("mode", "full"))
    }
    private fun flush() {
        // Never clear a batch until its atomic outbox write succeeds.
        if (frames.length() == 0) return
        val body = JSONObject().put("schema", "upstox_v3_frames_v1")
            .put("frames", frames).toString().toByteArray()
        if (BnfRawCapture.record(root, "marketradar://capture/websocket", started,
                System.currentTimeMillis(), 200, body)) {
            frames = JSONArray(); bytes = 0; started = 0
        }
    }
    @Synchronized fun close() {
        generation++; socket?.cancel(); socket = null
        flush(); executor.shutdown()
    }
}
