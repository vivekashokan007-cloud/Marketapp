package com.marketradar.app

import org.json.JSONObject
import java.net.URI
import java.net.URLDecoder

/** Extract table metadata without reinterpreting or changing the original captured URL. */
internal object BnfSourceUrl {
    data class Metadata(val source: String, val endpoint: String, val params: JSONObject)

    fun parse(url: String): Metadata {
        require(BnfRawCapture.eligible(url))
        // Upstox instrument keys sometimes appear as a literal | (or spaces) in
        // the query. URI(url) rejects those otherwise valid captured requests.
        val base = URI(url.substringBefore('?'))
        val params = JSONObject()
        if ('?' in url) url.substringAfter('?').split('&').forEach { part ->
            val pair = part.split('=', limit = 2)
            params.put(URLDecoder.decode(pair[0], "UTF-8"),
                URLDecoder.decode(pair.getOrElse(1) { "" }, "UTF-8"))
        }
        return Metadata(if (base.scheme == "marketradar") "market_radar" else "upstox", base.path, params)
    }
}
