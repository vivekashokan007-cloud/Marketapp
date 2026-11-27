package com.marketradar.app

import org.json.JSONObject

/** The notification uses the same BNF-only identity as the persisted poll. */
internal object PollNotification {
    fun body(poll: JSONObject): String =
        "BNF: ${poll.getDouble("bnf")} | VIX: ${poll.getDouble("vix")}"
}
