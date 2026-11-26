package com.marketradar.app

import com.google.protobuf.util.JsonFormat
import com.upstox.marketdatafeederv3udapi.rpc.proto.MarketDataFeedV3
import org.json.JSONObject
import java.util.Base64

object BnfFeedDecoder {
    fun frame(raw: ByteArray, receiptMs: Long): JSONObject {
        val row = JSONObject().put("receipt_ms", receiptMs).put("raw_sha256", CloudVault.hash(raw))
            .put("raw_protobuf_base64", Base64.getEncoder().encodeToString(raw))
        try {
            require(raw.isNotEmpty())
            val decoded = MarketDataFeedV3.FeedResponse.parseFrom(raw)
            row.put("decoded", JSONObject(JsonFormat.printer().preservingProtoFieldNames().print(decoded)))
                .put("decode_status", "DECODED")
        } catch (_: Exception) { row.put("decode_status", "DECODE_FAILED") }
        return row
    }
}
