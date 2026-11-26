package com.marketradar.app

import java.time.OffsetDateTime
import org.json.JSONObject

/** Server lease duration translated to a monotonic local deadline, with RTT deducted.
 * Changing the phone's wall clock cannot extend permission to monitor or write.
 */
object CloudLeaseClock {
    fun deadline(reply: JSONObject, sentElapsed: Long, receivedElapsed: Long): Long {
        val until = OffsetDateTime.parse(reply.getString("lease_until")).toInstant().toEpochMilli()
        val server = OffsetDateTime.parse(reply.getString("server_now")).toInstant().toEpochMilli()
        val remaining = (until - server).coerceIn(0, 120_000)
        val rtt = (receivedElapsed - sentElapsed).coerceAtLeast(0)
        return receivedElapsed + (remaining - rtt - 5_000).coerceAtLeast(0)
    }
}
